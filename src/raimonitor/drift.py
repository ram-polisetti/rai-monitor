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
_DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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


# ---------------------------------------------------------------------------
# Full-history drift: CUSUM change-point detection + day-of-week seasonality
#
# PSI (above) compares two windows. CUSUM instead walks the *whole* metric
# history and flags the points where the series' level shifts — the right
# tool for "when did this metric regime change?" over weeks of windows.
#
# Method (two-sided tabular CUSUM with a Phase-I reference sample):
#   - the first ``baseline_n`` values (default ``max(3, n // 4)``) define the
#     in-control mean and std;
#   - z_i = (x_i - mean) / std;  S+_i = max(0, S+_{i-1} + z_i - k),
#                                 S-_i = min(0, S-_{i-1} + z_i + k);
#   - when |S| first exceeds ``h`` an alarm fires. The reported change point
#     is the last index where the cumulative sum was zero, plus one — i.e.
#     the first window of the new regime (the standard CUSUM estimator).
#   - after an alarm the reference is re-baselined on the new regime, so a
#     sustained shift raises exactly one alarm instead of re-firing.
# Parameters: k (slack, in std units — shifts smaller than k are ignored)
# and h (decision threshold, in std units). Defaults k=0.5, h=5 follow the
# textbook recommendation for detecting ~1-sigma level shifts. A flat
# baseline (zero variance) falls back to the full-series std; a flat whole
# series raises ValueError since there is nothing to detect.
#
# Day-of-week seasonality: decision volumes and rates often follow a weekly
# rhythm (weekday vs weekend traffic). ``dow_baseline`` learns one mean per
# weekday from history; ``deseasonalize`` subtracts it, so CUSUM sees the
# underlying level rather than the calendar. Weekdays with fewer than two
# observations fall back to the global mean (documented, not silent).


def _parse_ts(ts: str):
    from datetime import datetime
    text = ts.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _std(values: list[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def _near_zero(std: float, scale: float) -> bool:
    return std <= 1e-9 * max(1.0, abs(scale))


def cusum(values: list[float], k: float = 0.5, h: float = 5.0,
          baseline_n: int | None = None) -> list[int]:
    """Two-sided tabular CUSUM change-point indices for a value series.

    The first ``baseline_n`` values (default ``max(3, n // 4)``) are the
    in-control reference. Returns the sorted indices of the first window of
    each detected new regime. Deterministic. Raises ``ValueError`` on fewer
    than 3 values, a degenerate ``baseline_n``, or (near-)zero variance
    across the whole series.
    """
    n = len(values)
    if n < 3:
        raise ValueError("cusum needs at least 3 values")
    ref_n = baseline_n if baseline_n is not None else max(3, n // 4)
    if not 1 <= ref_n < n:
        raise ValueError("baseline_n must satisfy 1 <= baseline_n < len(values)")
    mean = sum(values[:ref_n]) / ref_n
    std = _std(values[:ref_n])
    if _near_zero(std, mean):
        std = _std(values)  # flat baseline: use full-series spread
        if _near_zero(std, sum(values) / n):
            raise ValueError("cusum needs non-zero variance")
    points: list[int] = []
    s_pos = s_neg = 0.0
    last_zero_pos = last_zero_neg = ref_n - 1
    i = ref_n
    while i < n:
        z = (values[i] - mean) / std
        s_pos = max(0.0, s_pos + z - k)
        s_neg = min(0.0, s_neg + z + k)
        if s_pos == 0.0:
            last_zero_pos = i
        if s_neg == 0.0:
            last_zero_neg = i
        if s_pos > h or s_neg < -h:
            cp = (last_zero_pos if s_pos > h else last_zero_neg) + 1
            points.append(cp)
            # Re-baseline on the new regime so a sustained shift alarms once.
            new_ref = values[cp:i + 1]
            mean = sum(new_ref) / len(new_ref)
            new_std = _std(new_ref)
            if not _near_zero(new_std, mean):
                std = new_std
            s_pos = s_neg = 0.0
            last_zero_pos = last_zero_neg = i
        i += 1
    return points


def dow_baseline(series: list[tuple[str, float]]) -> dict[int, float]:
    """Mean value per weekday (0=Monday) from (timestamp, value) pairs.

    Weekdays seen fewer than twice fall back to the global mean, and the
    fallback is recorded in the returned ``"_fallback_weekdays"`` entry.
    """
    from collections import defaultdict
    by_dow: dict[int, list[float]] = defaultdict(list)
    for ts, value in series:
        dt = _parse_ts(ts)
        if dt is None or value is None:
            continue
        by_dow[dt.weekday()].append(value)
    all_values = [v for vals in by_dow.values() for v in vals]
    if not all_values:
        raise ValueError("no usable (timestamp, value) pairs")
    global_mean = sum(all_values) / len(all_values)
    baseline: dict[int, float] = {}
    fallback = []
    for dow in range(7):
        vals = by_dow.get(dow, [])
        if len(vals) >= 2:
            baseline[dow] = sum(vals) / len(vals)
        else:
            baseline[dow] = global_mean
            fallback.append(_DOW_NAMES[dow])
    baseline["_fallback_weekdays"] = fallback  # type: ignore[assignment]
    return baseline


def deseasonalize(series: list[tuple[str, float]],
                  baseline: dict[int, float] | None = None) -> list[float]:
    """Subtract the day-of-week baseline; returns residuals in input order."""
    baseline = baseline or dow_baseline(series)
    residuals = []
    for ts, value in series:
        dt = _parse_ts(ts)
        residuals.append(value - baseline[dt.weekday()] if dt else value)
    return residuals


def detect_change_points(windows: list[dict], metric: str,
                         group_attr: str | None = None,
                         k: float = 0.5, h: float = 5.0,
                         baseline_n: int | None = None,
                         deseasonalize_dow: bool = False) -> dict:
    """CUSUM change-point detection over a full metric history.

    ``windows`` is the ordered ``"windows"`` list of a metrics document;
    ``metric`` is one of ``dir`` (needs ``group_attr``), ``tpr_gap``,
    ``fpr_gap``, ``decision_rate``, ``accuracy``, ``volume``. Windows where
    the metric is undefined (``None``) are skipped. With
    ``deseasonalize_dow=True`` the day-of-week baseline is removed first.

    Returns ``{"metric", "n_windows", "n_usable", "change_points": [...]}``
    where each change point names the ``window_start`` of the first window
    of the new regime, the ``direction`` of the shift, and the metric values
    around it.
    """
    from .alerts import METRICS
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; choose from {METRICS}")
    if metric in ("dir", "tpr_gap", "fpr_gap") and not group_attr:
        raise ValueError(f"metric {metric!r} requires group_attr")

    def observe(window: dict):
        if metric == "dir":
            group = (window.get("groups") or {}).get(group_attr) or {}
            return group.get("disparate_impact_ratio")
        if metric in ("tpr_gap", "fpr_gap"):
            return (window.get(metric) or {}).get(group_attr)
        if metric == "decision_rate":
            return window.get("decision_rate")
        if metric == "accuracy":
            return window.get("accuracy")
        return window.get("n")

    ordered = sorted(windows, key=lambda w: (w.get("window_start", ""),
                                             w.get("system", "")))
    series = [(w["window_start"], observe(w)) for w in ordered
              if observe(w) is not None]
    result: dict = {"metric": metric, "group_attr": group_attr,
                    "n_windows": len(ordered), "n_usable": len(series),
                    "deseasonalized": deseasonalize_dow,
                    "change_points": []}
    if len(series) < 3:
        result["note"] = "fewer than 3 usable windows; no detection run"
        return result
    values = ([float(v) for _, v in series]
              if not deseasonalize_dow
              else deseasonalize([(ts, float(v)) for ts, v in series]))
    points = cusum(values, k=k, h=h, baseline_n=baseline_n)
    for idx in points:
        ts, value = series[idx]
        prev = series[idx - 1][1] if idx > 0 else None
        direction = ("down" if prev is not None and value < prev else
                     "up" if prev is not None else "unknown")
        result["change_points"].append({
            "window_start": ts, "direction": direction,
            "value": value, "previous_value": prev})
    return result
