-- Finish the job migration 20261024001500 started.
--
-- That migration removed the `least(duration_minutes*60, ...)` clamp from
-- simulation_snapshot and simulation_append so runs stream past their arc, but
-- it missed the same clamp in simulation_control's pause and stop branches.
--
-- The consequence is a run that cannot be restarted. Pausing or stopping a
-- patient who has been streaming for hours rewinds elapsed_seconds to the
-- duration cap (600s for a 10-minute arc) while last_sample_elapsed stays at
-- the real figure (23162s on Raghav Jagan, bed 23B). Resume then needs
-- `p_elapsed > last_sample_elapsed` to come true, which takes the difference in
-- real time -- six hours of silence on that patient -- because every append is
-- rejected by the monotonic guard until the clock catches up.
--
-- The clock is unbounded now, so just record it truthfully.

create or replace function public.simulation_control(p_org uuid,p_patient uuid,p_actor uuid,p_action text,p_config jsonb default null)
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
    -- Never resume behind the last sample taken, or the monotonic guard in
    -- simulation_append silently drops every observation until real time
    -- catches up with the recorded clock.
    update simulation_runs set status='running',running_since=t,revision=revision+1,
      elapsed_seconds=greatest(elapsed_seconds,last_sample_elapsed)
    where id=r.id returning * into r;
  elsif p_action='pause' and r.status='running' then
    update simulation_runs set status='paused',
      elapsed_seconds=elapsed_seconds+extract(epoch from(t-running_since)),
      running_since=null,revision=revision+1
    where id=r.id returning * into r;
  elsif p_action='stop' and r.status in ('ready','running','paused') then
    update simulation_runs set status='stopped',
      elapsed_seconds=elapsed_seconds+coalesce(extract(epoch from(t-running_since)),0),
      running_since=null,revision=revision+1
    where id=r.id returning * into r;
  else raise exception 'Action is not valid for the current simulation status' using errcode='22023';
  end if;
  insert into audit_log(org_id,actor_id,action,target_type,target_id,payload)
  values(p_org,p_actor,'simulation.'||p_action,'simulation_run',r.id,jsonb_build_object('patient_id',p_patient));
  return to_jsonb(r);
end $$;

revoke all on function public.simulation_control(uuid,uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function public.simulation_control(uuid,uuid,uuid,text,jsonb) to service_role;

-- Repair runs already stranded behind their own sampling clock.
update public.simulation_runs
set elapsed_seconds = last_sample_elapsed
where last_sample_elapsed > elapsed_seconds;
