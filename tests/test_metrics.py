"""Tests for the metrics engine."""

import pytest

from raimonitor.metrics import compute_metrics, compute_window_metrics
from raimonitor.schema import normalize_event


def _event(ts, decision, outcome, groups, system="loan-v1"):
    return normalize_event(
        {
            "timestamp": ts,
            "system": system,
            "decision": decision,
            "outcome": outcome,
            "groups": groups,
        },
        "decision_log",
    )


def _balanced_events():
    # 4 Male (3 positive), 4 Female (1 positive); outcomes match decisions.
    events = []
    for i, (group, dec) in enumerate(
        [("Male", 1), ("Male", 1), ("Male", 1), ("Male", 0),
         ("Female", 1), ("Female", 0), ("Female", 0), ("Female", 0)]
    ):
        events.append(_event(f"2026-09-0{i + 1}T00:00:00Z", dec, dec, {"sex": group}))
    return events


def test_dir_and_rates():
    metrics = compute_window_metrics(_balanced_events())
    assert metrics["n"] == 8
    assert metrics["decision_rate"] == pytest.approx(0.5)
    sex = metrics["groups"]["sex"]
    assert sex["values"]["Male"]["decision_rate"] == pytest.approx(0.75)
    assert sex["values"]["Female"]["decision_rate"] == pytest.approx(0.25)
    assert sex["disparate_impact_ratio"] == pytest.approx(0.25 / 0.75)


def test_dir_none_with_single_group():
    events = [_event(f"2026-09-0{i + 1}T00:00:00Z", 1, 1, {"sex": "Male"})
              for i in range(3)]
    metrics = compute_window_metrics(events)
    assert metrics["groups"]["sex"]["disparate_impact_ratio"] is None


def test_tpr_gap_and_accuracy():
    # outcomes differ from decisions for one Female case -> TPR gap appears
    events = _balanced_events()
    events[-1]["outcome"] = "1"  # Female, decision 0, outcome 1 -> FN
    metrics = compute_window_metrics(events)
    assert metrics["tpr_gap"]["sex"] == pytest.approx(1.0 - 0.5, abs=1e-9)
    assert metrics["accuracy"] == pytest.approx(7 / 8)


def test_no_outcomes_gives_none_performance():
    events = [_event(f"2026-09-0{i + 1}T00:00:00Z", 1, None, {"sex": "Male"})
              for i in range(2)]
    metrics = compute_window_metrics(events)
    assert metrics["accuracy"] is None
    assert metrics["tpr_gap"]["sex"] is None


def test_empty_window_rejected():
    with pytest.raises(ValueError):
        compute_window_metrics([])


def test_compute_metrics_chronological_and_per_system():
    events = _balanced_events() + [
        _event("2026-09-02T00:00:00Z", 1, 1, {"sex": "Male"}, system="loan-v2")
    ]
    doc = compute_metrics(events, window="30D")
    starts = [w["window_start"] for w in doc["windows"]]
    assert starts == sorted(starts)
    assert {w["system"] for w in doc["windows"]} == {"loan-v1", "loan-v2"}
