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

import random

from .evidence import bootstrap_rate_ci, new_rng, window_rng
from .schema import is_positive
from .windows import bucketize, parse_window


def _rate(positives: int, total: int) -> float | None:
    return positives / total if total else None


def _group_counts(events: list[dict], attr: str,
                  positive_label: str) -> dict[str, list[int]]:
    """Per-value [positives, total] counts for one group attribute."""
    counts: dict[str, list[int]] = {}
    for event in events:
        value = event["groups"].get(attr)
        if value is None:
            continue
        bucket = counts.setdefault(value, [0, 0])
        bucket[1] += 1
        if is_positive(event, positive_label):
            bucket[0] += 1
    return counts


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


def _scored_flags(events: list[dict], attr: str | None, value: str | None,
                  positive_label: str) -> tuple[list[int], list[int]]:
    """0/1 indicator flags for bootstrapping TPR and FPR CIs.

    Returns ``(tpr_flags, fpr_flags)`` where ``tpr_flags`` has one entry per
    actually-positive scored event (1 when the model also predicted
    positive) and ``fpr_flags`` one per actually-negative scored event (1
    when the model predicted positive). The means of these flags are the
    TPR and FPR, so bootstrapping the flags bootstraps the rates.
    """
    tpr_flags: list[int] = []
    fpr_flags: list[int] = []
    for event in events:
        if attr is not None and event["groups"].get(attr) != value:
            continue
        outcome = event.get("outcome")
        if outcome is None:
            continue
        pred_pos = is_positive(event, positive_label)
        if outcome == positive_label:
            tpr_flags.append(1 if pred_pos else 0)
        else:
            fpr_flags.append(1 if pred_pos else 0)
    return tpr_flags, fpr_flags


def _insufficient_info(n: int) -> dict:
    """Placeholder metric block for a group value below ``min_group_n``.

    Rates are explicit ``None`` — never noisy point estimates — and the
    ``insufficient_evidence`` flag tells downstream consumers (DIR, gaps,
    alert rules, the report) to exclude this group value.
    """
    return {
        "n": n,
        "decision_rate": None,
        "decision_rate_ci": None,
        "tpr": None,
        "tpr_ci": None,
        "fpr": None,
        "fpr_ci": None,
        "accuracy": None,
        "precision": None,
        "n_scored": 0,
        "insufficient_evidence": True,
    }


def _group_value_metrics(events: list[dict], attr: str, value: str,
                         positives: int, total: int, positive_label: str,
                         bootstrap_reps: int,
                         rng: random.Random | None) -> dict:
    err = _error_rates(events, attr, value, positive_label)
    info: dict = {
        "n": total,
        "decision_rate": _rate(positives, total),
        "decision_rate_ci": None,
        **err,
        "tpr_ci": None,
        "fpr_ci": None,
        "insufficient_evidence": False,
    }
    if bootstrap_reps > 0 and rng is not None:
        flags = [1 if is_positive(e, positive_label) else 0
                 for e in events if e["groups"].get(attr) == value]
        info["decision_rate_ci"] = list(bootstrap_rate_ci(flags, bootstrap_reps, rng))
        tpr_flags, fpr_flags = _scored_flags(events, attr, value, positive_label)
        info["tpr_ci"] = list(bootstrap_rate_ci(tpr_flags, bootstrap_reps, rng))
        info["fpr_ci"] = list(bootstrap_rate_ci(fpr_flags, bootstrap_reps, rng))
    return info


def compute_window_metrics(events: list[dict], positive_label: str = "1",
                           min_group_n: int = 1, bootstrap_reps: int = 0,
                           bootstrap_seed: int = 0,
                           rng: random.Random | None = None) -> dict:
    """Compute the full metric set for one window's events (one system).

    ``min_group_n``: group values with fewer than this many events are
    flagged ``insufficient_evidence`` — their rates are ``None`` and they
    are excluded from DIR and gap computations. ``bootstrap_reps``: when
    > 0, attach 95% bootstrap CIs to decision rates and per-group TPR/FPR;
    randomness comes from ``rng`` (or a ``random.Random(bootstrap_seed)``
    when omitted), consumed in sorted group order so results are
    deterministic.
    """
    if not events:
        raise ValueError("cannot compute metrics for an empty window")
    if min_group_n < 1:
        raise ValueError(f"min_group_n must be >= 1, got {min_group_n}")
    if bootstrap_reps < 0:
        raise ValueError(f"bootstrap_reps must be >= 0, got {bootstrap_reps}")
    if bootstrap_reps > 0 and rng is None:
        rng = new_rng(bootstrap_seed)
    system = events[0]["system"]
    n = len(events)
    positives = sum(1 for e in events if is_positive(e, positive_label))
    attrs = sorted({attr for e in events for attr in e["groups"]})

    group_metrics: dict[str, dict] = {}
    tpr_gaps: dict[str, float | None] = {}
    fpr_gaps: dict[str, float | None] = {}
    for attr in attrs:
        counts = _group_counts(events, attr, positive_label)
        per_value = {}
        for value in sorted(counts):
            pos, total = counts[value]
            if total < min_group_n:
                per_value[value] = _insufficient_info(total)
            else:
                per_value[value] = _group_value_metrics(
                    events, attr, value, pos, total, positive_label,
                    bootstrap_reps, rng)
        group_metrics[attr] = {
            "disparate_impact_ratio": _dir({
                value: info for value, info in per_value.items()
                if not info.get("insufficient_evidence")
            }),
            "values": per_value,
        }
        tpr_gaps[attr] = _gap([v["tpr"] for v in per_value.values()])
        fpr_gaps[attr] = _gap([v["fpr"] for v in per_value.values()])

    overall_err = _error_rates(events, None, None, positive_label)
    decision_rate_ci = None
    if bootstrap_reps > 0 and rng is not None:
        flags = [1 if is_positive(e, positive_label) else 0 for e in events]
        decision_rate_ci = list(bootstrap_rate_ci(flags, bootstrap_reps, rng))
    return {
        "system": system,
        "n": n,
        "decision_rate": _rate(positives, n),
        "decision_rate_ci": decision_rate_ci,
        "accuracy": overall_err["accuracy"],
        "precision": overall_err["precision"],
        "n_scored": overall_err["n_scored"],
        "groups": group_metrics,
        "tpr_gap": tpr_gaps,
        "fpr_gap": fpr_gaps,
    }


def compute_metrics(events: list[dict], window: str = "7D",
                    positive_label: str = "1", min_group_n: int = 1,
                    bootstrap_reps: int = 0, bootstrap_seed: int = 0) -> dict:
    """Bucket events into windows and compute metrics per (system, window).

    Returns ``{"window": spec, "windows": [...]}`` with windows in
    chronological order. Each entry carries ``window_start`` plus the output
    of :func:`compute_window_metrics`. Systems are kept separate: metrics are
    computed per system within each window.

    ``min_group_n`` guards small groups (see :mod:`evidence`);
    ``bootstrap_reps``/``bootstrap_seed`` control bootstrap CIs. CIs use a
    per-(window, system) seeded RNG, so the whole document is deterministic
    for fixed inputs — and identical whether computed in one batch or
    incrementally by the live server.
    """
    parsed = parse_window(window)
    buckets = bucketize(events, parsed)
    windows = []
    for start, bucket in buckets.items():
        by_system: dict[str, list[dict]] = {}
        for event in bucket:
            by_system.setdefault(event["system"], []).append(event)
        for system in sorted(by_system):
            rng = (window_rng(bootstrap_seed, start, system)
                   if bootstrap_reps > 0 else None)
            metrics = compute_window_metrics(
                by_system[system], positive_label,
                min_group_n=min_group_n, bootstrap_reps=bootstrap_reps,
                rng=rng)
            windows.append({"window_start": start, **metrics})
    return {
        "window": window,
        "positive_label": positive_label,
        "min_group_n": min_group_n,
        "bootstrap_reps": bootstrap_reps,
        "bootstrap_seed": bootstrap_seed,
        "windows": windows,
    }
