-- Admit and optionally place a patient in one transaction. Nurse admissions
-- also create an active assignment so the new patient is visible immediately.
create function public.admit_patient(p_org uuid, p_actor uuid, p_name text, p_unit uuid, p_bed uuid default null)
returns jsonb language plpgsql set search_path = public, pg_temp as $$
declare
  actor_role text;
  target public.unit_beds;
  admitted public.patients;
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
  insert into audit_log (org_id, actor_id, action, target_type, target_id, payload)
  values (p_org, p_actor, 'patient.admitted', 'patient', admitted.id, jsonb_build_object('unit_id', p_unit, 'bed_id', p_bed));
  return to_jsonb(admitted);
end $$;
revoke all on function public.admit_patient(uuid,uuid,text,uuid,uuid) from public, anon, authenticated;
grant execute on function public.admit_patient(uuid,uuid,text,uuid,uuid) to service_role;
notify pgrst, 'reload schema';
