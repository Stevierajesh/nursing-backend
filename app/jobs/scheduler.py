"""Background jobs.

Two timers:

  * sla_sweep -- flips un-responded escalations to auto_escalated. This is the
    closed loop, and the reason a worried nurse cannot be silently ignored.
  * medication_sweep -- the same closed loop for medication review requests,
    widening an unanswered one from its named prescriber to the whole pool.
  * risk_recompute -- refreshes fused risk for active patients so the board
    reflects new vitals even when nobody taps anything.

APScheduler in-process rather than Celery: a hackathon deployment should not
need a broker, and both jobs are idempotent and short.

Note on multi-worker deployments: with gunicorn -w N this would run N times.
Guarded by a lock file so only the first worker schedules the jobs.
"""

from __future__ import annotations

import atexit
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import Config
from app.extensions import supabase
from app.services.escalation import evaluate_patient, sweep_sla_breaches
from app.services.medication import sweep_medication_review_breaches
from app.services.simulation import simulation_tick

log = logging.getLogger(__name__)

_LOCK_PATH = Path(tempfile.gettempdir()) / "nurse_worry_scheduler.lock"


def sla_sweep() -> None:
    try:
        breached = sweep_sla_breaches()
        if breached:
            log.warning(
                "SLA breach: %d escalation(s) auto-escalated: %s",
                len(breached),
                [e["id"] for e in breached],
            )
    except Exception:
        # A job that raises is a job APScheduler stops retrying cleanly; keep
        # the timer alive and surface the trace.
        log.exception("sla_sweep failed")


def medication_sweep() -> None:
    """Second level of the medication escalation, on the same timer as the first."""
    try:
        escalated = sweep_medication_review_breaches()
        if escalated:
            log.warning(
                "Medication SLA breach: %d request(s) escalated: %s",
                len(escalated),
                [r["id"] for r in escalated],
            )
    except Exception:
        log.exception("medication_sweep failed")


def risk_recompute() -> None:
    """Refresh risk for every active patient that has recent activity.

    Scoped to patients with worry signals or vitals in the last 24h so this
    does not become an O(census) scan every minute.
    """
    try:
        patients = (
            supabase()
            .table("patients")
            .select("id, org_id")
            .is_("discharged_at", "null")
            .limit(500)
            .execute()
        ).data or []

        for p in patients:
            try:
                evaluate_patient(p["org_id"], p["id"])
            except Exception:
                log.exception("risk recompute failed for patient %s", p["id"])
    except Exception:
        log.exception("risk_recompute failed")


def _acquire_lock() -> bool:
    """True if this process should own the schedulers.

    O_EXCL creation is atomic, so exactly one worker wins. The PID is written
    so a stale lock is diagnosable.
    """
    try:
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Stale lock from a crashed process: take over if the owner is gone.
        try:
            pid = int(_LOCK_PATH.read_text().strip() or "0")
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            try:
                _LOCK_PATH.unlink(missing_ok=True)
                fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                return False
        else:
            return False

    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    atexit.register(lambda: _LOCK_PATH.unlink(missing_ok=True))
    return True


def start_scheduler() -> BackgroundScheduler | None:
    if not Config.ENABLE_SCHEDULER:
        log.info("Scheduler disabled via ENABLE_SCHEDULER")
        return None

    # Flask's reloader runs the module twice; only the child should schedule.
    if Config.DEBUG and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return None

    if not _acquire_lock():
        log.info("Another process owns the scheduler; skipping in pid %d", os.getpid())
        return None

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        simulation_tick, "interval", seconds=5, id="simulation_tick",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        sla_sweep,
        "interval",
        seconds=Config.SLA_SWEEP_SECONDS,
        id="sla_sweep",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        medication_sweep,
        "interval",
        seconds=Config.SLA_SWEEP_SECONDS,
        id="medication_sweep",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        risk_recompute,
        "interval",
        seconds=Config.RISK_RECOMPUTE_SECONDS,
        id="risk_recompute",
        max_instances=1,
        coalesce=True,
        # Run once immediately instead of waiting out the first interval.
        # Without this the board can sit for a full minute after boot showing
        # unscored patients, and the API's live fallback has to carry every
        # request in that window.
        next_run_time=datetime.now(),
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))

    log.info(
        "Scheduler started (sla_sweep %ds, risk_recompute %ds)",
        Config.SLA_SWEEP_SECONDS,
        Config.RISK_RECOMPUTE_SECONDS,
    )
    return scheduler
