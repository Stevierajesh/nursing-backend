-- Simulated patients stream continuously instead of stopping at the end of
-- their authored arc.
--
-- Before this, both RPCs clamped the run clock with
-- `least(duration_minutes*60, ...)`. Once a run reached its duration the
-- clamped elapsed stopped advancing, so `p_elapsed > last_sample_elapsed` could
-- never be satisfied again and the run was flipped to 'completed'. A patient
-- went permanently silent a few minutes after admission, which is not how a
-- monitored bed behaves -- a ward does not stop taking observations because a
-- scenario finished.
--
-- Now the clock runs unbounded. `duration_minutes` keeps its original meaning:
-- how long the authored trajectory takes to play out. Past that point the
-- patient HOLDS at the end state of their scenario and keeps producing
-- observations around it. Holding is the only defensible default -- a patient
-- who deteriorated does not silently reset to baseline, and looping the arc
-- would invent a relapse nobody authored.

-- Unbounded clock; the 15-second cadence is now the only gate.
create or replace function public.simulation_snapshot(p_org uuid,p_patient uuid)
returns jsonb language plpgsql set search_path=public,pg_temp as $$
declare r simulation_runs; elapsed double precision;
begin
  select * into r from simulation_runs where org_id=p_org and patient_id=p_patient and status='running';
  if not found then return null; end if;
  if not exists(select 1 from patients where id=p_patient and org_id=p_org and discharged_at is null) then
    update simulation_runs set status='stopped',running_since=null,revision=revision+1 where id=r.id and status='running';
    return null;
  end if;
  elapsed:=r.elapsed_seconds+extract(epoch from(clock_timestamp()-r.running_since));
  -- The old "or we are at the end" escape hatch is gone with the clamp: there
  -- is no final sample to let through any more.
  if elapsed-r.last_sample_elapsed<15 then return null; end if;
  return to_jsonb(r)||jsonb_build_object('elapsed_seconds',elapsed);
end $$;

-- Same, and no longer self-completes.
create or replace function public.simulation_append(p_org uuid,p_run uuid,p_revision integer,p_elapsed double precision,p_values jsonb)
returns boolean language plpgsql set search_path=public,pg_temp as $$
declare r simulation_runs; v vitals; elapsed double precision;
begin
  -- Same lock order as control: patient first, run second.
  perform 1 from patients where id=(select patient_id from simulation_runs where id=p_run and org_id=p_org)
    and org_id=p_org and discharged_at is null for update;
  if not found then return false; end if;
  select * into r from simulation_runs where id=p_run and org_id=p_org for update;
  if not found or r.status<>'running' or r.revision<>p_revision then return false; end if;
  elapsed:=r.elapsed_seconds+extract(epoch from(clock_timestamp()-r.running_since));
  -- Monotonic and drift guards are unchanged; they are what make this safe to
  -- run from more than one worker.
  if p_elapsed<0 or p_elapsed<=r.last_sample_elapsed or abs(elapsed-p_elapsed)>10 then return false; end if;
  v:=jsonb_populate_record(null::vitals,p_values);
  insert into vitals(org_id,patient_id,recorded_at,heart_rate,resp_rate,spo2,systolic_bp,diastolic_bp,
    temperature_c,pain_score,supplemental_o2,consciousness,news_score,source,simulation_run_id,simulation_elapsed_seconds)
  values(r.org_id,r.patient_id,clock_timestamp(),v.heart_rate,v.resp_rate,v.spo2,v.systolic_bp,v.diastolic_bp,
    v.temperature_c,v.pain_score,v.supplemental_o2,v.consciousness,v.news_score,'simulation',r.id,p_elapsed);
  update simulation_runs set last_sample_elapsed=p_elapsed,revision=revision+1 where id=r.id;
  return true;
end $$;

-- CREATE OR REPLACE keeps the existing ACL, but these are re-stated so the
-- lockdown is readable in one place rather than inferred from migration 011.
revoke all on function public.simulation_snapshot(uuid,uuid),
  public.simulation_append(uuid,uuid,integer,double precision,jsonb) from public,anon,authenticated;
grant execute on function public.simulation_snapshot(uuid,uuid),
  public.simulation_append(uuid,uuid,integer,double precision,jsonb) to service_role;

-- Runs parked at 'completed' by the old rule are resumed from where they
-- stopped, so their arc does not replay from the beginning.
update public.simulation_runs
set status='running', running_since=clock_timestamp(), revision=revision+1
where status='completed';
