# Escalation logic — two open findings

Found during live end-to-end verification. **Not fixed** — handing these to you
rather than changing `risk.py` under you.

## 1. Stale worry fires escalation rules as if fresh

`app/services/risk.py`, `worry_component()`:

```python
decay = 0.5 ** (age_minutes / WORRY_HALF_LIFE_MINUTES)
intensity = (level / 2.0) * decay
if intensity > best:
    best = intensity
peak_level = max(peak_level, level)   # <-- never decayed, no time bound
```

The returned *intensity* decays correctly and feeds the score. But
`_decide_escalation()` branches on `peak_level`, which is the raw maximum over
whatever `recent_worry()` returned (last 20 rows, no time filter).

**Observed:** Robert Ellis has a seeded worry signal from 12 days ago. Its
decayed contribution is ≈0, but `peak_level` still reads 2, so the background
recompute escalated him on `very_worried_plus_drift` — a 12-day-old concern
paired with today's vitals drift. Nobody had tapped anything this shift.

Reproduce:
```python
from app.services.risk import compute_risk
# vitals drifting upward + one level-2 worry row with created_at 12 days ago
# -> should_escalate True, rule 'very_worried_plus_drift'
```

Worth deciding: should the escalation decision use a recency-bounded peak, the
decayed intensity against a threshold, or the most recent signal only? They
behave differently when a nurse de-escalates with a level-0 tap.

## 2. Deduped escalations report the wrong reason to the nurse

`app/api/worry.py` computes `escalated = escalation is not None`.

`escalation.open_escalation()` returns `None` in two very different cases:

- the fusion rules did not fire, **and**
- the rules did fire but `has_open_escalation()` is already True (dedupe)

Both currently produce:

> "Recorded and flagged on the charge nurse's board. Vitals are currently
> stable, so no page was sent."

In the dedupe case that message is false on both counts — the vitals were not
stable and a page already exists. A nurse taps during a real deterioration and
is told it was nothing.

Suggested shape: have `open_escalation()` distinguish *created* from
*suppressed-duplicate*, and give the duplicate case its own message naming the
existing escalation and who is handling it.

---

Neither is reachable from the UI demo path as long as the seeded 12-day-old
worry signals exist. If you want a clean demo before fixing, clear them:

```sql
delete from worry_signals
where created_at < now() - interval '1 day';
```
(That also empties the metrics/equity history, so do it only if you are
demoing the live tap rather than the dashboards.)
