-- A singular age alongside the band.
--
-- `age_band` is not repurposed and not dropped: the equity views in migration
-- 003 group by it, and the calibration model keys its baseline distributions on
-- it ("18-39", "40-64", "65-79", "80+"). Those are genuinely band-shaped
-- questions -- "is escalation equitable across age groups" is not asked of a
-- single year -- so the two columns coexist and mean different things.
--
-- Note this is less de-identifying than banding alone. Fine for synthetic demo
-- patients; worth a second look before any real cohort is loaded.

alter table public.patients
  add column if not exists age smallint
    check (age is null or age between 0 and 130);

comment on column public.patients.age is
  'Age in years. NULL = not recorded. Display field; age_band remains the grouping key for equity metrics and calibration.';
