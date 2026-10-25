-- "Request more info from nurse" on the approval review (design doc p30-32).
--
-- Its own columns rather than reusing decision_note: a question back to the
-- nurse is not a decision, and overloading the decision field would make an
-- undecided request look closed in any query that reads it.
--
-- The request stays OPEN. The prescriber has not answered, so the escalation
-- clock keeps running for the same reason acknowledging does not stop it --
-- if the nurse never replies, this still has to reach somebody.

alter table public.medication_reviews
  add column if not exists info_requested_at timestamptz,
  add column if not exists info_requested_by uuid references public.profiles(id),
  add column if not exists info_request_note text;

comment on column public.medication_reviews.info_request_note is
  'A prescriber question back to the requesting nurse. Does not close the request and does not pause the SLA.';
