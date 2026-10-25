-- Verify RLS actually isolates organizations and scopes nurses to their
-- assigned patients. Runs as the `authenticated` role with auth.uid() faked
-- via a GUC, which is how Supabase evaluates these policies in practice.

\set ON_ERROR_STOP on
\pset pager off

-- ---------------------------------------------------------------------------
-- fixtures: two orgs, so cross-tenant leakage is testable
-- ---------------------------------------------------------------------------

insert into auth.users (id, email) values
  ('11111111-1111-1111-1111-111111111111', 'charge-a@a.test'),
  ('22222222-2222-2222-2222-222222222222', 'nurse-a1@a.test'),
  ('33333333-3333-3333-3333-333333333333', 'nurse-a2@a.test'),
  ('44444444-4444-4444-4444-444444444444', 'charge-b@b.test');

insert into organizations (id, name, slug) values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'Hospital A', 'hosp-a'),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'Hospital B', 'hosp-b');

insert into units (id, org_id, name) values
  ('aaaaaaaa-1111-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001', 'A-West'),
  ('bbbbbbbb-1111-0000-0000-000000000002', 'bbbbbbbb-0000-0000-0000-000000000002', 'B-North');

insert into profiles (id, org_id, unit_id, role, full_name, email) values
  ('11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000001',
   'aaaaaaaa-1111-0000-0000-000000000001', 'charge_nurse', 'Charge A', 'charge-a@a.test'),
  ('22222222-2222-2222-2222-222222222222', 'aaaaaaaa-0000-0000-0000-000000000001',
   'aaaaaaaa-1111-0000-0000-000000000001', 'nurse', 'Nurse A1', 'nurse-a1@a.test'),
  ('33333333-3333-3333-3333-333333333333', 'aaaaaaaa-0000-0000-0000-000000000001',
   'aaaaaaaa-1111-0000-0000-000000000001', 'nurse', 'Nurse A2', 'nurse-a2@a.test'),
  ('44444444-4444-4444-4444-444444444444', 'bbbbbbbb-0000-0000-0000-000000000002',
   'bbbbbbbb-1111-0000-0000-000000000002', 'charge_nurse', 'Charge B', 'charge-b@b.test');

insert into patients (id, org_id, unit_id, mrn_hash, display_name, room) values
  ('aaaaaaaa-2222-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
   'aaaaaaaa-1111-0000-0000-000000000001', 'hash-a1', 'Patient A-One', '101'),
  ('aaaaaaaa-2222-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000001',
   'aaaaaaaa-1111-0000-0000-000000000001', 'hash-a2', 'Patient A-Two', '102'),
  ('bbbbbbbb-2222-0000-0000-000000000003', 'bbbbbbbb-0000-0000-0000-000000000002',
   'bbbbbbbb-1111-0000-0000-000000000002', 'hash-b1', 'Patient B-One', '201');

-- Nurse A1 is assigned only to Patient A-One.
insert into assignments (org_id, nurse_id, patient_id) values
  ('aaaaaaaa-0000-0000-0000-000000000001',
   '22222222-2222-2222-2222-222222222222',
   'aaaaaaaa-2222-0000-0000-000000000001');

insert into worry_signals (org_id, patient_id, nurse_id, level, reasons) values
  ('aaaaaaaa-0000-0000-0000-000000000001',
   'aaaaaaaa-2222-0000-0000-000000000001',
   '22222222-2222-2222-2222-222222222222', 4, '{breathing_off}'),
  ('bbbbbbbb-0000-0000-0000-000000000002',
   'bbbbbbbb-2222-0000-0000-000000000003',
   '44444444-4444-4444-4444-444444444444', 4, '{color}');

-- RLS does not apply to the table owner, so test as `authenticated`.

