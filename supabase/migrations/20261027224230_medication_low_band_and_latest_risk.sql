-- Low observations intentionally carry a band without recommending a drug.
-- Keep the finding when a nurse sends a free-text medication concern.
alter table public.medication_reviews
  drop constraint medication_reviews_recommended_band_check,
  add constraint medication_reviews_recommended_band_check
    check (recommended_band is null
           or recommended_band in ('low', 'watch', 'high', 'critical'));

-- Bound the work by patient count, not the accumulated simulation history.
-- Uses risk_scores_patient_time_idx to stop after one row for each patient.
-- Only the backend service may call this; it supplies authorized patient IDs
-- and the caller's org after applying nurse-assignment visibility checks.
create or replace function public.latest_patient_risks(p_org uuid, p_patients uuid[])
returns setof public.risk_scores
language sql stable security invoker
set search_path = ''
as $$
  select latest.*
  from (select distinct unnest(p_patients) as id) requested
  join public.patients p on p.id = requested.id and p.org_id = p_org
  cross join lateral (
    select r.* from public.risk_scores r
    where r.patient_id = p.id and r.org_id = p_org
    order by r.computed_at desc
    limit 1
  ) latest;
$$;

revoke all on function public.latest_patient_risks(uuid, uuid[]) from public, anon, authenticated;
grant execute on function public.latest_patient_risks(uuid, uuid[]) to service_role;
