-- Expand the scale without reinterpreting historical "Very worried" (2).
-- Apply once, before deploying the updated API and both clients.
alter table public.worry_signals drop constraint worry_signals_level_check;
update public.worry_signals set level = 4 where level = 2;
alter table public.worry_signals add constraint worry_signals_level_check
  check (level between 0 and 4);

-- Keep historical risk metadata consistent with the migrated signal labels.
update public.risk_scores
set inputs = jsonb_set(inputs, '{worry_level}', '4'::jsonb)
where inputs->>'worry_level' = '2';
