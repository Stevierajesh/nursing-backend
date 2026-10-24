\pset pager off
begin;
set role authenticated;

\echo ''
\echo '--- READS must still work (realtime depends on them) ---'
set test.current_user_id = '11111111-1111-1111-1111-111111111111';
select count(*) as charge_a_sees_patients from patients;
set test.current_user_id = '22222222-2222-2222-2222-222222222222';
select count(*) as nurse_a1_sees_patients from patients;

\echo ''
\echo '--- ATTACK 1: nurse promotes self to admin ---'
savepoint s1;
do $$
begin
  update profiles set role = 'admin' where id = '22222222-2222-2222-2222-222222222222';
  if (select role from profiles where id = '22222222-2222-2222-2222-222222222222') = 'admin' then
    raise notice '  FAIL: nurse is now admin';
  else
    raise notice '  PASS: update did not take effect';
  end if;
exception when insufficient_privilege then
  raise notice '  PASS: blocked (insufficient_privilege)';
end $$;
rollback to savepoint s1;

\echo ''
\echo '--- ATTACK 2: charge nurse forges acknowledged_at on an escalation ---'
set test.current_user_id = '11111111-1111-1111-1111-111111111111';
savepoint s2;
do $$
begin
  update escalations set acknowledged_at = now(), status = 'acknowledged';
  raise notice '  FAIL: response timestamp was client-writable';
exception when insufficient_privilege then
  raise notice '  PASS: blocked (insufficient_privilege)';
end $$;
rollback to savepoint s2;

\echo ''
\echo '--- ATTACK 3: nurse inserts worry directly, bypassing fusion ---'
set test.current_user_id = '22222222-2222-2222-2222-222222222222';
savepoint s3;
do $$
begin
  insert into worry_signals (org_id, patient_id, nurse_id, level)
  values ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-2222-0000-0000-000000000001',
          '22222222-2222-2222-2222-222222222222', 2);
  raise notice '  FAIL: direct insert allowed, API fusion bypassed';
exception when insufficient_privilege then
  raise notice '  PASS: blocked (insufficient_privilege)';
end $$;
rollback to savepoint s3;

\echo ''
\echo '--- ATTACK 4: nurse moves self into another org ---'
savepoint s4;
do $$
begin
  update profiles set org_id = 'bbbbbbbb-0000-0000-0000-000000000002'
   where id = '22222222-2222-2222-2222-222222222222';
  raise notice '  FAIL: org hopping allowed';
exception when insufficient_privilege then
  raise notice '  PASS: blocked (insufficient_privilege)';
end $$;
rollback to savepoint s4;

\echo ''
\echo '--- ATTACK 5: nurse deletes an escalation to hide it ---'
savepoint s5;
do $$
begin
  delete from escalations;
  raise notice '  FAIL: delete allowed';
exception when insufficient_privilege then
  raise notice '  PASS: blocked (insufficient_privilege)';
end $$;
rollback to savepoint s5;

\echo ''
\echo '--- ALLOWED: user fixes the spelling of their own name ---'
savepoint s6;
do $$
begin
  update profiles set full_name = 'Nurse A-One' where id = '22222222-2222-2222-2222-222222222222';
  raise notice '  PASS: self name edit permitted';
exception when insufficient_privilege then
  raise notice '  FAIL: name edit blocked (too strict)';
end $$;
rollback to savepoint s6;

\echo ''
\echo '--- ATTACK 6: rename own profile AND sneak in a role change ---'
savepoint s7;
do $$
begin
  update profiles set full_name = 'X', role = 'admin'
   where id = '22222222-2222-2222-2222-222222222222';
  raise notice '  FAIL: role smuggled in alongside a permitted column';
exception when insufficient_privilege then
  raise notice '  PASS: blocked (insufficient_privilege)';
end $$;
rollback to savepoint s7;

reset role;
rollback;
