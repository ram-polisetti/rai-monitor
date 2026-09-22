"""Distribution-drift detection.

Two complementary signals:

1. **PSI (Population Stability Index)** — compares the decision distribution
   (and each numeric feature's binned distribution) of a current window
   against a baseline window::

       PSI = sum((actual% - expected%) * ln(actual% / expected%))

   Conventional interpretation (documented, not a verdict):
   PSI < 0.1 → no significant shift; 0.1–0.25 → moderate shift, investigate;
   PSI > 0.25 → significant shift.
2. **Decision-rate shift** — the absolute change in the positive-decision
   rate between baseline and current windows. Simple, interpretable, and
   hard to game with binning choices.

Both are deterministic. Feature PSI uses decile bins derived from the
baseline window so bin edges cannot shift with the current data.
"""

from __future__ import annotations

import math

from .schema import is_positive

_EPS = 1e-6


def _proportions(counts: list[int]) -> list[float]:
    total = sum(counts)
    if total == 0:
        return [0.0] * len(counts)
    return [c / total for c in counts]


def psi(expected_counts: list[int], actual_counts: list[int]) -> float:
    """Population Stability Index between two binned distributions."""
    if len(expected_counts) != len(actual_counts):
        raise ValueError("expected and actual must have the same number of bins")
    total = 0.0
    for exp_p, act_p in zip(_proportions(expected_counts),
                            _proportions(actual_counts)):
        exp_p = max(exp_p, _EPS)
        act_p = max(act_p, _EPS)
        total += (act_p - exp_p) * math.log(act_p / exp_p)
    return total


def _decile_edges(values: list[float]) -> list[float]:
    ordered = sorted(values)
    n = len(ordered)
    edges = []
    for q in range(1, 10):
        edges.append(ordered[min(int(q * n / 10), n - 1)])
    # de-duplicate while preserving order; need at least 2 distinct edges
    unique = list(dict.fromkeys(edges))
    return unique if len(unique) >= 2 else [ordered[0], ordered[-1]]


def _bin_counts(values: list[float], edges: list[float]) -> list[int]:
    counts = [0] * (len(edges) + 1)
    for value in values:
        placed = False
        for i, edge in enumerate(edges):
            if value <= edge:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return counts


def _numeric_feature_values(events: list[dict], name: str) -> list[float]:
    values = []
    for event in events:
        raw = (event.get("features") or {}).get(name)
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return values


def detect_drift(baseline_events: list[dict], current_events: list[dict],
                 positive_label: str = "1",
                 features: list[str] | None = None) -> dict:
    """Compare a current window against a baseline window.

    Returns ``{"decision_psi", "decision_rate_shift", "feature_psi": {...},
    "n_baseline", "n_current"}``. ``features`` names numeric features to PSI;
    when omitted, all numeric features present in the baseline are used.
    """
    if not baseline_events:
        raise ValueError("baseline window is empty")
    if not current_events:
        raise ValueError("current window is empty")

    base_pos = sum(1 for e in baseline_events if is_positive(e, positive_label))
    curr_pos = sum(1 for e in current_events if is_positive(e, positive_label))
    decision_psi = psi([base_pos, len(baseline_events) - base_pos],
                       [curr_pos, len(current_events) - curr_pos])
    base_rate = base_pos / len(baseline_events)
    curr_rate = curr_pos / len(current_events)

    if features is None:
        features = sorted({
            name for e in baseline_events for name, v in (e.get("features") or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        })
    feature_psi: dict[str, float | None] = {}
    for name in features:
        base_values = _numeric_feature_values(baseline_events, name)
        curr_values = _numeric_feature_values(current_events, name)
        if len(base_values) < 10 or not curr_values:
            feature_psi[name] = None
            continue
        edges = _decile_edges(base_values)
        feature_psi[name] = psi(_bin_counts(base_values, edges),
                                _bin_counts(curr_values, edges))

    return {
        "decision_psi": decision_psi,
        "decision_rate_shift": curr_rate - base_rate,
        "baseline_decision_rate": base_rate,
        "current_decision_rate": curr_rate,
        "feature_psi": feature_psi,
        "n_baseline": len(baseline_events),
        "n_current": len(current_events),
    }
