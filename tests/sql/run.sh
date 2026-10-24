#!/usr/bin/env bash
#
# Run the RLS policy tests against a throwaway local Postgres.
#
# These exist because RLS cannot be tested by reading it. Two real
# privilege-escalation holes were found this way and fixed in
# migration 20261024000400_lock_down_writes.sql:
#
#   1. a nurse could set their own role to 'admin'
#   2. a charge nurse could forge escalations.acknowledged_at, which would
#      make worry-to-response time -- the product's headline metric --
#      unfalsifiable
#
# Requires postgresql-16 binaries. Does not touch the Supabase project.
#
#   ./tests/sql/run.sh
#
set -euo pipefail

PGBIN=${PGBIN:-/usr/lib/postgresql/16/bin}
# Pick a free port rather than a fixed one, so a leftover instance from an
# interrupted run does not make this fail with an unhelpful pg_ctl error.
PORT=${PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')}
WORKDIR=$(mktemp -d)
PGDATA="$WORKDIR/pgdata"
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MIGRATIONS="$HERE/../../supabase/migrations"

cleanup() {
  "$PGBIN/pg_ctl" -D "$PGDATA" stop -m immediate >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

if [ ! -x "$PGBIN/initdb" ]; then
  echo "postgres binaries not found at $PGBIN — set PGBIN" >&2
  exit 1
fi

echo "Starting throwaway Postgres on 127.0.0.1:$PORT …"
"$PGBIN/initdb" -D "$PGDATA" -U postgres --auth=trust >/dev/null
# -k /tmp keeps the unix socket path under the 107-byte limit
"$PGBIN/pg_ctl" -D "$PGDATA" \
  -o "-p $PORT -k /tmp -c listen_addresses=127.0.0.1" \
  -l "$WORKDIR/pg.log" start >/dev/null
sleep 2

psql() { command psql -h 127.0.0.1 -p "$PORT" -U postgres "$@"; }

psql -q -c "create database worrytest;"
PSQL=(psql -q -d worrytest -v ON_ERROR_STOP=1)

echo "Installing Supabase shim …"
"${PSQL[@]}" -f "$HERE/00_supabase_shim.sql" 2>/dev/null

echo "Applying migrations …"
for f in "$MIGRATIONS"/*.sql; do
  printf '  %-48s' "$(basename "$f")"
  "${PSQL[@]}" -f "$f" >/dev/null && echo OK
done

echo "Loading fixtures (two orgs, so leakage is testable) …"
"${PSQL[@]}" -f "$HERE/01_fixtures.sql" >/dev/null

echo
echo "Running policy tests …"
command psql -h 127.0.0.1 -p "$PORT" -U postgres -d worrytest -q \
  -f "$HERE/02_rls_attacks.sql" 2>&1 \
  | grep -v '^SET$\|^SAVEPOINT$\|^ROLLBACK$\|^DO$\|^BEGIN$\|^RESET$' \
  | sed 's|^psql:.*02_rls_attacks.sql:[0-9]*: ||'

echo
if command psql -h 127.0.0.1 -p "$PORT" -U postgres -d worrytest -tA \
     -f "$HERE/02_rls_attacks.sql" 2>&1 | grep -q 'FAIL'; then
  echo "RESULT: at least one policy test FAILED"
  exit 1
fi
echo "RESULT: all policy tests passed"
