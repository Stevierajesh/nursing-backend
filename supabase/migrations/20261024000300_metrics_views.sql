-- Metrics views.
--
-- These encode the two claims the product makes: that worry-to-response time
-- is measurable, and that the system can be audited for equity. Keeping them
-- in SQL means the dashboard and the API agree on the definition.

-- ---------------------------------------------------------------------------
-- worry-to-response time
--
-- The headline metric. ack_seconds is how long a worried nurse waited for
-- anyone to respond; assess_seconds is time to actual bedside reassessment.
-- ---------------------------------------------------------------------------

create or replace view v_worry_response_times as
select
  e.id            as escalation_id,
  e.org_id,
  e.patient_id,
  pt.unit_id,
  e.worry_signal_id,
  w.nurse_id,
  w.level         as worry_level,
  w.reasons       as worry_reasons,
  e.rule,
  e.status,
  e.triggered_at,
  e.acknowledged_at,
  e.assessed_at,
  extract(epoch from (e.acknowledged_at - e.triggered_at)) as ack_seconds,
  extract(epoch from (e.assessed_at - e.triggered_at))     as assess_seconds,
  e.status = 'auto_escalated'                              as breached_sla,
  -- demographic columns carried through so the equity view can group on them
  pt.race_code,
  pt.primary_language,
  pt.age_band,
  pt.sex
from escalations e
join patients pt on pt.id = e.patient_id
left join worry_signals w on w.id = e.worry_signal_id;

-- ---------------------------------------------------------------------------
-- equity audit
--
-- Answers the judge question "is it fair?" directly: do response times or
-- SLA breach rates differ across patient groups? Each row is one
-- (dimension, value) bucket so the API can serve any grouping uniformly.
-- ---------------------------------------------------------------------------

create or replace view v_equity_audit as
with buckets as (
  select org_id, 'race'     as dimension, coalesce(race_code, 'unknown')        as value,
         ack_seconds, assess_seconds, breached_sla
  from v_worry_response_times
  union all
  select org_id, 'language' as dimension, coalesce(primary_language, 'unknown') as value,
         ack_seconds, assess_seconds, breached_sla
  from v_worry_response_times
  union all
  select org_id, 'age_band' as dimension, coalesce(age_band, 'unknown')         as value,
         ack_seconds, assess_seconds, breached_sla
  from v_worry_response_times
  union all
  select org_id, 'sex'      as dimension, coalesce(sex, 'unknown')              as value,
         ack_seconds, assess_seconds, breached_sla
  from v_worry_response_times
)
select
  org_id,
  dimension,
  value,
  count(*)                                              as escalation_count,
  round(avg(ack_seconds)    / 60.0, 1)                  as avg_ack_minutes,
  round(avg(assess_seconds) / 60.0, 1)                  as avg_assess_minutes,
  percentile_cont(0.5) within group (order by ack_seconds) / 60.0 as median_ack_minutes,
  round(
    100.0 * count(*) filter (where breached_sla) / nullif(count(*), 0),
    1
  )                                                     as sla_breach_pct
from buckets
group by org_id, dimension, value;

-- ---------------------------------------------------------------------------
-- worry reason predictiveness
--
-- The learn loop. For each worry reason, how often was it followed by a real
-- deterioration event within 24h? This is what lets you say on stage which
-- flavours of nurse intuition carry the most signal.
-- ---------------------------------------------------------------------------

create or replace view v_worry_reason_performance as
with exploded as (
  select
    w.org_id,
    unnest(w.reasons) as reason,
    w.id              as worry_signal_id,
    w.patient_id,
    w.created_at,
    w.level
  from worry_signals w
  where w.level > 0
)
select
  x.org_id,
  x.reason,
  count(*) as signal_count,
  count(*) filter (
    where exists (
      select 1 from outcomes o
      where o.patient_id = x.patient_id
        and o.event_type in ('icu_transfer', 'rrt_call', 'code_blue', 'death')
        and o.occurred_at between x.created_at and x.created_at + interval '24 hours'
    )
  ) as followed_by_event_count,
  round(
    100.0 * count(*) filter (
      where exists (
        select 1 from outcomes o
        where o.patient_id = x.patient_id
          and o.event_type in ('icu_transfer', 'rrt_call', 'code_blue', 'death')
          and o.occurred_at between x.created_at and x.created_at + interval '24 hours'
      )
    ) / nullif(count(*), 0),
    1
  ) as precision_pct
from exploded x
group by x.org_id, x.reason;

-- Views inherit the RLS of their base tables only when created with
-- security_invoker; without it they run as the owner and leak across orgs.
alter view v_worry_response_times   set (security_invoker = on);
alter view v_equity_audit           set (security_invoker = on);
alter view v_worry_reason_performance set (security_invoker = on);
