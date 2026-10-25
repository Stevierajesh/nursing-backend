-- Reconstruct the old constraint and data inside a rolled-back transaction.
begin;
update worry_signals set level = 2;
alter table worry_signals drop constraint worry_signals_level_check;
alter table worry_signals add constraint worry_signals_level_check check (level between 0 and 2);
insert into risk_scores (org_id, patient_id, score, inputs)
values ('aaaaaaaa-0000-0000-0000-000000000001',
        'aaaaaaaa-2222-0000-0000-000000000001', 25, '{"worry_level":2}');

\ir ../../supabase/migrations/20261024000700_five_worry_levels.sql

do $$
declare n integer;
begin
  if exists (select 1 from worry_signals where level <> 4) then
    raise exception 'Historical Very worried was not preserved';
  end if;
  if exists (select 1 from risk_scores where inputs->>'worry_level' = '2') then
    raise exception 'Historical risk metadata was not migrated';
  end if;
  for n in 0..4 loop
    update worry_signals set level = n;
  end loop;
  begin
    update worry_signals set level = 5;
    raise exception 'Level 5 accepted';
  exception when check_violation then null;
  end;
  begin
    update worry_signals set level = -1;
    raise exception 'Negative level accepted';
  exception when check_violation then null;
  end;
  raise notice 'PASS: historical levels preserved; exactly five levels accepted';
end $$;
rollback;
