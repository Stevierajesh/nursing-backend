\set ON_ERROR_STOP on
begin;

-- Real CHECK constraint coverage: mocks cannot catch a missing allowed band.
insert into public.medication_reviews
  (org_id, patient_id, requested_by, prescriber_id, condition, request_note,
   recommended_band, recommended_class_id, suggested_class_id, nurse_action)
select 'aaaaaaaa-0000-0000-0000-000000000001',
       'aaaaaaaa-2222-0000-0000-000000000001',
       '22222222-2222-2222-2222-222222222222',
       '11111111-1111-1111-1111-111111111111', condition,
       'Low observation regression test', band, null, null, 'none'
from unnest(array['blood_pressure','blood_sugar','heart_rate']) condition
cross join unnest(array['low','watch','high','critical',null]::text[]) band;

do $$ begin
  assert (select count(*) from public.medication_reviews
          where request_note='Low observation regression test')=15;
  begin
    update public.medication_reviews set recommended_band='invalid'
    where request_note='Low observation regression test';
    raise exception 'Invalid band accepted';
  exception when check_violation then null; end;
end $$;

-- One frequently scored patient must not crowd out an older score for another.
insert into public.risk_scores(org_id,patient_id,computed_at,score)
select 'aaaaaaaa-0000-0000-0000-000000000001',
       'aaaaaaaa-2222-0000-0000-000000000001',
       '2026-10-27'::timestamptz + n * interval '1 minute', n % 100
from generate_series(1,100) n;
insert into public.risk_scores(org_id,patient_id,computed_at,score) values
('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-2222-0000-0000-000000000002','2026-10-01',42),
('bbbbbbbb-0000-0000-0000-000000000002','bbbbbbbb-2222-0000-0000-000000000003','2026-10-28',99);

set local role service_role;
do $$
declare rows_found int;
begin
  select count(*) into rows_found from public.latest_patient_risks(
    'aaaaaaaa-0000-0000-0000-000000000001', array[
    'aaaaaaaa-2222-0000-0000-000000000001',
    'aaaaaaaa-2222-0000-0000-000000000001',
    'aaaaaaaa-2222-0000-0000-000000000002',
    'bbbbbbbb-2222-0000-0000-000000000003',
    'ffffffff-ffff-ffff-ffff-ffffffffffff']::uuid[]);
  assert rows_found=2, 'Must return one row per requested patient in the org';
  assert (select score from public.latest_patient_risks(
    'aaaaaaaa-0000-0000-0000-000000000001',
    array['aaaaaaaa-2222-0000-0000-000000000002']::uuid[]))=42,
    'Older patient score must not be lost';
  assert (select computed_at from public.latest_patient_risks(
    'aaaaaaaa-0000-0000-0000-000000000001',
    array['aaaaaaaa-2222-0000-0000-000000000001']::uuid[]))=
    '2026-10-27'::timestamptz + interval '100 minutes', 'Must pick the newest score';
  assert not exists(select from public.latest_patient_risks(
    'aaaaaaaa-0000-0000-0000-000000000001',array[]::uuid[]));
end $$;
reset role;

-- Passing an arbitrary org/patient must never expose the RPC to clients.
set local role authenticated;
do $$ begin
  begin
    perform public.latest_patient_risks('aaaaaaaa-0000-0000-0000-000000000001',array[]::uuid[]);
    raise exception 'Authenticated client could call backend-only RPC';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
do $$ begin
  assert not has_function_privilege('anon','public.latest_patient_risks(uuid,uuid[])','execute');
  raise notice 'PASS: low-band persistence and bounded, org-scoped latest-risk lookup';
end $$;
rollback;
