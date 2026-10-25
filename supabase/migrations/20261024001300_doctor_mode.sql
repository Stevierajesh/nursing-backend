-- Doctor mode: the approval review page and two-level medication escalation.
--
-- The nurse suggests a medication class; an assigned prescriber approves or
-- rejects it. If nobody responds inside the SLA the request escalates from one
-- named prescriber to every prescriber in the org -- that widening *is* the
-- second level. There is deliberately no "charge physician" role: adding a
-- fourth clinical role to model one timeout would be a worse trade than
-- widening visibility on the row that timed out.
--
-- Nothing here places an order. The flow ends at "approved", recorded in Salus.

-- ---------------------------------------------------------------------------
-- 1. Nurse notes. Review section 4 had no table behind it.
-- ---------------------------------------------------------------------------

create table if not exists public.nurse_notes (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references public.organizations(id) on delete cascade,
  patient_id uuid not null references public.patients(id) on delete cascade,
  author_id uuid not null references public.profiles(id),
  body text not null check (length(trim(body)) between 1 and 4000),
  recorded_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

-- The review page reads the most recent notes for one patient, newest first.
create index if not exists nurse_notes_patient_recent_idx
  on public.nurse_notes (patient_id, recorded_at desc);

alter table public.nurse_notes enable row level security;

-- GRANT ALL carries TRUNCATE and TRUNCATE ignores RLS, so revoke first.
revoke all on public.nurse_notes from anon, authenticated;
grant select on public.nurse_notes to authenticated;
grant all on public.nurse_notes to service_role;

create policy nurse_notes_read on public.nurse_notes for select to authenticated
using (org_id = private.current_org_id() and
  (private.is_org_wide() or private.is_assigned(patient_id)));

-- ---------------------------------------------------------------------------
-- 2. Patient history. Review section 5, four free-text fields.
-- ---------------------------------------------------------------------------

alter table public.patients
  add column if not exists past_history text,
  add column if not exists home_medications text,
  add column if not exists prior_admissions text,
  add column if not exists past_responses text;

-- Same null discipline as allergies: absence is "not recorded", never "none".
-- A prescriber ticking "I have reviewed this section" must be able to tell the
-- difference between a patient with no prior admissions and one nobody asked.
comment on column public.patients.past_history is
  'NULL = not recorded. Free text; not a coded problem list.';
comment on column public.patients.home_medications is
  'NULL = not recorded. Free text; not a reconciled medication list.';
comment on column public.patients.prior_admissions is
  'NULL = not recorded. Free text summary, not a linked admission record.';
comment on column public.patients.past_responses is
  'NULL = not recorded. How this patient responded to previous treatment.';

-- ---------------------------------------------------------------------------
-- 3. Pain. Charted on the vitals row, scored outside NEWS2.
-- ---------------------------------------------------------------------------

alter table public.vitals
  add column if not exists pain_score smallint
    check (pain_score is null or pain_score between 0 and 10);

-- NEWS2 does not score pain and this column must never be fed into news_score.
-- It is banded separately against the 0-10 numeric rating scale so the review
-- page can show a badge without implying the badge came from NEWS2.
comment on column public.vitals.pain_score is
  'Numeric rating scale 0-10. NOT a NEWS2 parameter; excluded from news_score.';

-- ---------------------------------------------------------------------------
-- 4. Medication review approval states and the escalation clock.
-- ---------------------------------------------------------------------------

alter table public.organizations
  add column if not exists medication_sla_minutes int not null default 15
    check (medication_sla_minutes between 1 and 240);

comment on column public.organizations.medication_sla_minutes is
  'Minutes a pending medication review may sit before it escalates to every prescriber in the org.';

alter table public.medication_reviews
  add column if not exists sla_deadline timestamptz,
  add column if not exists acknowledged_at timestamptz,
  add column if not exists acknowledged_by uuid references public.profiles(id),
  add column if not exists escalated_at timestamptz,
  add column if not exists escalated_to uuid references public.profiles(id),
  add column if not exists decided_at timestamptz,
  add column if not exists decided_by uuid references public.profiles(id),
  add column if not exists decision_note text;

-- 'reviewed' is retained rather than rewritten to 'approved': a row closed
-- under the old two-state flow recorded that someone looked, not that they
-- agreed. Backfilling it to 'approved' would invent a clinical decision.
alter table public.medication_reviews
  drop constraint if exists medication_reviews_status_check;

alter table public.medication_reviews
  add constraint medication_reviews_status_check check (status in (
    'pending',     -- waiting on the assigned prescriber
    'escalated',   -- SLA breached; now visible to every prescriber in the org
    'approved',    -- prescriber agreed with the nurse's suggested class
    'rejected',    -- prescriber declined it, with a reason
    'dismissed',   -- closed without a clinical decision
    'reviewed'     -- legacy, pre-approval-states; do not write
  ));

comment on column public.medication_reviews.status is
  'pending|escalated are open; approved|rejected|dismissed are closed. "reviewed" is legacy and must not be written by new code.';

-- The sweep scans exactly this: open rows whose deadline has passed.
create index if not exists medication_reviews_sla_idx
  on public.medication_reviews (sla_deadline)
  where status = 'pending';

-- Backfill a deadline for rows written before the clock existed, so the sweep
-- does not treat every historical pending row as instantly overdue.
update public.medication_reviews r
set sla_deadline = r.created_at + make_interval(mins => o.medication_sla_minutes)
from public.organizations o
where o.id = r.org_id and r.sla_deadline is null;

-- ---------------------------------------------------------------------------
-- 5. Widen read access on escalation.
-- ---------------------------------------------------------------------------

-- Replaces the policy from 20261024001000. The only change is the prescriber
-- arm: an escalated row is readable by any prescriber in the org, which is how
-- the second level reaches someone without a second role existing.
drop policy if exists medication_reviews_read on public.medication_reviews;

create policy medication_reviews_read on public.medication_reviews
for select to authenticated
using (org_id = private.current_org_id() and
  (private.current_app_role()::text in ('admin','charge_nurse')
   or (private.current_app_role()::text = 'prescriber'
       and (prescriber_id = auth.uid()
            or escalated_to = auth.uid()
            or status = 'escalated'))
   or (private.current_app_role()::text = 'nurse' and private.is_assigned(patient_id))));
