"""Seed the demo environment.

Builds a hospital unit that tells the story on stage:

  * Robert Ellis -- the hero case. NEWS is only 3-4, so no existing early
    warning system would fire, but it has been climbing for six hours. When
    the nurse taps "very worried / breathing off", the layer escalates.
  * Yolanda Price -- stable, and a nurse is worried anyway. Produces a flag
    and deliberately no page. This is the precision story.
  * Henry Okafor -- objectively crashing, nobody has tapped. Escalates on
    NEWS alone, proving the layer augments rather than replaces.
  * Plus historical escalations with response times, demographic spread, and
    outcomes, so the metrics and equity screens have real data behind them.

Idempotent: re-running wipes and rebuilds the demo org only.

    python scripts/seed.py
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from app.extensions import supabase  # noqa: E402
from app.services.news import news_score  # noqa: E402

random.seed(42)  # reproducible demos

ORG_SLUG = "mercy-general"
DEMO_PASSWORD = "WorryLayer!2026"


def iso(dt: datetime) -> str:
    return dt.isoformat()


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


def wipe_demo_org() -> None:
    """Remove the demo org and its auth users.

    Domain tables cascade from organizations; auth.users do not, so they are
    deleted explicitly or a re-seed collides on the email unique constraint.
    """
    client = supabase()
    existing = (
        client.table("organizations").select("id").eq("slug", ORG_SLUG).execute()
    ).data or []
    if not existing:
        return

    org_id = existing[0]["id"]
    profiles = (
        client.table("profiles").select("id").eq("org_id", org_id).execute()
    ).data or []

    print(f"  removing existing demo org {org_id} ({len(profiles)} users)")
    client.table("organizations").delete().eq("id", org_id).execute()

    for p in profiles:
        try:
            client.auth.admin.delete_user(p["id"])
        except Exception as exc:
            print(f"  ! could not delete auth user {p['id']}: {exc}")


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def create_org() -> str:
    row = (
        supabase()
        .table("organizations")
        .insert(
            {
                "name": "Mercy General Hospital",
                "slug": ORG_SLUG,
                "worry_sla_minutes": 20,
            }
        )
        .execute()
    ).data[0]
    print(f"  org: {row['name']} ({row['id']})")
    return row["id"]


def create_units(org_id: str) -> dict[str, str]:
    names = ["4 West — Med/Surg", "6 North — Telemetry"]
    rows = (
        supabase()
        .table("units")
        .insert([{"org_id": org_id, "name": n} for n in names])
        .execute()
    ).data
    out = {r["name"]: r["id"] for r in rows}
    for name in out:
        print(f"  unit: {name}")
    return out


def create_user(
    org_id: str, email: str, full_name: str, role: str, unit_id: str | None
) -> str:
    client = supabase()
    created = client.auth.admin.create_user(
        {
            "email": email,
            "password": DEMO_PASSWORD,
            "email_confirm": True,
            "user_metadata": {"full_name": full_name},
        }
    )
    uid = created.user.id
    client.table("profiles").insert(
        {
            "id": uid,
            "org_id": org_id,
            "unit_id": unit_id,
            "role": role,
            "full_name": full_name,
            "email": email,
        }
    ).execute()
    print(f"  user: {email:28} {role:14} {full_name}")
    return uid


def create_patient(org_id: str, unit_id: str, spec: dict) -> str:
    row = (
        supabase()
        .table("patients")
        .insert(
            {
                "org_id": org_id,
                "unit_id": unit_id,
                "mrn_hash": spec["mrn"],
                "display_name": spec["name"],
                "room": spec["room"],
                "age_band": spec["age_band"],
                "sex": spec["sex"],
                "race_code": spec["race_code"],
                "primary_language": spec["language"],
                "admitted_at": iso(now() - timedelta(days=spec.get("los_days", 2))),
            }
        )
        .execute()
    ).data[0]
    return row["id"]


def insert_vitals_series(
    org_id: str, patient_id: str, series: list[tuple[int, dict]]
) -> None:
    """series: list of (minutes_ago, vitals_overrides), newest last."""
    rows = []
    for minutes_ago, overrides in series:
        v = {
            "heart_rate": 74,
            "resp_rate": 16,
            "spo2": 98,
            "systolic_bp": 122,
            "diastolic_bp": 76,
            "temperature_c": 36.7,
            "supplemental_o2": False,
            "consciousness": "A",
        }
        v.update(overrides)
        v["news_score"] = news_score(v)
        v["org_id"] = org_id
        v["patient_id"] = patient_id
        v["recorded_at"] = iso(now() - timedelta(minutes=minutes_ago))
        rows.append(v)
    supabase().table("vitals").insert(rows).execute()


# ---------------------------------------------------------------------------
# patient scenarios
# ---------------------------------------------------------------------------

# The hero case. Each observation is individually unremarkable -- NEWS peaks
# at 4, below the medium-response band -- but the trajectory is unmistakable.
# This is precisely the patient the nurse notices and the score does not.
ROBERT_DRIFT = [
    (360, {"resp_rate": 16, "spo2": 98, "heart_rate": 72}),
    (300, {"resp_rate": 17, "spo2": 97, "heart_rate": 78}),
    (240, {"resp_rate": 18, "spo2": 96, "heart_rate": 84}),
    (180, {"resp_rate": 19, "spo2": 96, "heart_rate": 88}),
    (120, {"resp_rate": 20, "spo2": 95, "heart_rate": 94}),
    (60, {"resp_rate": 21, "spo2": 95, "heart_rate": 98}),
    (15, {"resp_rate": 22, "spo2": 94, "heart_rate": 104}),
]

# Flat and boring. Worry on this patient must not page anyone.
STABLE = [(m, {}) for m in (330, 270, 210, 150, 90, 30)]

# Objectively bad right now, nobody has tapped worry.
HENRY_CRASH = [
    (300, {"resp_rate": 18, "spo2": 96}),
    (240, {"resp_rate": 20, "spo2": 95, "heart_rate": 96}),
    (180, {"resp_rate": 22, "spo2": 94, "heart_rate": 108, "systolic_bp": 108}),
    (120, {"resp_rate": 24, "spo2": 92, "heart_rate": 118, "systolic_bp": 98,
           "supplemental_o2": True}),
    (20, {"resp_rate": 26, "spo2": 91, "heart_rate": 124, "systolic_bp": 92,
          "supplemental_o2": True, "temperature_c": 38.4}),
]

MILD = [
    (300, {}),
    (240, {"resp_rate": 18}),
    (180, {"resp_rate": 19, "heart_rate": 92}),
    (60, {"resp_rate": 20, "heart_rate": 95}),
    (20, {"resp_rate": 20, "heart_rate": 96}),
]

PATIENTS = [
    {
        "name": "Robert Ellis", "mrn": "mrn-hash-0001", "room": "412",
        "age_band": "65-74", "sex": "M", "race_code": "white",
        "language": "English", "los_days": 3,
        "series": ROBERT_DRIFT, "unit": 0,
        "note": "HERO CASE: NEWS only 4, but drifting 6h. Tap worry here.",
    },
    {
        "name": "Yolanda Price", "mrn": "mrn-hash-0002", "room": "408",
        "age_band": "45-54", "sex": "F", "race_code": "black",
        "language": "English", "los_days": 1,
        "series": STABLE, "unit": 0,
        "note": "STABLE: worry here produces a flag, no page.",
    },
    {
        "name": "Henry Okafor", "mrn": "mrn-hash-0003", "room": "415",
        "age_band": "75-84", "sex": "M", "race_code": "black",
        "language": "English", "los_days": 5,
        "series": HENRY_CRASH, "unit": 0,
        "note": "NEWS HIGH: escalates with no worry tap at all.",
    },
    {
        "name": "Mei-Ling Chen", "mrn": "mrn-hash-0004", "room": "403",
        "age_band": "55-64", "sex": "F", "race_code": "asian",
        "language": "Mandarin", "los_days": 2,
        "series": MILD, "unit": 0,
    },
    {
        "name": "Carlos Represa", "mrn": "mrn-hash-0005", "room": "410",
        "age_band": "35-44", "sex": "M", "race_code": "hispanic",
        "language": "Spanish", "los_days": 1,
        "series": STABLE, "unit": 0,
    },
    {
        "name": "Dorothy Whitfield", "mrn": "mrn-hash-0006", "room": "601",
        "age_band": "85+", "sex": "F", "race_code": "white",
        "language": "English", "los_days": 7,
        "series": MILD, "unit": 1,
    },
    {
        "name": "Aisha Rahman", "mrn": "mrn-hash-0007", "room": "604",
        "age_band": "25-34", "sex": "F", "race_code": "asian",
        "language": "Urdu", "los_days": 1,
        "series": STABLE, "unit": 1,
    },
    {
        "name": "Frank Delacroix", "mrn": "mrn-hash-0008", "room": "607",
        "age_band": "65-74", "sex": "M", "race_code": "white",
        "language": "English", "los_days": 4,
        "series": MILD, "unit": 1,
    },
]


# ---------------------------------------------------------------------------
# history: makes the metrics screens non-empty
# ---------------------------------------------------------------------------

REASONS = [
    "breathing_off",
    "not_himself",
    "color",
    "pain",
    "agitation",
    "output",
    "just_a_feeling",
]


def seed_history(
    org_id: str, patient_ids: list[str], nurse_ids: list[str], charge_id: str
) -> None:
    """Fabricate two weeks of resolved escalations with response times.

    The response-time spread is intentionally uneven across patient language,
    so the equity audit surfaces a real disparity rather than a flat screen.
    A demo that shows the audit finding nothing teaches the judges nothing.
    """
    client = supabase()
    worry_rows, esc_rows, outcome_rows = [], [], []

    patient_lang = {
        p["id"]: p["primary_language"]
        for p in (
            client.table("patients")
            .select("id, primary_language")
            .eq("org_id", org_id)
            .execute()
        ).data
        or []
    }

    for day in range(1, 15):
        for _ in range(random.randint(1, 3)):
            pid = random.choice(patient_ids)
            nurse = random.choice(nurse_ids)
            level = random.choice([1, 2, 2])
            triggered = now() - timedelta(days=day, hours=random.randint(0, 20))

            worry_rows.append(
                {
                    "org_id": org_id,
                    "patient_id": pid,
                    "nurse_id": nurse,
                    "level": level,
                    "reasons": random.sample(REASONS, random.randint(1, 2)),
                    "note": None,
                    "capture_ms": random.randint(1400, 4200),
                    "created_at": iso(triggered),
                }
            )

            # Non-English speakers wait longer. This is the disparity the
            # equity screen is built to catch.
            lang = patient_lang.get(pid, "English")
            base_ack = 6 if lang == "English" else 13
            ack_minutes = max(1, int(random.gauss(base_ack, 5)))

            breached = ack_minutes > 20
            status = "auto_escalated" if breached else "resolved"
            ack_at = triggered + timedelta(minutes=ack_minutes)
            assess_at = ack_at + timedelta(minutes=random.randint(2, 12))

            esc_rows.append(
                {
                    "org_id": org_id,
                    "patient_id": pid,
                    "worry_signal_id": None,  # linked after insert
                    "rule": random.choice(
                        ["very_worried_plus_drift", "very_worried_plus_news",
                         "worried_plus_drift_and_news", "news_high"]
                    ),
                    "status": status,
                    "triggered_at": iso(triggered),
                    "sla_deadline": iso(triggered + timedelta(minutes=20)),
                    "acknowledged_by": charge_id,
                    "acknowledged_at": iso(ack_at),
                    "assessed_by": charge_id,
                    "assessed_at": iso(assess_at),
                    "resolved_at": iso(assess_at + timedelta(minutes=20)),
                }
            )

            # ~22% of escalated worries preceded a real event, which is what
            # makes the reason-performance view interpretable.
            if random.random() < 0.22:
                outcome_rows.append(
                    {
                        "org_id": org_id,
                        "patient_id": pid,
                        "event_type": random.choice(
                            ["icu_transfer", "rrt_call", "rrt_call", "code_blue"]
                        ),
                        "occurred_at": iso(triggered + timedelta(hours=random.randint(2, 20))),
                    }
                )

    inserted_worry = client.table("worry_signals").insert(worry_rows).execute().data
    # Link each historical escalation to its worry signal so the
    # reason-performance view has something to explode.
    for esc, w in zip(esc_rows, inserted_worry):
        esc["worry_signal_id"] = w["id"]

    client.table("escalations").insert(esc_rows).execute()
    if outcome_rows:
        client.table("outcomes").insert(outcome_rows).execute()

    print(
        f"  history: {len(worry_rows)} worry signals, "
        f"{len(esc_rows)} escalations, {len(outcome_rows)} outcomes"
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    Config.validate()
    print(f"\nSeeding {Config.SUPABASE_URL}\n")

    print("Clearing previous demo data...")
    wipe_demo_org()

    print("\nCreating organization...")
    org_id = create_org()
    units = create_units(org_id)
    unit_ids = list(units.values())

    print("\nCreating users...")
    admin_id = create_user(
        org_id, "admin@mercy.test", "Dana Whitmore", "admin", None
    )
    charge_id = create_user(
        org_id, "charge@mercy.test", "Priya Raghunathan", "charge_nurse", unit_ids[0]
    )
    nurse1_id = create_user(
        org_id, "maria@mercy.test", "Maria Santos", "nurse", unit_ids[0]
    )
    nurse2_id = create_user(
        org_id, "james@mercy.test", "James Okonkwo", "nurse", unit_ids[0]
    )
    nurse3_id = create_user(
        org_id, "lin@mercy.test", "Lin Zhao", "nurse", unit_ids[1]
    )

    print("\nCreating patients and vitals...")
    patient_ids: list[str] = []
    assignments = []
    for spec in PATIENTS:
        unit_id = unit_ids[spec["unit"]]
        pid = create_patient(org_id, unit_id, spec)
        patient_ids.append(pid)
        insert_vitals_series(org_id, pid, spec["series"])

        nurse = nurse3_id if spec["unit"] == 1 else (
            nurse1_id if len(patient_ids) % 2 else nurse2_id
        )
        assignments.append(
            {"org_id": org_id, "nurse_id": nurse, "patient_id": pid}
        )

        tag = f"  <- {spec['note']}" if spec.get("note") else ""
        print(f"  patient: {spec['name']:22} rm {spec['room']}{tag}")

    supabase().table("assignments").insert(assignments).execute()
    print(f"\n  {len(assignments)} active shift assignments")

    print("\nSeeding two weeks of history for the metrics screens...")
    seed_history(org_id, patient_ids, [nurse1_id, nurse2_id, nurse3_id], charge_id)

    print(
        f"""
Done.

Logins (password: {DEMO_PASSWORD})
  admin@mercy.test    admin         -- org settings, equity audit
  charge@mercy.test   charge_nurse  -- the dashboard
  maria@mercy.test    nurse         -- the mobile app

Demo script:
  1. Log into the nurse app as maria@mercy.test
  2. Open Robert Ellis (rm 412). NEWS is 4 -- no existing system fires.
  3. Tap "Very worried" + "breathing off".
  4. Watch the dashboard escalate him live, with Maria's reason attached.
  5. Open Yolanda Price and tap worry too -- flagged, no page. That is the
     precision story.
  6. Leave the escalation un-acknowledged for 20 min (or lower the org SLA)
     to show auto-escalation closing the loop.
"""
    )


if __name__ == "__main__":
    main()
