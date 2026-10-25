-- Nurse-app admissions start stable monitoring atomically with the patient,
-- bed placement, nurse assignment, and first normal observation.
create or replace function public.admit_patient(p_org uuid, p_actor uuid, p_name text, p_unit uuid, p_bed uuid default null)
returns jsonb language plpgsql set search_path = public, pg_temp as $$
declare
  actor_role text;
  target public.unit_beds;
  admitted public.patients;
  run_id uuid;
  baseline jsonb := '{"heart_rate":78,"resp_rate":16,"spo2":98,"systolic_bp":124,"diastolic_bp":78,"temperature_c":36.8}'::jsonb;
  config jsonb;
begin
  select role::text into actor_role from profiles where id = p_actor and org_id = p_org;
  if actor_role is null or actor_role not in ('nurse', 'charge_nurse', 'admin') then
    raise exception 'Insufficient permission' using errcode = '42501';
  end if;
  if p_name is null or length(trim(p_name)) not between 1 and 200 then
    raise exception 'Patient name is required' using errcode = '22023';
  end if;
  if not exists (select 1 from units where id = p_unit and org_id = p_org) then
    raise exception 'Unit not found' using errcode = 'P0002';
  end if;
  if p_bed is not null then
    select * into target from unit_beds where id = p_bed and org_id = p_org and unit_id = p_unit for update;
    if not found then raise exception 'Bed not found in this unit' using errcode = 'P0002'; end if;
    if exists (select 1 from patients where org_id = p_org and unit_id = p_unit and bed = target.bed and discharged_at is null) then
      raise exception 'This bed is already occupied. Choose another bed.' using errcode = '23505';
    end if;
  end if;
  insert into patients (org_id, unit_id, display_name, mrn_hash, room, bed)
  values (p_org, p_unit, trim(p_name), 'admit-' || gen_random_uuid()::text, target.room, target.bed)
  returning * into admitted;
  if actor_role = 'nurse' then
    insert into assignments (org_id, nurse_id, patient_id) values (p_org, p_actor, admitted.id);
  end if;
  config := jsonb_build_object('scenario', 'stable', 'health_context', '',
    'baseline', baseline, 'duration_minutes', 10, 'onset_minutes', 1, 'seed', 42);
  update patients set is_simulated = true, baseline_vitals = baseline
    where id = admitted.id returning * into admitted;
  insert into simulation_runs (org_id, patient_id, config, status, running_since, last_sample_elapsed, created_by)
  values (p_org, admitted.id, config, 'running', clock_timestamp(), 0, p_actor)
  returning id into run_id;
  -- The first reading is present before admission returns. The existing
  -- 15-second scheduler continues the stable scenario indefinitely.
  insert into vitals (org_id, patient_id, heart_rate, resp_rate, spo2, systolic_bp,
    diastolic_bp, temperature_c, supplemental_o2, consciousness, news_score,
    source, simulation_run_id, simulation_elapsed_seconds)
  values (p_org, admitted.id, 78, 16, 98, 124, 78, 36.8, false, 'A', 0,
    'simulation', run_id, 0);
  insert into audit_log (org_id, actor_id, action, target_type, target_id, payload)
  values (p_org, p_actor, 'simulation.start', 'simulation_run', run_id,
    jsonb_build_object('patient_id', admitted.id, 'scenario', 'stable', 'reason', 'nurse_app_admission'));
  insert into audit_log (org_id, actor_id, action, target_type, target_id, payload)
  values (p_org, p_actor, 'patient.admitted', 'patient', admitted.id, jsonb_build_object('unit_id', p_unit, 'bed_id', p_bed));
  return to_jsonb(admitted);
end $$;
revoke all on function public.admit_patient(uuid,uuid,text,uuid,uuid) from public, anon, authenticated;
grant execute on function public.admit_patient(uuid,uuid,text,uuid,uuid) to service_role;
notify pgrst, 'reload schema';
