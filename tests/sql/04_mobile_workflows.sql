begin;
do $$ begin
 if (select bed from patients where id='99999999-2222-0000-0000-000000000001') is distinct from '12' then
   raise exception 'Demo bed backfill failed';
 end if;
 if (select count(*) from unit_beds where org_id='99999999-0000-0000-0000-000000000001')<>2 then
   raise exception 'Demo inventory or vacant bed missing';
 end if;
end $$;
insert into auth.users (id,email) values
 ('55555555-5555-5555-5555-555555555555','doc1@example.com'),
 ('66666666-6666-6666-6666-666666666666','doc2@example.com');
insert into profiles (id,org_id,role,full_name,email) values
 ('55555555-5555-5555-5555-555555555555','aaaaaaaa-0000-0000-0000-000000000001','prescriber','Doc One','doc1@example.com'),
 ('66666666-6666-6666-6666-666666666666','aaaaaaaa-0000-0000-0000-000000000001','prescriber','Doc Two','doc2@example.com');
insert into medication_reviews (org_id,patient_id,requested_by,prescriber_id,condition,request_note) values
 ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-2222-0000-0000-000000000001','22222222-2222-2222-2222-222222222222','55555555-5555-5555-5555-555555555555','blood_pressure','Review A1'),
 ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-2222-0000-0000-000000000002','33333333-3333-3333-3333-333333333333','66666666-6666-6666-6666-666666666666','heart_rate','Review A2'),
 ('bbbbbbbb-0000-0000-0000-000000000002','bbbbbbbb-2222-0000-0000-000000000003','44444444-4444-4444-4444-444444444444','44444444-4444-4444-4444-444444444444','blood_sugar','Review B');
insert into unit_beds (org_id,unit_id,room,bed) values
 ('aaaaaaaa-0000-0000-0000-000000000001','aaaaaaaa-1111-0000-0000-000000000001','101','1'),
 ('bbbbbbbb-0000-0000-0000-000000000002','bbbbbbbb-1111-0000-0000-000000000002','201','1');
update patients set bed='1' where id='aaaaaaaa-2222-0000-0000-000000000001'::uuid;
do $$ begin
  begin
    update patients set bed='1' where id='aaaaaaaa-2222-0000-0000-000000000002';
    raise exception 'Occupied bed accepted';
  exception when unique_violation then null; end;
end $$;
set local role authenticated;
select set_config('test.current_user_id','22222222-2222-2222-2222-222222222222',true);
do $$ begin
 if (select count(*) from medication_reviews)<>1 then raise exception 'Nurse medication scope leaked'; end if;
 if (select count(*) from unit_beds)<>1 then raise exception 'Bed inventory leaked across orgs'; end if;
 begin
  update medication_reviews set status='reviewed';
  raise exception 'Client forged prescriber review';
 exception when insufficient_privilege then null; end;
 begin
  truncate medication_reviews;
  raise exception 'Client truncated medication reviews';
 exception when insufficient_privilege then null; end;
 begin
  update unit_beds set bed='99';
  raise exception 'Client edited bed inventory';
 exception when insufficient_privilege then null; end;
end $$;
select set_config('test.current_user_id','55555555-5555-5555-5555-555555555555',true);
do $$ begin
 if (select count(*) from medication_reviews)<>1 then raise exception 'Prescriber queue leaked'; end if;
 if exists(select 1 from medication_reviews where prescriber_id<>auth.uid()) then raise exception 'Wrong prescriber queue'; end if;
 raise notice 'PASS: bed uniqueness, medication assignment/org isolation, and direct-write restrictions';
end $$;
reset role;
rollback;
