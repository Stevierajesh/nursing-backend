# Nurse Worry Layer — API

Flask resource server for the Nurse Worry Layer. Supabase provides identity and
storage; this service verifies tokens, enforces org scoping, runs the risk
fusion, and owns the escalation lifecycle.

## The one design claim worth knowing

**Worry alone never raises an alarm.** A worried nurse with a stable patient
produces a visible flag on the charge nurse's board and nothing more. An
escalation fires only when worry coincides with an objective signal — an
elevated NEWS, or a NEWS that is drifting upward. See `app/services/risk.py`,
and `tests/test_risk.py` which pins that behaviour.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env     # fill in SUPABASE_URL and SUPABASE_SERVICE_KEY
```

> The hosted database is **already migrated and seeded**. The commands below
> are for a fresh project only — `seed.py` deletes and rebuilds the demo org.

Apply the schema:

```bash
# preferred — Project Settings → Database → Connection string → URI
export DATABASE_URL='postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres'
.venv/bin/python scripts/apply_migrations.py

# or emit SQL to paste into the dashboard SQL editor
.venv/bin/python scripts/apply_migrations.py --print
```

Seed the demo hospital:

```bash
.venv/bin/python scripts/seed.py
```

Run:

```bash
make dev         # foreground, http://localhost:5001
make start       # background
make stop
make restart
make help        # all targets
```

Tests:

```bash
make test        # 15 unit tests, no network needed
make test-rls    # RLS policy attacks on a throwaway Postgres 16
make test-all    # both
```

Confirm it is up before starting either frontend:

```bash
make health
```

`status: ok` with `database.reachable: true` means the service key works.
`jwt_scheme` tells you which verification path is live — this project reports
`asymmetric (JWKS)`, which is why `SUPABASE_JWT_SECRET` is correctly blank.

### Demo accounts

Password for all five: `WorryLayer!2026`

| Email | Role |
|---|---|
| `admin@mercygeneral.example` | admin |
| `charge@mercygeneral.example` | charge_nurse |
| `maria@mercygeneral.example` | nurse |
| `james@mercygeneral.example` | nurse |
| `lin@mercygeneral.example` | nurse |

Call the API as any of them:

```bash
make token ACCOUNT=maria     # print a raw access token
make me ACCOUNT=charge       # GET /api/v1/me as that user

# the token composes with curl for any endpoint
curl -s localhost:5001/api/v1/dashboard/patients \
  -H "Authorization: Bearer $(make token ACCOUNT=charge)"
```

`ACCOUNT` defaults to `charge`. It is deliberately not called `USER` — make
inherits `USER` from the shell, so `make token` would silently try to
authenticate as your login name.

## Auth

The frontends sign in against Supabase directly and send the resulting access
token as `Authorization: Bearer <jwt>`. This service never issues tokens.

1. `app/auth/jwt.py` verifies the signature. Asymmetric projects are verified
   against the JWKS endpoint; legacy HS256 projects against the shared secret.
   The scheme is chosen from the token's own `alg` header, so a project that
   rotates keys keeps working. Anything unrecognised — notably `none` — is
   refused.
2. `app/auth/middleware.py` resolves `org_id` and `role` from the `profiles`
   table and installs them on `g.user`, cached for 60s.

Decorators compose, outermost first:

```python
@bp.post("/something")
@require_auth                               # verifies the token, sets g.user
@require_role("charge_nurse", "admin")      # 403 otherwise
def handler(): ...
```

`require_role` validates its role names at import time, so a typo fails on
startup rather than silently denying every request.

### All writes go through this API

`authenticated` holds **SELECT only** on every table (migration
`…_lock_down_writes.sql`). Every mutation goes through Flask with the
service-role key.

That is not stylistic. Testing the original policies against a real Postgres
found two privilege-escalation holes, both from the same root cause — RLS
restricts which *rows* a caller may touch but says nothing about which
*columns*, and the frontends hold a working anon key:

1. **Privilege escalation.** A nurse could run
   `update profiles set role='admin' where id = <own id>` from devtools. The
   self-update policy permitted the row; nothing constrained the column.
2. **Metric forgery.** A charge nurse could write `escalations.acknowledged_at`
   directly. Since worry-to-response time is the number this product sells, a
   client-writable response timestamp makes it unfalsifiable.

Both are fixed and regression-tested. The one remaining client-side write is a
column-level grant on `profiles(full_name)` so a user can fix the spelling of
their own name, backstopped by a trigger that rejects `role`/`org_id` changes
from anything other than the service role.

```bash
./tests/sql/run.sh    # throwaway Postgres 16, applies migrations, runs attacks
```

### Org isolation is enforced twice

This service uses the **service-role key, which bypasses RLS**. Org scoping on
the API path is therefore enforced in application code — `scoped()`,
`assert_patient_in_org()`, and an explicit `.eq("org_id", …)` on every query.

RLS policies still exist (`supabase/migrations/…_rls.sql`) because the dashboard
subscribes to `escalations` and `worry_signals` over Supabase Realtime directly
from the browser, and that path *is* policy-governed.

`assert_patient_in_org` returns 404 for both "does not exist" and "belongs to
another org", so the API cannot be used to probe for patient IDs elsewhere.

## Roles

| Role | Sees | Can |
|---|---|---|
| `nurse` | only patients they are assigned to on an open shift | raise worry, chart vitals |
| `charge_nurse` | the whole org | acknowledge / assess / resolve escalations, manage patients and assignments |
| `admin` | the whole org | everything, plus roster, roles, and the escalation SLA |

## API

| Method | Path | Role |
|---|---|---|
| GET | `/api/v1/health` | public |
| GET | `/api/v1/me` | any |
| POST | `/api/v1/worry` | nurse+ |
| GET | `/api/v1/worry/patient/:id` | any |
| GET | `/api/v1/patients/mine` | any |
| GET | `/api/v1/patients/:id` | any |
| POST | `/api/v1/patients/:id/vitals` | nurse+ |
| GET | `/api/v1/dashboard/patients` | charge+ |
| GET | `/api/v1/dashboard/escalations` | charge+ |
| GET | `/api/v1/dashboard/summary` | charge+ |
| POST | `/api/v1/escalations/:id/acknowledge` | charge+ |
| POST | `/api/v1/escalations/:id/assess` | charge+ |
| POST | `/api/v1/escalations/:id/resolve` | charge+ |
| GET | `/api/v1/metrics/worry-to-response` | charge+ |
| GET | `/api/v1/metrics/equity` | charge+ |
| GET | `/api/v1/metrics/worry-reasons` | charge+ |
| GET/PATCH | `/api/v1/admin/org` | charge+ / admin |
| GET/POST | `/api/v1/admin/units` | any / admin |
| GET/POST/PATCH | `/api/v1/admin/users` | charge+ / admin |
| POST | `/api/v1/admin/patients` | charge+ |
| GET/POST/DELETE | `/api/v1/admin/assignments` | charge+ |
| GET | `/api/v1/admin/audit` | admin |

`GET /api/v1/health` reports which JWT scheme the project uses, which is the
first thing to check when auth fails mysteriously.

## Risk fusion

`compute_risk(vitals, worry)` returns a 0–100 score from three weighted
components, plus an escalation verdict:

| Component | Weight | Source |
|---|---|---|
| vitals | 0.45 | current NEWS, normalised against a ceiling of 12 |
| trend | 0.30 | least-squares NEWS slope over 8h, upward movement only |
| worry | 0.25 | peak worry level, decayed on a 4h half-life |

Escalation rules, evaluated most-specific first; the matched rule name is stored
on the escalation so the board can always say why someone was paged:

| Rule | Condition |
|---|---|
| `news_high` | NEWS ≥ 7 — fires with no worry at all |
| `very_worried_plus_drift` | worry = 2 and slope ≥ 0.25/hr |
| `very_worried_plus_news` | worry = 2 and NEWS ≥ 5 |
| `worried_plus_drift_and_news` | worry = 1 and slope ≥ 0.25/hr and NEWS ≥ 5 |
| *(none)* | worry with stable vitals — flagged, not escalated |

Level-1 worry has the strictest bar because a mild hunch on a mildly abnormal
patient is the single largest false-alarm source.

Weights and thresholds are hand-set priors, not learned. They are module
constants in `risk.py` so they can be tuned from `outcomes` once there is
enough data, and so every number on the demo screen is explainable.

## Background jobs

APScheduler, in-process, guarded by a lock file so only one gunicorn worker owns
the timers.

- `sla_sweep` (30s) — flips escalations past `sla_deadline` with no response to
  `auto_escalated`. This is the closed loop.
- `risk_recompute` (60s) — refreshes fused risk for active patients, so new
  vitals can escalate a previously-flagged worry without anyone tapping again.

Set `ENABLE_SCHEDULER=false` to run the API without them.

## Known gaps

- Escalations notify by appearing on the board. No SMS/pager integration.
- `voice_url` is stored but nothing transcribes it.
- The equity audit covers *response* equity. It does not yet audit *capture*
  equity — whether nurses raise worry at different rates for different patient
  groups — which needs a denominator of patient-days per group.
- Risk weights are priors, not fitted to outcomes.
