-- Doctor-authored prescription details recorded with a medication decision.
-- These records are not transmitted to a pharmacy or ordering system.
alter table public.medication_reviews
  add column prescriptions jsonb not null default '[]'::jsonb check (jsonb_typeof(prescriptions)='array'),
  add column reviewed_sections jsonb not null default '[]'::jsonb check (jsonb_typeof(reviewed_sections)='array');
notify pgrst, 'reload schema';
