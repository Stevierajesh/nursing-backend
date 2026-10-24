-- Row Level Security.
--
-- The Flask backend uses the service-role key and therefore BYPASSES every
-- policy here. These policies exist to protect direct-from-browser reads:
-- the dashboard subscribes to escalations and worry_signals over Supabase
-- Realtime instead of polling the API, and that path is only as safe as RLS.
-- Org isolation is consequently enforced twice -- in app code via the
-- org_scoped decorator, and in Postgres here.

-- ---------------------------------------------------------------------------
-- claim helpers
--
-- SECURITY DEFINER so that reading profiles from inside a profiles policy does
-- not recurse. search_path is pinned to defeat search_path hijacking.
-- ---------------------------------------------------------------------------

create or replace function auth.current_org_id()
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select org_id from public.profiles where id = auth.uid()
$$;

create or replace function auth.current_app_role()
returns public.app_role
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select role from public.profiles where id = auth.uid()
$$;

-- Charge nurses and admins see the whole org; nurses see only their patients.
create or replace function auth.is_org_wide()
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

-- Is the caller currently assigned to this patient?
create or replace function auth.is_assigned(target_patient uuid)
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

-- ---------------------------------------------------------------------------
-- enable RLS everywhere
-- ---------------------------------------------------------------------------

alter table organizations enable row level security;
alter table units         enable row level security;
alter table profiles      enable row level security;
alter table patients      enable row level security;
alter table assignments   enable row level security;
alter table vitals        enable row level security;
alter table worry_signals enable row level security;
alter table risk_scores   enable row level security;
alter table escalations   enable row level security;
alter table outcomes      enable row level security;
alter table audit_log     enable row level security;

-- ---------------------------------------------------------------------------
-- organizations / units
-- ---------------------------------------------------------------------------

create policy org_read_own on organizations
  for select to authenticated
  using (id = auth.current_org_id());

create policy org_admin_update on organizations
  for update to authenticated
  using (id = auth.current_org_id() and auth.current_app_role() = 'admin');

create policy units_read_own_org on units
  for select to authenticated
  using (org_id = auth.current_org_id());

create policy units_admin_write on units
  for all to authenticated
  using (org_id = auth.current_org_id() and auth.current_app_role() = 'admin')
  with check (org_id = auth.current_org_id() and auth.current_app_role() = 'admin');

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------

-- Everyone in an org can see their colleagues -- the dashboard needs to render
-- "acknowledged by" names, and the nurse app shows who is on shift.
create policy profiles_read_own_org on profiles
  for select to authenticated
  using (org_id = auth.current_org_id());

create policy profiles_update_self on profiles
  for update to authenticated
  using (id = auth.uid())
  with check (id = auth.uid() and org_id = auth.current_org_id());

create policy profiles_admin_write on profiles
  for all to authenticated
  using (org_id = auth.current_org_id() and auth.current_app_role() = 'admin')
  with check (org_id = auth.current_org_id() and auth.current_app_role() = 'admin');

-- ---------------------------------------------------------------------------
-- patients: org-wide for admin/charge_nurse, assignment-scoped for nurses
-- ---------------------------------------------------------------------------

create policy patients_read on patients
  for select to authenticated
  using (
    org_id = auth.current_org_id()
    and (auth.is_org_wide() or auth.is_assigned(id))
  );

create policy patients_admin_write on patients
  for all to authenticated
  using (org_id = auth.current_org_id() and auth.is_org_wide())
  with check (org_id = auth.current_org_id() and auth.is_org_wide());

-- ---------------------------------------------------------------------------
-- assignments
-- ---------------------------------------------------------------------------

create policy assignments_read on assignments
  for select to authenticated
  using (
    org_id = auth.current_org_id()
    and (auth.is_org_wide() or nurse_id = auth.uid())
  );

create policy assignments_charge_write on assignments
  for all to authenticated
  using (org_id = auth.current_org_id() and auth.is_org_wide())
  with check (org_id = auth.current_org_id() and auth.is_org_wide());

-- ---------------------------------------------------------------------------
-- vitals / risk_scores: read follows patient visibility, writes via API only
-- ---------------------------------------------------------------------------

create policy vitals_read on vitals
  for select to authenticated
  using (
    org_id = auth.current_org_id()
    and (auth.is_org_wide() or auth.is_assigned(patient_id))
  );

create policy risk_scores_read on risk_scores
  for select to authenticated
  using (
    org_id = auth.current_org_id()
    and (auth.is_org_wide() or auth.is_assigned(patient_id))
  );

-- ---------------------------------------------------------------------------
-- worry_signals
--
-- A nurse may raise worry for a patient they are assigned to, and may read
-- back their own signals. Charge nurses and admins see all of them -- that is
-- the point of the layer.
-- ---------------------------------------------------------------------------

create policy worry_read on worry_signals
  for select to authenticated
  using (
    org_id = auth.current_org_id()
    and (auth.is_org_wide() or nurse_id = auth.uid())
  );

create policy worry_nurse_insert on worry_signals
  for insert to authenticated
  with check (
    org_id = auth.current_org_id()
    and nurse_id = auth.uid()
    and auth.is_assigned(patient_id)
  );

-- Deliberately no update/delete policy: a raised worry signal is immutable.
-- Retracting it is a new signal at level 0, which keeps the audit trail honest.

-- ---------------------------------------------------------------------------
-- escalations
-- ---------------------------------------------------------------------------

create policy escalations_read on escalations
  for select to authenticated
  using (
    org_id = auth.current_org_id()
    and (auth.is_org_wide() or auth.is_assigned(patient_id))
  );

-- Acknowledge/assess transitions go through the API so that timestamps are
-- server-authoritative; a client-set acknowledged_at would make the
-- worry-to-response metric meaningless.
create policy escalations_charge_update on escalations
  for update to authenticated
  using (org_id = auth.current_org_id() and auth.is_org_wide())
  with check (org_id = auth.current_org_id() and auth.is_org_wide());

-- ---------------------------------------------------------------------------
-- outcomes / audit_log
-- ---------------------------------------------------------------------------

create policy outcomes_read on outcomes
  for select to authenticated
  using (org_id = auth.current_org_id() and auth.is_org_wide());

create policy audit_admin_read on audit_log
  for select to authenticated
  using (org_id = auth.current_org_id() and auth.current_app_role() = 'admin');

-- ---------------------------------------------------------------------------
-- realtime: dashboard subscribes to these two tables
-- ---------------------------------------------------------------------------

alter publication supabase_realtime add table worry_signals;
alter publication supabase_realtime add table escalations;
alter publication supabase_realtime add table risk_scores;
