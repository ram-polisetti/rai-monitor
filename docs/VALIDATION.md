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

---

# Session 2 — evidence guards and live-server parity (2026-09-22)

Same dataset, feed, and policy change as above. The script now runs two
additional phases (see `run_guards_and_live_server` in
`examples/adult_validation.py`).

## Phase 1 — evidence guards (`--min-group-n 100 --bootstrap 100 --bootstrap-seed 42`)

**Observed:**

- Flagged as insufficient evidence (rates reported as `None`, excluded
  from DIR): `race=Amer-Indian-Eskimo` in all 9 windows,
  `race=Other` in all 9 windows, `race=Asian-Pac-Islander` in 2 windows —
  exactly the small groups in a 3,500-event week.
- Example CI: window 2026-01-01, `sex=Male` decision rate **0.226**,
  95% CI **(0.212, 0.242)** (100 reps, seed 42).
- Incidents with guards: **10** — identical to the unguarded run. The
  guards suppress noisy rates on tiny groups without changing rule
  outcomes on groups with sufficient evidence.

## Phase 2 — live-server parity

`raimonitor serve --port 18080 --interval 1` tailed the same
`decisions.csv` (30,000 events) with the same guard settings.

**Observed:**

- Server consumed all **30,000** events.
- `GET /api/metrics` **byte-identical to the batch `metrics.json`** —
  streaming ingestion reproduces the batch pipeline exactly (this holds
  because bootstrap CIs use per-(window, system) seeded RNGs, so results
  do not depend on processing order).
- Live incidents: **10**, matching the batch run.
- Server restart with the same state dir: events still 30,000, incidents
  still 10 — **no duplicates**, the watermark resume works.

## Limitations of the session-2 validation

- The live-server test streams a static file rather than a truly live
  producer; it validates tailing/parity/restart, not backpressure under
  high event rates.
- Bootstrap CIs on 3,500-event windows cost ~10 s per full run at 100
  reps — acceptable for monitoring windows, but the rep count should be
  tuned for very large windows.

---

# Session 3 — CUSUM change-point detection and signed incident log (2026-09-22)

Same dataset, feed, and policy change as above. The script now runs a
third phase (see `run_session3_checks` in `examples/adult_validation.py`).

## Phase 3a — CUSUM over the DIR(sex) history

`raimonitor drift --metrics pipeline/metrics.json --metric dir
--group-attr sex` over the 9 weekly DIR(sex) values
(0.685, 0.646, 0.713, 0.704, 0.686, 0.549, 0.084, 0.106, 0.131).

**Observed:**

- Change points found: **1**.
- First change point: `window_start=2026-02-05`, `direction=down`,
  `value=0.549`, `previous_value=0.686`.
- The script asserts the first change point is exactly the 2026-02-05
  window — the first 7-day window containing day 41 of the feed
  (2026-02-10). CUSUM localized the injected policy change to the
  correct window, one window before the full collapse (DIR 0.08–0.13)
  is visible.

## Phase 3b — tamper-evident incident log

`raimonitor verify-log --incidents pipeline/incidents.jsonl`.

**Observed:**

- Exit code **0**; hash chain intact: **10 signed records**,
  0 legacy unsigned, `errors=[]`.
- All 10 incidents raised by the batch pipeline verify as an unbroken
  SHA-256 chain.

## Limitations of the session-3 validation

- The injected drift is stark (DIR 0.69 → 0.08); subtler real-world
  drift would exercise CUSUM's sensitivity parameters (k, h) more than
  this binary before/after.
- Verification covers the batch-written log; a live-streamed log under
  concurrent appends is exercised by the unit tests (watermark resume,
  no duplicate incidents) rather than this script.
