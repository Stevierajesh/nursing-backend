-- Authored real-time simulations. Clinical observations retain their provenance.
alter table public.patients add column is_simulated boolean not null default false;
alter table public.patients add column health_context text;
alter table public.patients add column baseline_vitals jsonb;
create table public.simulation_runs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.organizations(id) on delete cascade,
  patient_id uuid not null references public.patients(id) on delete cascade,
  config jsonb not null check (jsonb_typeof(config) = 'object'),
  status text not null default 'ready' check (status in ('ready','running','paused','completed','stopped')),
  elapsed_seconds double precision not null default 0 check (elapsed_seconds >= 0),
  running_since timestamptz,
  last_sample_elapsed double precision not null default -15,
  revision integer not null default 0,
  created_by uuid not null references public.profiles(id),
  created_at timestamptz not null default clock_timestamp()
);
create unique index simulation_one_active on public.simulation_runs(patient_id)
where status in ('ready','running','paused');
create index simulation_org_patient on public.simulation_runs(org_id,patient_id,created_at desc);
alter table public.vitals add column source text not null default 'charted'
  check (source in ('charted','simulation'));
alter table public.vitals add column simulation_run_id uuid references public.simulation_runs(id);
alter table public.vitals add column simulation_elapsed_seconds double precision;
create unique index simulation_sample_unique on public.vitals(simulation_run_id,simulation_elapsed_seconds)
  where simulation_run_id is not null;
alter table public.vitals add constraint simulation_provenance check (
  (source='charted' and simulation_run_id is null and simulation_elapsed_seconds is null)
  or (source='simulation' and simulation_run_id is not null and simulation_elapsed_seconds is not null));
alter table public.simulation_runs enable row level security;
revoke all on public.simulation_runs from anon, authenticated;
grant select on public.simulation_runs to authenticated;
grant all on public.simulation_runs to service_role;
create policy simulation_admin_read on public.simulation_runs for select to authenticated
using (org_id=private.current_org_id() and private.current_app_role()::text in ('admin','charge_nurse'));

-- RPCs are service-role only. API authenticates the caller; SQL enforces scope
-- again and serializes controls and emissions by locking the patient/run.
create function public.simulation_control(p_org uuid,p_patient uuid,p_actor uuid,p_action text,p_config jsonb default null)
returns jsonb language plpgsql set search_path=public,pg_temp as $$
declare r simulation_runs; pat patients; t timestamptz:=clock_timestamp();
begin
  if not exists(select 1 from profiles where id=p_actor and org_id=p_org and role::text in ('admin','charge_nurse')) then
    raise exception 'Simulation management requires charge nurse or admin' using errcode='42501';
  end if;
  select * into pat from patients where id=p_patient and org_id=p_org for update;
  if not found then raise exception 'Patient not found' using errcode='P0002'; end if;
  if pat.discharged_at is not null then raise exception 'Patient is discharged' using errcode='22023'; end if;
  select * into r from simulation_runs where patient_id=p_patient and org_id=p_org
    order by created_at desc,id desc limit 1 for update;
  if p_action='configure' then
    if r.status in ('ready','running','paused') then raise exception 'Stop the existing run before configuring another' using errcode='22023'; end if;
    if p_config is null then raise exception 'Configuration required' using errcode='22023'; end if;
    insert into simulation_runs(org_id,patient_id,config,created_by)
    values(p_org,p_patient,p_config,p_actor) returning * into r;
    update patients set is_simulated=true,health_context=p_config->>'health_context',baseline_vitals=p_config->'baseline' where id=p_patient;
  elsif r.id is null then
    raise exception 'Configure a simulation first' using errcode='22023';
  elsif p_action='restart' then
    update simulation_runs set status='stopped',running_since=null,revision=revision+1 where id=r.id;
    insert into simulation_runs(org_id,patient_id,config,created_by,status,running_since)
    values(p_org,p_patient,r.config,p_actor,'running',t) returning * into r;
  elsif (p_action='start' and r.status='ready') or (p_action='resume' and r.status='paused') then
    update simulation_runs set status='running',running_since=t,revision=revision+1 where id=r.id returning * into r;
  elsif p_action='pause' and r.status='running' then
    update simulation_runs set status='paused',elapsed_seconds=least((config->>'duration_minutes')::float*60,
      elapsed_seconds+extract(epoch from(t-running_since))),running_since=null,revision=revision+1
    where id=r.id returning * into r;
  elsif p_action='stop' and r.status in ('ready','running','paused') then
    update simulation_runs set status='stopped',elapsed_seconds=least((config->>'duration_minutes')::float*60,
      elapsed_seconds+coalesce(extract(epoch from(t-running_since)),0)),running_since=null,revision=revision+1
    where id=r.id returning * into r;
  else raise exception 'Action is not valid for the current simulation status' using errcode='22023';
  end if;
  insert into audit_log(org_id,actor_id,action,target_type,target_id,payload)
  values(p_org,p_actor,'simulation.'||p_action,'simulation_run',r.id,jsonb_build_object('patient_id',p_patient));
  return to_jsonb(r);
end $$;

create function public.simulation_snapshot(p_org uuid,p_patient uuid)
returns jsonb language plpgsql set search_path=public,pg_temp as $$
declare r simulation_runs; elapsed double precision;
begin
  select * into r from simulation_runs where org_id=p_org and patient_id=p_patient and status='running';
  if not found then return null; end if;
  if not exists(select 1 from patients where id=p_patient and org_id=p_org and discharged_at is null) then
    update simulation_runs set status='stopped',running_since=null,revision=revision+1 where id=r.id and status='running';
    return null;
  end if;
  elapsed:=least((r.config->>'duration_minutes')::float*60,
    r.elapsed_seconds+extract(epoch from(clock_timestamp()-r.running_since)));
  if elapsed-r.last_sample_elapsed<15 and elapsed<(r.config->>'duration_minutes')::float*60 then return null; end if;
  return to_jsonb(r)||jsonb_build_object('elapsed_seconds',elapsed);
end $$;

create function public.simulation_append(p_org uuid,p_run uuid,p_revision integer,p_elapsed double precision,p_values jsonb)
returns boolean language plpgsql set search_path=public,pg_temp as $$
declare r simulation_runs; v vitals; elapsed double precision;
begin
  -- Same lock order as control: patient first, run second.
  perform 1 from patients where id=(select patient_id from simulation_runs where id=p_run and org_id=p_org)
    and org_id=p_org and discharged_at is null for update;
  if not found then return false; end if;
  select * into r from simulation_runs where id=p_run and org_id=p_org for update;
  if not found or r.status<>'running' or r.revision<>p_revision then return false; end if;
  elapsed:=least((r.config->>'duration_minutes')::float*60,
    r.elapsed_seconds+extract(epoch from(clock_timestamp()-r.running_since)));
  if p_elapsed<0 or p_elapsed<=r.last_sample_elapsed or abs(elapsed-p_elapsed)>10 then return false; end if;
  v:=jsonb_populate_record(null::vitals,p_values);
  insert into vitals(org_id,patient_id,recorded_at,heart_rate,resp_rate,spo2,systolic_bp,diastolic_bp,
    temperature_c,supplemental_o2,consciousness,news_score,source,simulation_run_id,simulation_elapsed_seconds)
  values(r.org_id,r.patient_id,clock_timestamp(),v.heart_rate,v.resp_rate,v.spo2,v.systolic_bp,v.diastolic_bp,
    v.temperature_c,v.supplemental_o2,v.consciousness,v.news_score,'simulation',r.id,p_elapsed);
  update simulation_runs set last_sample_elapsed=p_elapsed,revision=revision+1,
    status=case when p_elapsed>=(config->>'duration_minutes')::float*60 then 'completed' else status end,
    elapsed_seconds=case when p_elapsed>=(config->>'duration_minutes')::float*60 then p_elapsed else elapsed_seconds end,
    running_since=case when p_elapsed>=(config->>'duration_minutes')::float*60 then null else running_since end
    where id=r.id;
  return true;
end $$;

-- Patient + optional simulation + assignment are created atomically.
create function public.create_simulation_patient(p_org uuid,p_actor uuid,p_patient jsonb,p_config jsonb,p_nurse uuid default null)
returns jsonb language plpgsql set search_path=public,pg_temp as $$
declare p patients; r jsonb;
begin
  if not exists(select 1 from profiles where id=p_actor and org_id=p_org and role::text in ('admin','charge_nurse')) then
    raise exception 'Insufficient role' using errcode='42501'; end if;
  if p_patient->>'unit_id' is not null and not exists(select 1 from units where id=(p_patient->>'unit_id')::uuid and org_id=p_org) then
    raise exception 'Unit not found' using errcode='22023'; end if;
  if p_nurse is not null and not exists(select 1 from profiles where id=p_nurse and org_id=p_org and role::text='nurse') then
    raise exception 'Nurse not found' using errcode='22023'; end if;
  insert into patients(org_id,display_name,mrn_hash,unit_id,room,bed,age_band,sex,race_code,primary_language)
  values(p_org,p_patient->>'display_name',p_patient->>'mrn_hash',(p_patient->>'unit_id')::uuid,
    p_patient->>'room',p_patient->>'bed',p_patient->>'age_band',p_patient->>'sex',p_patient->>'race_code',p_patient->>'primary_language') returning * into p;
  r:=simulation_control(p_org,p.id,p_actor,'configure',p_config);
  if p_nurse is not null then insert into assignments(org_id,nurse_id,patient_id) values(p_org,p_nurse,p.id); end if;
  select * into p from patients where id=p.id;
  return jsonb_build_object('patient',to_jsonb(p),'simulation',r);
end $$;

revoke all on function public.simulation_control(uuid,uuid,uuid,text,jsonb),
  public.simulation_snapshot(uuid,uuid),public.simulation_append(uuid,uuid,integer,double precision,jsonb),
  public.create_simulation_patient(uuid,uuid,jsonb,jsonb,uuid) from public,anon,authenticated;
grant execute on function public.simulation_control(uuid,uuid,uuid,text,jsonb),
  public.simulation_snapshot(uuid,uuid),public.simulation_append(uuid,uuid,integer,double precision,jsonb),
  public.create_simulation_patient(uuid,uuid,jsonb,jsonb,uuid) to service_role;
