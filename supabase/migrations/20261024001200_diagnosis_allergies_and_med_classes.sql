-- Diagnosis and allergies on patients, plus a curated medication-class
-- vocabulary so a nurse can suggest a class rather than the app recommending
-- a drug.

alter table public.patients
  add column if not exists diagnosis text,
  add column if not exists allergies text[];

-- The null/empty distinction is the whole point of using an array here. A
-- blank allergies field must never render as "no known allergies": on a
-- bedside screen a nurse reads absence as reassurance.
comment on column public.patients.allergies is
  'NULL = not recorded. Empty array = none known, recorded deliberately.';

comment on column public.patients.diagnosis is
  'Free text working diagnosis. Not a coded term; do not treat as structured.';

-- Vocabulary lives in data rather than a check constraint or a frontend
-- constant, so whoever owns the protocol can curate it without a deploy, and
-- every row carries where it came from.
create table if not exists public.medication_classes (
  id uuid primary key default gen_random_uuid(),
  condition text not null
    check (condition in ('blood_pressure','blood_sugar','heart_rate')),
  label text not null check (length(trim(label)) between 1 and 120),
  -- Attribution. A row with no source has not been clinically reviewed and
  -- the UI is expected to say so.
  source text,
  active boolean not null default true,
  sort_order int not null default 0,
  created_at timestamptz not null default now(),
  unique (condition, label)
);

-- The nurse's suggestion. Nullable: every existing review predates this, and
-- a free-text-only request stays valid.
alter table public.medication_reviews
  add column if not exists suggested_class_id uuid
    references public.medication_classes(id);

alter table public.medication_classes enable row level security;

-- Revoke everything first: GRANT ALL carries TRUNCATE, and TRUNCATE ignores
-- RLS entirely. Same pattern as the other tables in this schema.
revoke all on public.medication_classes from anon, authenticated;
grant select on public.medication_classes to authenticated;
grant all on public.medication_classes to service_role;

create policy medication_classes_read on public.medication_classes
  for select to authenticated using (active);

-- Starter vocabulary. `source` is deliberately the same marker on every row:
-- these groupings are textbook-level, but nobody clinical has signed off on
-- this list for this unit. Replace source with the protocol or formulary it
-- comes from once that review has happened.
insert into public.medication_classes (condition, label, sort_order, source)
values
  ('blood_pressure', 'ACE inhibitor',          1, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_pressure', 'ARB',                    2, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_pressure', 'Calcium channel blocker',3, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_pressure', 'Thiazide diuretic',      4, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_pressure', 'Beta blocker',           5, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_sugar',    'Rapid-acting insulin',   1, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_sugar',    'Long-acting insulin',    2, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_sugar',    'Biguanide',              3, 'UNREVIEWED — placeholder vocabulary'),
  ('blood_sugar',    'Sulfonylurea',           4, 'UNREVIEWED — placeholder vocabulary'),
  ('heart_rate',     'Beta blocker',           1, 'UNREVIEWED — placeholder vocabulary'),
  ('heart_rate',     'Calcium channel blocker',2, 'UNREVIEWED — placeholder vocabulary'),
  ('heart_rate',     'Antiarrhythmic',         3, 'UNREVIEWED — placeholder vocabulary')
on conflict (condition, label) do nothing;
