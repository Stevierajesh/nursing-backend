# Patient simulation

Patients added through **Nurse app → Patients → New patient** automatically start
continuous stable monitoring. Admission creates the running simulation and its first
observation in the same transaction. Normal baselines are HR 78 bpm, RR 16/min,
SpO₂ 98%, BP 124/78 mmHg, and temperature 36.8°C (NEWS 0). The existing scheduler
supplies small normal variations every 15 seconds, including after the initial
10-minute scenario duration. Values remain labeled as simulated. Nurses are also
assigned their newly admitted patients. Dashboard-created simulations retain their
explicit Configure/Start controls.

Admins and charge nurses configure simulations in **Dashboard → Admin → Patient simulations**.
Create a patient, choose a unit and optional vacant bed / nurse assignment, enter health context,
and select an authored scenario. Existing patients can also receive a simulation profile.
Profiles are saved in a ready state; **Start** begins generation.

The initial module emits HR, respiratory rate, SpO₂, systolic/diastolic BP, temperature,
and NEWS. Scenarios are stable, respiratory decline, circulatory decline, and recovery.
Baseline means starting values for decline/stable scenarios and recovery target values.
Health context is descriptive; this is not a physiological model of arbitrary diagnoses.
Baselines can also come from a versioned empirical model fitted to the Human Vital Sign Dataset.
Choose a reference age group and recorded gender group, then **Sample from dataset** in the
admin form. This learns baseline combinations only; there is no trained deterioration
classifier, lab/medication generation, dataset replay, or generated nurse worry.
See [CALIBRATION.md](CALIBRATION.md) for the fitting method, source audit, and reproduction command.

## Enable on the existing database

Apply **only** the new migration. Do not reseed or replay the older migrations on the hosted project.
Set `DATABASE_URL` in the backend's gitignored `.env`, then from `nursing-backend` run:

```bash
.venv/bin/python scripts/apply_migrations.py --only 20261024001100_patient_simulation.sql
```

Alternatively, run `supabase/migrations/20261024001100_patient_simulation.sql` once in the
Supabase SQL editor. The API service key cannot execute DDL. Without the migration,
the simulation panel reports a specific setup error and the worker logs a throttled warning.
Existing patient APIs remain available.

The backend scheduler must be enabled (`ENABLE_SCHEDULER=true`). Development reload or a
backend restart loads the added scheduler job. The worker checks every 5 seconds; each
running patient produces an observation approximately every 15 seconds. Each saved observation
immediately invokes the existing risk/escalation evaluation. The existing periodic risk
job recovers from transient evaluation failures. Browser views poll every 15 seconds;
the admin simulator polls every 5 seconds. This is polling, not a bedside-device feed.

## Playback and history

Duration is 2–240 **real minutes**; there is no accelerated virtual clock. Onset is the number
of real elapsed minutes before a decline starts. The preview samples the exact deterministic
trajectory used by the worker. The variation seed changes smooth bounded fluctuations.

Pause stops observations and freezes trajectory progress, while real clinical response timers
and worry decay continue. Resume continues from the saved progress. Stop ends the run.
Restart creates a new run with the same configuration. Earlier observations, worry signals,
risk records, and escalation history remain intact and still contribute to the existing
patient-level risk calculation until they age out. Restart is not a clean patient reset;
create a new patient for an isolated demonstration. Acknowledgement and assessment do not
change physiology. Recovery must be selected explicitly. Completion emits the final state
once; discharge stops generation.

Synthetic patients are marked `is_simulated`; every emitted observation stores
`source=simulation`, its run ID, and elapsed seconds. The nurse patient list includes current
values; the patient worry view shows all six vitals, baseline, timestamp, stale-observation
notice, and recent history. Scenario settings are visible to charge/admin users only.

Patient creation, initial run configuration, and optional nurse assignment occur in one
transaction. Database row locks serialize pause/restart and observation insertion; revisions
reject snapshots from old workers. RPCs are service-role only. Organization filtering and RLS
protect configuration. Runs and profiles survive backend restarts; downtime advances the
scenario by real elapsed time without backfilling fabricated measurements.

## API

All simulation management and preview routes require admin or charge nurse.

- `GET /api/v1/simulations`: latest run per patient in the caller's organization.
- `POST /api/v1/simulations/preview`: configuration → nine trajectory sample points.
- `GET /api/v1/simulations/calibration`: model version, source, groups, and data audit.
- `POST /api/v1/simulations/calibration/sample`: seed + optional reference age/sex → baseline, provenance, derived pressure values.
- `POST /api/v1/simulations/:patient_id/configure`: save a new ready run (stop an existing active run first).
- `POST /api/v1/simulations/:patient_id/control`: `{ "action": "start|pause|resume|restart|stop" }`.
- `POST /api/v1/admin/patients`: existing patient fields plus optional `simulation` config
  and `nurse_id`; the latter is supported with simulation creation.

Configuration shape:

```json
{
  "scenario": "respiratory_decline",
  "health_context": "Postoperative patient with history of hypertension",
  "baseline": {
    "heart_rate": 78,
    "resp_rate": 16,
    "spo2": 98,
    "systolic_bp": 124,
    "diastolic_bp": 78,
    "temperature_c": 36.8
  },
  "duration_minutes": 10,
  "onset_minutes": 1,
  "seed": 42
}
```

## Verification

`make test` covers trajectories, validation, signal independence, worker behavior, and API permissions.
`make test-rls` applies real migrations to disposable Postgres and checks lifecycle transitions,
stale/duplicate worker snapshots, discharge, completion, atomic creation, provenance, and RPC/RLS permissions.
