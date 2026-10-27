-- Salus recommends a medication class; the nurse accepts it or counters.
--
-- Before this, the nurse picked from a dropdown and nothing recorded whether
-- the app had an opinion or what the nurse did with it. That makes the one
-- genuinely measurable thing about a recommendation engine -- how often the
-- person at the bedside disagrees with it -- unrecoverable after the fact.
--
-- All three columns are written server-side in the create handler, never from
-- the request body. A client-supplied `nurse_action` would let the interesting
-- number be whatever the client felt like reporting, the same reasoning that
-- keeps `escalations.acknowledged_at` out of client hands.

alter table public.medication_reviews
  -- What the rules recommended at the moment the nurse sent the request.
  -- NULL means Salus had no recommendation: either the observation was in
  -- range, or it was a low reading this vocabulary cannot answer. It does not
  -- mean the nurse ignored one.
  add column if not exists recommended_class_id uuid
    references public.medication_classes(id),
  -- The observation band that produced the recommendation (watch/high/
  -- critical), kept so a decision can be re-read without re-deriving it from
  -- vitals that have since moved.
  add column if not exists recommended_band text
    check (recommended_band is null
           or recommended_band in ('watch', 'high', 'critical')),
  -- Which rule set produced it. The rules are placeholders; when they are
  -- replaced by a reviewed protocol, rows written under the old version stay
  -- interpretable instead of silently appearing to follow the new one.
  add column if not exists recommendation_rules_version text,
  add column if not exists nurse_action text not null default 'none'
    check (nurse_action in (
      'accepted',    -- Salus recommended a class and the nurse sent that class
      'countered',   -- Salus recommended a class and the nurse sent a different one
      'declined',    -- Salus recommended a class and the nurse sent none at all
      'unprompted',  -- Salus had no recommendation; the nurse suggested a class anyway
      'none'         -- no recommendation and no suggested class: free-text request
    ));

comment on column public.medication_reviews.nurse_action is
  'Derived server-side by comparing suggested_class_id against the recommendation computed at send time. Never accepted from the client.';

comment on column public.medication_reviews.recommended_class_id is
  'NULL = Salus had no recommendation, which is not the same as the nurse declining one.';

-- Rows written before the recommendation existed keep nurse_action = 'none'
-- rather than being backfilled to 'unprompted'. The app had no opinion to
-- accept or counter at the time, and inventing one would put agreement data
-- into the record that nobody generated.
