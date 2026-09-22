# Real-world validation — UCI Adult production-feed simulation

**Script:** `examples/adult_validation.py`
**Run command:** `python3 examples/adult_validation.py` (downloads the dataset on first run)
**Validated on:** 2026-09-22

## Methodology

- **Dataset:** UCI Adult / Census Income
  (https://archive.ics.uci.edu/dataset/2/adult, retrieved 2026-09-22).
  48,842 rows; deterministic prep (strip whitespace, drop rows with `?` in
  the used columns) keeps **32,561** rows.
- **Simulation:** rows deterministically shuffled (seed 42), streamed as
  **500 decisions/day over 60 days** starting 2026-01-01, as system
  `income-screen-v1`. Timestamps are synthetic; the people and labels are real.
- **Predictor:** a FIXED rule-based policy — no model training, keeping the
  focus on monitoring rather than modeling:
  - Days 1–40: predict `>50K` iff `education-num >= 13` AND `hours-per-week >= 40`.
  - Days 41–60: same rule, but the hours bar rises to `>= 55` for `sex=Female`.
  
  The day-41 change emulates a real deployment event: an innocent-looking
  threshold tweak that disparately impacts one group.
- **Ground truth:** the actual income label. **Groups:** `sex`, `race`.
- **Pipeline:** `raimonitor run --window 7D` with rules `dir-below-80`
  (sex, DIR < 0.8, persistence 2, critical) and `tpr-gap-wide`
  (sex, TPR gap > 0.2, persistence 2, warning).

## Observed results (2026-09-22 run)

30,000 decisions ingested; 9 weekly windows (last window partial, 2,000 events).

| window_start | n    | DIR(sex) |
|--------------|------|----------|
| 2026-01-01   | 3500 | 0.685 |
| 2026-01-08   | 3500 | 0.646 |
| 2026-01-15   | 3500 | 0.713 |
| 2026-01-22   | 3500 | 0.704 |
| 2026-01-29   | 3500 | 0.686 |
| 2026-02-05   | 3500 | 0.549 |
| 2026-02-12   | 3500 | 0.084 |
| 2026-02-19   | 3500 | 0.106 |
| 2026-02-26   | 2000 | 0.131 |

**Incidents raised: 10.**
- `dir-below-80` (critical): 8 incidents, windows 2026-01-08 → 2026-02-26.
  The baseline policy already carries disparate impact on real data
  (DIR 0.65–0.71), so the rule fires from the second window — a true
  positive, not a false alarm. After the day-41 policy change DIR collapses
  to 0.08–0.13 and the rule keeps firing.
- `tpr-gap-wide` (warning): 2 incidents, windows 2026-02-19 and 2026-02-26
  (observed gaps 0.361, 0.382) — the drift-specific signal, firing only
  after the policy change persists.

## Interpretation

The dashboard catches **both** failure modes an operator cares about: the
standing disparity baked into the original policy (visible from week two)
and the deployment regression (the cliff at the February windows, plus the
new TPR-gap signal). The full dashboard for this run renders at
`examples/adult_out/pipeline/report.html` (generated artifact, git-ignored).

## Limitations of this validation

- Timestamps are synthetic; the simulation tests the monitoring machinery,
  not a real deployment timeline.
- The drift is injected and deliberately stark; subtler real-world drift
  would exercise the PSI path more than the threshold rules.
- The predictor is a fixed rule, so "model" behavior is fully known — a
  learned model would add its own dynamics.
