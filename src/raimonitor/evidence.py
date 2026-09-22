"""Evidence-quality guards for windowed metrics.

Mirrors the spirit of opsaudit's evidence-quality design: metrics are
signals for human review, and a rate computed on too little data is worse
than no rate at all.

Two guards:

1. ``min_group_n`` — a per-(group attribute, value) sample-size floor.
   Group values below the floor are flagged ``insufficient_evidence`` and
   their rates are reported as ``None`` rather than noisy point estimates.
   They are excluded from DIR and gap computations, and DIR itself is
   ``None`` when fewer than two group values carry sufficient evidence.
2. Bootstrap confidence intervals — 95% intervals (2.5th/97.5th
   percentiles) on windowed decision rates (overall and per group value)
   and on per-group TPR/FPR, from plain resampling with replacement. All
   randomness comes from an explicitly seeded ``random.Random`` that the
   metrics engine consumes in a fixed (sorted) order, so results are
   deterministic run to run.

The guards never invent data: a missing rate is an explicit ``None`` with
a flag, never a silent default — and insufficient-evidence rates cannot
trip alert rules, because a ``None`` observation never fires.
"""

from __future__ import annotations

import random

CI_LOW_Q = 0.025
CI_HIGH_Q = 0.975


def bootstrap_rate_ci(flags: list[int], reps: int,
                      rng: random.Random) -> tuple[float | None, float | None]:
    """Bootstrap 95% CI for a rate from 0/1 indicator flags.

    ``flags`` are 1/0 per observation (e.g. 1 when the decision was
    positive). Resamples ``reps`` times with replacement using ``rng`` and
    returns the (2.5th, 97.5th) percentiles of the resampled means. Returns
    ``(None, None)`` when ``reps`` is 0 or there are no flags.
    """
    if reps <= 0 or not flags:
        return (None, None)
    n = len(flags)
    estimates = []
    for _ in range(reps):
        total = 0
        for _ in range(n):
            total += flags[rng.randrange(n)]
        estimates.append(total / n)
    estimates.sort()
    lo = estimates[max(0, int(CI_LOW_Q * reps))]
    hi = estimates[min(reps - 1, int(CI_HIGH_Q * reps))]
    return (lo, hi)


def new_rng(seed: int) -> random.Random:
    """Return a deterministically seeded RNG for bootstrap resampling."""
    return random.Random(seed)


def window_rng(bootstrap_seed: int, window_start: str, system: str) -> random.Random:
    """Deterministic per-(window, system) RNG for bootstrap CIs.

    Seeding per window (rather than sharing one RNG across the whole
    document) keeps CIs identical whether windows are computed in one
    batch run or incrementally by the live server — the seed, not the
    processing order, determines the resamples.
    """
    return new_rng(f"raimonitor-ci|{bootstrap_seed}|{window_start}|{system}")
