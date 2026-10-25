\set ON_ERROR_STOP on
begin;
do $$
declare
  org_id uuid := 'aaaaaaaa-0000-0000-0000-000000000001';
  test_nurse uuid := '22222222-2222-2222-2222-222222222222';
  unit_id uuid := 'aaaaaaaa-1111-0000-0000-000000000001';
  bed_id uuid := gen_random_uuid();
  admitted jsonb;
  pid uuid;
  run public.simulation_runs;
  reading public.vitals;
  snapshot jsonb;
begin
  insert into unit_beds(id, org_id, unit_id, room, bed)
  values(bed_id, org_id, unit_id, 'Admission test', 'test' || bed_id::text);
  admitted := admit_patient(org_id, test_nurse, 'Stable admission test', unit_id, bed_id);
  pid := (admitted->>'id')::uuid;
  assert (admitted->>'is_simulated')::boolean;
  assert exists(select 1 from assignments a where a.patient_id=pid and a.nurse_id=test_nurse and a.shift_end is null);
  select * into strict run from simulation_runs where patient_id=pid;
  assert run.status='running';
  assert run.config->>'scenario'='stable';
  assert run.last_sample_elapsed=0;
  select * into strict reading from vitals where patient_id=pid;
  assert reading.source='simulation' and reading.simulation_run_id=run.id and reading.simulation_elapsed_seconds=0;
  assert reading.news_score=0 and reading.heart_rate=78 and reading.resp_rate=16 and reading.spo2=98;
  assert reading.systolic_bp=124 and reading.diastolic_bp=78 and reading.temperature_c=36.8;
  assert simulation_snapshot(org_id,pid) is null, 'Must wait for next 15-second interval';
  -- The ongoing stream remains available after the initial scenario duration.
  update simulation_runs set running_since=clock_timestamp()-interval '1 hour' where id=run.id;
  snapshot := simulation_snapshot(org_id,pid);
  assert (snapshot->>'elapsed_seconds')::float > (run.config->>'duration_minutes')::float*60;
  assert simulation_append(org_id,run.id,(snapshot->>'revision')::integer,(snapshot->>'elapsed_seconds')::float,to_jsonb(reading));
  assert (select count(*) from vitals where patient_id=pid)=2;
  assert (select status from simulation_runs where id=run.id)='running';
  -- An occupied-bed admission must not create another patient or simulation.
  begin
    perform admit_patient(org_id,test_nurse,'Rejected admission',unit_id,bed_id);
    raise exception 'Occupied bed accepted';
  exception when unique_violation then null;
  end;
  assert (select count(*) from simulation_runs where patient_id=pid)=1;
end $$;
rollback;
