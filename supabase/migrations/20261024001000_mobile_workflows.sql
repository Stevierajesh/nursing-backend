alter table public.vitals add column glucose_mg_dl integer check (glucose_mg_dl between 1 and 2000);

create or replace function private.is_org_wide()
returns boolean language sql stable security definer
set search_path = public, pg_temp as $$
  select coalesce((select role::text in ('admin', 'charge_nurse', 'prescriber')
    from public.profiles where id = auth.uid()), false)
$$;

create table public.unit_beds (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.organizations(id) on delete cascade,
  unit_id uuid not null references public.units(id) on delete cascade,
  room text not null check (length(trim(room)) between 1 and 40),
  bed text not null check (length(trim(bed)) between 1 and 40),
  unique (org_id, unit_id, bed)
);

-- Explicit synthetic demo assignments, never inferred for other organizations.
update public.patients p set bed = demo.bed
from (values ('412','12'), ('408','8'), ('415','15'), ('403','3'),
             ('410','10'), ('601','1'), ('604','4'), ('607','7')) as demo(room,bed)
where p.room = demo.room and p.bed is null and p.org_id in
  (select id from public.organizations where slug = 'mercy-general');

insert into public.unit_beds (org_id, unit_id, room, bed)
select distinct org_id, unit_id, room, bed from public.patients
where unit_id is not null and room is not null and bed is not null
on conflict (org_id, unit_id, bed) do nothing;

insert into public.unit_beds (org_id, unit_id, room, bed)
select u.org_id, u.id, case when u.name ilike '%4%' then '421' else '608' end,
  case when u.name ilike '%4%' then '21' else '8' end
from public.units u join public.organizations o on o.id=u.org_id
where o.slug='mercy-general'
on conflict (org_id, unit_id, bed) do nothing;

create unique index patients_active_bed_unique on public.patients (org_id, unit_id, bed)
where discharged_at is null and bed is not null and unit_id is not null;

create table public.medication_reviews (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.organizations(id) on delete cascade,
  patient_id uuid not null references public.patients(id) on delete cascade,
  requested_by uuid not null references public.profiles(id),
  prescriber_id uuid not null references public.profiles(id),
  condition text not null check (condition in ('blood_pressure','blood_sugar','heart_rate')),
  status text not null default 'pending' check (status in ('pending','reviewed','dismissed')),
  request_note text not null check (length(trim(request_note)) between 1 and 2000),
  review_note text,
  vitals_snapshot jsonb not null default '{}',
  created_at timestamptz not null default now(),
  reviewed_at timestamptz,
  reviewed_by uuid references public.profiles(id)
);
create index medication_reviews_org_time on public.medication_reviews (org_id,created_at desc);

alter table public.unit_beds enable row level security;
alter table public.medication_reviews enable row level security;
revoke all on public.unit_beds, public.medication_reviews from anon, authenticated;
grant select on public.unit_beds, public.medication_reviews to authenticated;
grant all on public.unit_beds, public.medication_reviews to service_role;
create policy unit_beds_read on public.unit_beds for select to authenticated
using (org_id=private.current_org_id());
create policy medication_reviews_read on public.medication_reviews for select to authenticated
using (org_id=private.current_org_id() and
  (private.current_app_role()::text in ('admin','charge_nurse')
   or (private.current_app_role()::text='prescriber' and prescriber_id=auth.uid())
   or (private.current_app_role()::text='nurse' and private.is_assigned(patient_id))));
