"""Apply the SQL migrations to Supabase.

Two paths, because the Supabase MCP server may not be approved yet:

  1. DATABASE_URL set -> connect with psycopg and run each migration in order.
     This is the reliable path and the one to prefer.
  2. Otherwise -> print the combined SQL so it can be pasted into the Supabase
     dashboard SQL editor.

Migrations are applied in filename order and tracked in a `_migrations` table,
so re-running only applies what is new.

    # preferred
    export DATABASE_URL='postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres'
    python scripts/apply_migrations.py

    # fallback: emit SQL to paste into the dashboard
    python scripts/apply_migrations.py --print
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "supabase" / "migrations"

TRACKING_TABLE = """
create table if not exists _migrations (
  name       text primary key,
  applied_at timestamptz not null default now()
);
"""


def migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        sys.exit(f"No .sql files found in {MIGRATIONS_DIR}")
    return files


def print_combined() -> None:
    files = migration_files()
    print("-- Nurse Worry Layer: combined migrations")
    print("-- Paste into Supabase dashboard -> SQL Editor -> New query\n")
    for f in files:
        print(f"\n-- {'=' * 70}")
        print(f"-- {f.name}")
        print(f"-- {'=' * 70}\n")
        print(f.read_text())


def apply_with_psycopg(database_url: str) -> None:
    try:
        import psycopg
    except ImportError:
        sys.exit(
            "psycopg is not installed. Either:\n"
            "  pip install 'psycopg[binary]'\n"
            "or run with --print and paste the SQL into the dashboard."
        )

    files = migration_files()

    with psycopg.connect(database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(TRACKING_TABLE)
            conn.commit()

            cur.execute("select name from _migrations")
            applied = {row[0] for row in cur.fetchall()}

        pending = [f for f in files if f.name not in applied]
        if not pending:
            print(f"Up to date — all {len(files)} migration(s) already applied.")
            return

        for f in pending:
            print(f"Applying {f.name} …", end=" ", flush=True)
            try:
                with conn.cursor() as cur:
                    # Each migration is one transaction: a failure half-way
                    # through must not leave the schema in a partial state.
                    cur.execute(f.read_text())
                    cur.execute(
                        "insert into _migrations (name) values (%s)", (f.name,)
                    )
                conn.commit()
                print("ok")
            except Exception as exc:
                conn.rollback()
                print("FAILED")
                sys.exit(f"\n{f.name} failed and was rolled back:\n\n{exc}")

        print(f"\nApplied {len(pending)} migration(s).")


def main() -> None:
    if "--print" in sys.argv:
        print_combined()
        return

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit(
            "DATABASE_URL is not set.\n\n"
            "Get it from: Supabase dashboard -> Project Settings -> Database ->\n"
            "Connection string -> URI (use the pooler string, port 5432).\n\n"
            "  export DATABASE_URL='postgresql://...'\n"
            "  python scripts/apply_migrations.py\n\n"
            "Or emit the SQL to paste into the dashboard instead:\n"
            "  python scripts/apply_migrations.py --print"
        )

    apply_with_psycopg(database_url)


if __name__ == "__main__":
    main()
