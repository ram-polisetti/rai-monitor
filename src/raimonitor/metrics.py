"""Rolling metrics engine.

For each (system, time window) the engine computes:

- volume: event count ``n``
- decision behavior: overall positive-decision rate and per-group rates
- fairness: disparate impact ratio (DIR) per group attribute —
  ``min(group rate) / max(group rate)``; TPR/FPR gaps when outcomes exist
- performance: accuracy and precision when ground-truth outcomes exist

Everything is deterministic: identical input events always produce identical
metrics. Rates are ``None`` when undefined (e.g. DIR with fewer than two
groups, or a zero max rate) rather than silently defaulting.
"""

from __future__ import annotations

from .schema import is_positive
from .windows import bucketize, parse_window


def _rate(positives: int, total: int) -> float | None:
    return positives / total if total else None


def _group_rates(events: list[dict], attr: str, positive_label: str) -> dict[str, dict]:
    """Per-value counts and positive-decision rates for one group attribute."""
    counts: dict[str, list[int]] = {}
    for event in events:
        value = event["groups"].get(attr)
        if value is None:
            continue
        bucket = counts.setdefault(value, [0, 0])
        bucket[1] += 1
        if is_positive(event, positive_label):
            bucket[0] += 1
    return {
        value: {"n": total, "decision_rate": _rate(pos, total)}
        for value, (pos, total) in sorted(counts.items())
    }


def _dir(rates: dict[str, dict]) -> float | None:
    defined = [info["decision_rate"] for info in rates.values()
               if info["decision_rate"] is not None]
    if len(defined) < 2 or max(defined) == 0:
        return None
    return min(defined) / max(defined)


def _error_rates(events: list[dict], attr: str | None, value: str | None,
                 positive_label: str) -> dict[str, float | None]:
    """TPR/FPR over events, optionally restricted to one group value."""
    tp = fp = tn = fn = 0
    for event in events:
        if attr is not None and event["groups"].get(attr) != value:
            continue
        outcome = event.get("outcome")
        if outcome is None:
            continue
        pred_pos = is_positive(event, positive_label)
        actual_pos = outcome == positive_label
        if pred_pos and actual_pos:
            tp += 1
        elif pred_pos and not actual_pos:
            fp += 1
        elif not pred_pos and not actual_pos:
            tn += 1
        else:
            fn += 1
    return {
        "tpr": _rate(tp, tp + fn),
        "fpr": _rate(fp, fp + tn),
        "accuracy": _rate(tp + tn, tp + tn + fp + fn),
        "precision": _rate(tp, tp + fp),
        "n_scored": tp + tn + fp + fn,
    }


def _gap(values: list[float | None]) -> float | None:
    defined = [v for v in values if v is not None]
    return max(defined) - min(defined) if len(defined) >= 2 else None


def compute_window_metrics(events: list[dict], positive_label: str = "1") -> dict:
    """Compute the full metric set for one window's events (one system)."""
    if not events:
        raise ValueError("cannot compute metrics for an empty window")
    system = events[0]["system"]
    n = len(events)
    positives = sum(1 for e in events if is_positive(e, positive_label))
    attrs = sorted({attr for e in events for attr in e["groups"]})

    group_metrics: dict[str, dict] = {}
    tpr_gaps: dict[str, float | None] = {}
    fpr_gaps: dict[str, float | None] = {}
    for attr in attrs:
        rates = _group_rates(events, attr, positive_label)
        per_value = {}
        for value, info in rates.items():
            err = _error_rates(events, attr, value, positive_label)
            per_value[value] = {**info, **err}
        group_metrics[attr] = {
            "disparate_impact_ratio": _dir(rates),
            "values": per_value,
        }
        tpr_gaps[attr] = _gap([v["tpr"] for v in per_value.values()])
        fpr_gaps[attr] = _gap([v["fpr"] for v in per_value.values()])

    overall_err = _error_rates(events, None, None, positive_label)
    return {
        "system": system,
        "n": n,
        "decision_rate": _rate(positives, n),
        "accuracy": overall_err["accuracy"],
        "precision": overall_err["precision"],
        "n_scored": overall_err["n_scored"],
        "groups": group_metrics,
        "tpr_gap": tpr_gaps,
        "fpr_gap": fpr_gaps,
    }


def compute_metrics(events: list[dict], window: str = "7D",
                    positive_label: str = "1") -> dict:
    """Bucket events into windows and compute metrics per (system, window).

    Returns ``{"window": spec, "windows": [...]}`` with windows in
    chronological order. Each entry carries ``window_start`` plus the output
    of :func:`compute_window_metrics`. Systems are kept separate: metrics are
    computed per system within each window.
    """
    parsed = parse_window(window)
    buckets = bucketize(events, parsed)
    windows = []
    for start, bucket in buckets.items():
        by_system: dict[str, list[dict]] = {}
        for event in bucket:
            by_system.setdefault(event["system"], []).append(event)
        for system in sorted(by_system):
            metrics = compute_window_metrics(by_system[system], positive_label)
            windows.append({"window_start": start, **metrics})
    return {"window": window, "positive_label": positive_label, "windows": windows}
