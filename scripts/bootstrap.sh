#!/usr/bin/env bash
#
# Apply migrations and seed the demo hospital, in one command.
#
# Reads credentials from .env — secrets are never echoed.
#
#   ./scripts/bootstrap.sh
#
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$HERE"

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

if [ ! -f .env ]; then
  echo "No .env found. Copy .env.example to .env and fill in the keys." >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

missing=()
[ -n "${SUPABASE_URL:-}" ]         || missing+=("SUPABASE_URL")
[ -n "${SUPABASE_SERVICE_KEY:-}" ] || missing+=("SUPABASE_SERVICE_KEY")

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing in .env: ${missing[*]}" >&2
  echo "Get them from: Supabase Dashboard > Project Settings > API Keys" >&2
  exit 1
fi

echo "== 1/3  Applying migrations"
if [ -n "${DATABASE_URL:-}" ]; then
  $PY scripts/apply_migrations.py
else
  cat >&2 <<'MSG'
DATABASE_URL is not set, so migrations cannot be applied automatically.

Either add it to .env:
  Dashboard > Project Settings > Database > Connection string > URI
  (use the pooler string on port 5432, and URL-encode any special
   characters in the password)

  DATABASE_URL='postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres'

…or apply them by hand, which needs no connection string:
  .venv/bin/python scripts/apply_migrations.py --print
  then paste the output into Dashboard > SQL Editor > New query

Re-run this script once the schema is in place; it will skip to seeding.
MSG
  exit 1
fi

echo
echo "== 2/3  Seeding demo hospital"
$PY scripts/seed.py

echo
echo "== 3/3  Verifying"
$PY - <<'PY'
from app.extensions import supabase

client = supabase()
checks = [
    ("organizations", 1),
    ("units", 2),
    ("profiles", 5),
    ("patients", 8),
    ("vitals", 20),
    ("worry_signals", 10),
    ("escalations", 10),
]

ok = True
for table, expect_at_least in checks:
    n = (
        client.table(table).select("id", count="exact").execute().count or 0
    )
    good = n >= expect_at_least
    ok = ok and good
    print(f"  {'OK ' if good else 'LOW'}  {table:<16} {n:>4} rows (expected >= {expect_at_least})")

# The hero case must be escalatable: NEWS below the response band but drifting.
rows = (
    client.table("patients")
    .select("id, display_name")
    .eq("display_name", "Robert Ellis")
    .execute()
    .data
) or []
if rows:
    pid = rows[0]["id"]
    v = (
        client.table("vitals")
        .select("news_score, recorded_at")
        .eq("patient_id", pid)
        .order("recorded_at", desc=True)
        .limit(1)
        .execute()
        .data
    )
    latest = v[0]["news_score"] if v else None
    print(f"\n  Hero case 'Robert Ellis': latest NEWS = {latest}")
    if latest is not None and latest >= 7:
        print("  ! NEWS is already in the high band — he'd escalate without a worry tap.")
        print("    The demo is more persuasive when the tap is what triggers it.")
else:
    ok = False
    print("\n  ! Robert Ellis not found — seeding may have failed")

print("\n" + ("Bootstrap complete." if ok else "Bootstrap finished with warnings above."))
PY
