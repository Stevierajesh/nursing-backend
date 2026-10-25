\set ON_ERROR_STOP on
begin;
-- Exercise real SQL functions, lifecycle, transaction rollback and provenance.
do $$
declare
  org_a uuid:='aaaaaaaa-0000-0000-0000-000000000001';
  org_b uuid:='bbbbbbbb-0000-0000-0000-000000000002';
  actor uuid:='11111111-1111-1111-1111-111111111111';
  nurse uuid:='22222222-2222-2222-2222-222222222222';
  pid uuid:='aaaaaaaa-2222-0000-0000-000000000001';
  cfg jsonb:='{"scenario":"stable","duration_minutes":2,"onset_minutes":0,"seed":42,"health_context":"Postoperative","baseline":{"heart_rate":78,"resp_rate":16,"spo2":98,"systolic_bp":124,"diastolic_bp":78,"temperature_c":36.8}}';
  vals jsonb:='{"heart_rate":78,"resp_rate":16,"spo2":98,"systolic_bp":124,"diastolic_bp":78,"temperature_c":36.8,"news_score":0,"supplemental_o2":false,"consciousness":"A"}';
  r jsonb; snap jsonb; oldid uuid; freshid uuid; before_count integer; made jsonb;
begin
  -- No privilege to configure for a nurse or for a foreign organization.
  begin perform simulation_control(org_a,pid,nurse,'configure',cfg); raise exception 'Nurse configured simulation';
  exception when insufficient_privilege then null; end;
  begin perform simulation_control(org_b,pid,actor,'configure',cfg); raise exception 'Cross-org configuration accepted';
  exception when insufficient_privilege then null; end;
  r:=simulation_control(org_a,pid,actor,'configure',cfg);
  assert r->>'status'='ready';
  assert (select is_simulated from patients where id=pid);
  assert (select health_context from patients where id=pid)='Postoperative';
  assert simulation_snapshot(org_a,pid) is null;
  begin perform simulation_control(org_a,pid,actor,'configure',cfg); raise exception 'Duplicate active simulation';
  exception when invalid_parameter_value then null; end;
  r:=simulation_control(org_a,pid,actor,'start'); oldid:=(r->>'id')::uuid;
  snap:=simulation_snapshot(org_a,pid);
  assert simulation_append(org_a,oldid,(snap->>'revision')::integer,(snap->>'elapsed_seconds')::float,vals);
  assert not simulation_append(org_a,oldid,(snap->>'revision')::integer,(snap->>'elapsed_seconds')::float,vals), 'Duplicate worker inserted same snapshot';
  assert simulation_snapshot(org_a,pid) is null, 'Sample interval not respected';
  assert not simulation_append(org_b,oldid,1,15,vals), 'Cross-org append';
  assert (select count(*) from vitals where simulation_run_id=oldid and source='simulation')=1;
  -- A prepared snapshot cannot commit after pause.
  update simulation_runs set running_since=clock_timestamp()-interval '30 seconds' where id=oldid;
  snap:=simulation_snapshot(org_a,pid);
  r:=simulation_control(org_a,pid,actor,'pause');
  assert r->>'status'='paused';
  assert (r->>'elapsed_seconds')::float>=30;
  assert not simulation_append(org_a,oldid,(snap->>'revision')::integer,(snap->>'elapsed_seconds')::float,vals), 'Paused snapshot committed';
  assert simulation_snapshot(org_a,pid) is null;
  r:=simulation_control(org_a,pid,actor,'resume');
  assert r->>'status'='running';
  assert (r->>'elapsed_seconds')::float>=30, 'Resume lost elapsed time';
  snap:=simulation_snapshot(org_a,pid);
  r:=simulation_control(org_a,pid,actor,'restart'); freshid:=(r->>'id')::uuid;
  assert freshid<>oldid;
  assert (select status from simulation_runs where id=oldid)='stopped';
  assert not simulation_append(org_a,oldid,(snap->>'revision')::integer,(snap->>'elapsed_seconds')::float,vals), 'Stale run inserted after restart';
  assert (select count(*) from vitals where simulation_run_id=oldid)=1, 'Restart erased history';
  -- Completion writes the final observation once, and stops.
  update simulation_runs set running_since=clock_timestamp()-interval '121 seconds' where id=freshid;
  snap:=simulation_snapshot(org_a,pid);
  assert simulation_append(org_a,freshid,(snap->>'revision')::integer,(snap->>'elapsed_seconds')::float,vals);
  assert (select status from simulation_runs where id=freshid)='completed';
  assert simulation_snapshot(org_a,pid) is null;
  -- Discharging a patient prevents further generation.
  r:=simulation_control(org_a,pid,actor,'restart'); freshid:=(r->>'id')::uuid;
  update patients set discharged_at=now() where id=pid;
  assert simulation_snapshot(org_a,pid) is null;
  assert (select status from simulation_runs where id=freshid)='stopped';
  update patients set discharged_at=null where id=pid;
  -- Atomic creation includes profile and assignment; errors leave no patient.
  made:=create_simulation_patient(org_a,actor,'{"display_name":"Simulated new patient","mrn_hash":"sim-new","unit_id":"aaaaaaaa-1111-0000-0000-000000000001"}',cfg,nurse);
  assert made->'patient'->>'is_simulated'='true';
  assert exists(select 1 from assignments where patient_id=(made->'patient'->>'id')::uuid and nurse_id=nurse);
  select count(*) into before_count from patients;
  begin
    perform create_simulation_patient(org_a,actor,'{"display_name":"Must roll back","mrn_hash":"sim-invalid","unit_id":"bbbbbbbb-1111-0000-0000-000000000002"}',cfg,nurse);
    raise exception 'Foreign unit accepted';
  exception when invalid_parameter_value then null; end;
  assert (select count(*) from patients)=before_count;
  assert not has_function_privilege('authenticated','simulation_control(uuid,uuid,uuid,text,jsonb)','execute');
  assert not has_function_privilege('anon','simulation_snapshot(uuid,uuid)','execute');
  assert not has_function_privilege('authenticated','simulation_append(uuid,uuid,integer,double precision,jsonb)','execute');
  assert not has_function_privilege('authenticated','create_simulation_patient(uuid,uuid,jsonb,jsonb,uuid)','execute');
  assert not has_table_privilege('authenticated','simulation_runs','insert');
  assert not has_table_privilege('authenticated','simulation_runs','truncate');
  raise notice 'PASS: simulation lifecycle, race guards, scope, provenance, atomic patient creation and RPC permissions';
end $$;
-- Scenario controls are visible only to charge/admin within their org.
set local role authenticated;
select set_config('test.current_user_id','11111111-1111-1111-1111-111111111111',true);
do $$ begin
  assert (select count(*) from simulation_runs)>0;
  assert not exists(select 1 from simulation_runs where org_id<>'aaaaaaaa-0000-0000-0000-000000000001');
end $$;
select set_config('test.current_user_id','22222222-2222-2222-2222-222222222222',true);
do $$ begin assert (select count(*) from simulation_runs)=0; end $$;
set local role service_role;
do $$ declare r jsonb; begin
  r:=simulation_control('aaaaaaaa-0000-0000-0000-000000000001',
    'aaaaaaaa-2222-0000-0000-000000000001','11111111-1111-1111-1111-111111111111','restart');
  assert r->>'status'='running', 'Service role could not control playback';
  assert simulation_snapshot('aaaaaaaa-0000-0000-0000-000000000001',
    'aaaaaaaa-2222-0000-0000-000000000001') is not null;
end $$;
rollback;
