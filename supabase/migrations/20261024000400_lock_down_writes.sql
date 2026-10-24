-- Close two privilege-escalation holes found by testing the policies in
-- 20261024000200_rls.sql against a real Postgres.
--
-- Both stem from the same root cause: Supabase grants the `authenticated` role
-- full DML on the public schema by default, and the frontends hold a working
-- anon key. RLS restricts WHICH ROWS a caller may touch, but says nothing
-- about WHICH COLUMNS. So any logged-in user could open devtools and write
-- directly to Postgres, bypassing the API entirely.
--
-- Hole 1 — privilege escalation. `profiles_update_self` allowed a caller to
--   update their own profile row. Nothing stopped them setting role='admin'.
--   Verified: a nurse promoted themselves successfully.
--
-- Hole 2 — metric forgery. `escalations_charge_update` allowed a charge nurse
--   to update escalation rows directly, including `acknowledged_at`. Since
--   worry-to-response time is the number this product is selling, a
--   client-writable response timestamp makes it unfalsifiable.
--
-- Fix: `authenticated` gets SELECT only. Every mutation goes through the Flask
-- API, which holds the service-role key, computes the risk fusion, and stamps
-- all lifecycle timestamps server-side. That was already the only code path
-- the frontends used -- this makes it the only path that *works*.

-- ---------------------------------------------------------------------------
-- revoke all client-side writes
-- ---------------------------------------------------------------------------

revoke insert, update, delete
  on all tables in schema public
  from authenticated, anon;

-- Supabase's default privileges would re-grant these on any table added later.
alter default privileges in schema public
  revoke insert, update, delete on tables from authenticated;
alter default privileges in schema public
  revoke insert, update, delete on tables from anon;

-- Reads stay, because the dashboard subscribes to escalations and
-- worry_signals over Realtime and that path is governed by the RLS SELECT
-- policies.
grant select on all tables in schema public to authenticated;

-- `anon` is pre-login only and has no business reading clinical data.
revoke select on all tables in schema public from anon;

-- ---------------------------------------------------------------------------
-- defence in depth: allow self-service edits to safe columns only
--
-- A user may fix the spelling of their own name. Column-level grants are the
-- mechanism RLS lacks, and they compose with the row policy already in place.
-- role, org_id, and unit_id are deliberately excluded -- those are
-- administrative facts, changed only via the API.
-- ---------------------------------------------------------------------------

grant update (full_name) on profiles to authenticated;

-- ---------------------------------------------------------------------------
-- belt and braces: reject privilege changes that did not come from the API
--
-- The column grant above is the real control. This trigger is a second
-- independent guard, so that re-granting table-wide UPDATE by accident (or a
-- future migration doing it) cannot silently reopen hole 1.
--
-- The service-role key connects as `service_role` and bypasses RLS but not
-- triggers, so the check explicitly exempts it.
-- ---------------------------------------------------------------------------

create or replace function public.guard_profile_privileges()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if current_user in ('service_role', 'postgres', 'supabase_admin') then
    return new;
  end if;

  if new.role is distinct from old.role then
    raise exception
      'role may only be changed by an administrator through the API'
      using errcode = 'insufficient_privilege';
  end if;

  if new.org_id is distinct from old.org_id then
    raise exception
      'org_id may only be changed by an administrator through the API'
      using errcode = 'insufficient_privilege';
  end if;

  return new;
end $$;

drop trigger if exists guard_profile_privileges_trg on profiles;
create trigger guard_profile_privileges_trg
  before update on profiles
  for each row
  execute function public.guard_profile_privileges();

-- ---------------------------------------------------------------------------
-- drop the now-misleading write policies
--
-- With client writes revoked these can never fire. Leaving them in place would
-- imply direct client writes are an intended, governed path -- the next person
-- reading this schema would reasonably conclude a nurse can insert worry
-- straight into Postgres. They cannot, and should not.
-- ---------------------------------------------------------------------------

drop policy if exists org_admin_update       on organizations;
drop policy if exists units_admin_write      on units;
drop policy if exists profiles_admin_write   on profiles;
drop policy if exists patients_admin_write   on patients;
drop policy if exists assignments_charge_write on assignments;
drop policy if exists worry_nurse_insert     on worry_signals;
drop policy if exists escalations_charge_update on escalations;

comment on table escalations is
  'Lifecycle timestamps (acknowledged_at, assessed_at) are written only by the '
  'API via the service-role key. Client-side UPDATE is revoked: a '
  'client-writable response timestamp would make worry-to-response time '
  'unfalsifiable.';

comment on table worry_signals is
  'Append-only. Retracting a concern is a new signal at level 0, never an '
  'update, so the audit trail stays honest. Inserts go through POST '
  '/api/v1/worry, which computes the risk fusion and decides on escalation.';
