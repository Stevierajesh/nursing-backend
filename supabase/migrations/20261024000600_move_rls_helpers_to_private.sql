-- Move the RLS helpers out of `public` into a `private` schema.
--
-- Putting them in `public` (forced by hosted Supabase denying CREATE FUNCTION
-- in `auth`) had a side effect Supabase's database linter caught: PostgREST
-- serves every function in `public`, so each helper became a callable REST
-- endpoint at /rest/v1/rpc/<name> for both anon and authenticated.
--
-- `is_assigned(uuid)` was the most useful of those to an attacker -- a probe
-- oracle over patient UUIDs -- and none of them were ever meant to be public
-- API.
--
-- PostgREST only exposes schemas in its db-schemas setting (public and
-- graphql_public by default), so `private` is unreachable over REST while
-- remaining usable inside policy expressions.
--
-- EXECUTE must still be granted to `authenticated`: RLS policy expressions are
-- evaluated as the querying role, not the table owner, so revoking EXECUTE
-- outright would break every SELECT policy below. `anon` gets nothing -- it
-- holds no SELECT on these tables, so its policies never evaluate.

create schema if not exists private;

revoke all on schema private from public, anon, authenticated;
grant usage on schema private to authenticated;

-- ---------------------------------------------------------------------------
-- helpers, relocated verbatim
-- ---------------------------------------------------------------------------

create or replace function private.current_org_id()
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select org_id from public.profiles where id = auth.uid()
$$;

create or replace function private.current_app_role()
returns public.app_role
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select role from public.profiles where id = auth.uid()
$$;

create or replace function private.is_org_wide()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select coalesce(
    (select role in ('admin', 'charge_nurse') from public.profiles where id = auth.uid()),
    false
  )
$$;

create or replace function private.is_assigned(target_patient uuid)
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1 from public.assignments a
    where a.patient_id = target_patient
      and a.nurse_id = auth.uid()
      and a.shift_end is null
  )
$$;

create or replace function private.guard_profile_privileges()
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

revoke all on function
  private.current_org_id(),
  private.current_app_role(),
  private.is_org_wide(),
  private.is_assigned(uuid),
  private.guard_profile_privileges()
  from public, anon, authenticated;

grant execute on function
  private.current_org_id(),
  private.current_app_role(),
  private.is_org_wide(),
  private.is_assigned(uuid)
  to authenticated;

-- ---------------------------------------------------------------------------
-- repoint the trigger
-- ---------------------------------------------------------------------------

drop trigger if exists guard_profile_privileges_trg on profiles;
create trigger guard_profile_privileges_trg
  before update on profiles
  for each row
  execute function private.guard_profile_privileges();

-- ---------------------------------------------------------------------------
-- recreate every policy against private.*
--
-- Policies bind the function by OID, so they cannot be repointed in place.
-- ---------------------------------------------------------------------------

drop policy if exists org_read_own          on organizations;
drop policy if exists units_read_own_org    on units;
drop policy if exists profiles_read_own_org on profiles;
drop policy if exists profiles_update_self  on profiles;
drop policy if exists patients_read         on patients;
drop policy if exists assignments_read      on assignments;
drop policy if exists vitals_read           on vitals;
drop policy if exists risk_scores_read      on risk_scores;
drop policy if exists worry_read            on worry_signals;
drop policy if exists escalations_read      on escalations;
drop policy if exists outcomes_read         on outcomes;
drop policy if exists audit_admin_read      on audit_log;

create policy org_read_own on organizations
  for select to authenticated
  using (id = private.current_org_id());

create policy units_read_own_org on units
  for select to authenticated
  using (org_id = private.current_org_id());

create policy profiles_read_own_org on profiles
  for select to authenticated
  using (org_id = private.current_org_id());

create policy profiles_update_self on profiles
  for update to authenticated
  using (id = auth.uid())
  with check (id = auth.uid() and org_id = private.current_org_id());

create policy patients_read on patients
  for select to authenticated
  using (
    org_id = private.current_org_id()
    and (private.is_org_wide() or private.is_assigned(id))
  );

create policy assignments_read on assignments
  for select to authenticated
  using (
    org_id = private.current_org_id()
    and (private.is_org_wide() or nurse_id = auth.uid())
  );

create policy vitals_read on vitals
  for select to authenticated
  using (
    org_id = private.current_org_id()
    and (private.is_org_wide() or private.is_assigned(patient_id))
  );

create policy risk_scores_read on risk_scores
  for select to authenticated
  using (
    org_id = private.current_org_id()
    and (private.is_org_wide() or private.is_assigned(patient_id))
  );

create policy worry_read on worry_signals
  for select to authenticated
  using (
    org_id = private.current_org_id()
    and (private.is_org_wide() or nurse_id = auth.uid())
  );

create policy escalations_read on escalations
  for select to authenticated
  using (
    org_id = private.current_org_id()
    and (private.is_org_wide() or private.is_assigned(patient_id))
  );

create policy outcomes_read on outcomes
  for select to authenticated
  using (org_id = private.current_org_id() and private.is_org_wide());

create policy audit_admin_read on audit_log
  for select to authenticated
  using (org_id = private.current_org_id() and private.current_app_role() = 'admin');

-- ---------------------------------------------------------------------------
-- drop the public copies now nothing references them
-- ---------------------------------------------------------------------------

drop function if exists public.current_org_id();
drop function if exists public.current_app_role();
drop function if exists public.is_org_wide();
drop function if exists public.is_assigned(uuid);
drop function if exists public.guard_profile_privileges();
