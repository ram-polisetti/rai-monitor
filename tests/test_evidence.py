"""Tests for the evidence-quality guards (min_group_n, bootstrap CIs)."""

import pytest

from raimonitor.alerts import evaluate_rules
from raimonitor.evidence import bootstrap_rate_ci, new_rng
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


def _mixed_events():
    # sex=Male x6 (4 positive -> rate 0.667), sex=Female x2 (1 positive).
    # Female is the small group: guarded out at min_group_n=3.
    events = []
    specs = [("Male", 1), ("Male", 1), ("Male", 1), ("Male", 1),
             ("Male", 0), ("Male", 0),
             ("Female", 1), ("Female", 0)]
    for i, (group, dec) in enumerate(specs):
        events.append(_event(f"2026-09-01T0{i}:00:00Z", dec, dec, {"sex": group}))
    return events


def test_min_group_n_flags_small_group():
    metrics = compute_window_metrics(_mixed_events(), min_group_n=3)
    sex = metrics["groups"]["sex"]["values"]
    assert sex["Female"]["insufficient_evidence"] is True
    assert sex["Female"]["decision_rate"] is None
    assert sex["Female"]["tpr"] is None
    assert sex["Male"]["insufficient_evidence"] is False
    assert sex["Male"]["decision_rate"] == pytest.approx(4 / 6)


def test_min_group_n_excludes_from_dir():
    # With min_group_n=3 only Male has sufficient evidence -> DIR is None.
    metrics = compute_window_metrics(_mixed_events(), min_group_n=3)
    assert metrics["groups"]["sex"]["disparate_impact_ratio"] is None
    # Without the guard DIR is computed over both groups: 0.5 / (4/6).
    metrics = compute_window_metrics(_mixed_events())
    assert metrics["groups"]["sex"]["disparate_impact_ratio"] == pytest.approx(0.75)


def test_min_group_n_excludes_from_gaps():
    metrics = compute_window_metrics(_mixed_events(), min_group_n=3)
    assert metrics["tpr_gap"]["sex"] is None
    assert metrics["fpr_gap"]["sex"] is None


def test_min_group_n_invalid():
    with pytest.raises(ValueError):
        compute_window_metrics(_mixed_events(), min_group_n=0)


def test_insufficient_evidence_never_fires_rules():
    # A tiny group with an extreme rate must not trip a DIR rule: the
    # observation is None, not a noisy number.
    doc = compute_metrics(_mixed_events(), window="30D", min_group_n=100)
    incidents = evaluate_rules(doc, [
        {"name": "dir-low", "metric": "dir", "group_attr": "sex",
         "op": "lt", "threshold": 0.8, "persistence": 1, "severity": "warning"},
    ])
    assert incidents == []


def test_bootstrap_ci_deterministic():
    events = _mixed_events()
    doc1 = compute_metrics(events, window="30D", bootstrap_reps=200,
                           bootstrap_seed=7)
    doc2 = compute_metrics(events, window="30D", bootstrap_reps=200,
                           bootstrap_seed=7)
    assert doc1 == doc2
    w = doc1["windows"][0]
    lo, hi = w["decision_rate_ci"]
    assert 0.0 <= lo <= w["decision_rate"] <= hi <= 1.0
    male_ci = w["groups"]["sex"]["values"]["Male"]["decision_rate_ci"]
    assert male_ci[0] <= 4 / 6 <= male_ci[1]


def test_bootstrap_ci_off_by_default():
    w = compute_window_metrics(_mixed_events())["groups"]["sex"]["values"]["Male"]
    assert w["decision_rate_ci"] is None
    assert w["tpr_ci"] is None
    w0 = compute_window_metrics(_mixed_events())
    assert w0["decision_rate_ci"] is None


def test_bootstrap_ci_degenerate_group():
    # All-positive group -> CI collapses to [1, 1], not NaN.
    events = [_event(f"2026-09-01T0{i}:00:00Z", 1, 1, {"sex": "Male"})
              for i in range(6)]
    metrics = compute_window_metrics(events, bootstrap_reps=100, bootstrap_seed=3)
    ci = metrics["groups"]["sex"]["values"]["Male"]["decision_rate_ci"]
    assert ci == [1.0, 1.0]


def test_bootstrap_helper_unit():
    rng = new_rng(42)
    lo, hi = bootstrap_rate_ci([1, 1, 1, 0], 500, rng)
    assert lo <= 0.75 <= hi
    assert 0.0 <= lo <= hi <= 1.0
    assert bootstrap_rate_ci([], 100, new_rng(1)) == (None, None)
    assert bootstrap_rate_ci([1, 0], 0, new_rng(1)) == (None, None)


def test_bootstrap_tpr_fpr_cis_present():
    events = _mixed_events()
    metrics = compute_window_metrics(events, bootstrap_reps=100, bootstrap_seed=5)
    male = metrics["groups"]["sex"]["values"]["Male"]
    assert male["tpr_ci"][0] <= male["tpr"] <= male["tpr_ci"][1]


def test_bootstrap_insufficient_group_gets_no_ci():
    metrics = compute_window_metrics(_mixed_events(), min_group_n=3,
                                     bootstrap_reps=100, bootstrap_seed=5)
    female = metrics["groups"]["sex"]["values"]["Female"]
    assert female["decision_rate_ci"] is None
    assert female["tpr_ci"] is None


def test_metrics_doc_carries_guard_settings():
    doc = compute_metrics(_mixed_events(), min_group_n=4, bootstrap_reps=50,
                          bootstrap_seed=9)
    assert doc["min_group_n"] == 4
    assert doc["bootstrap_reps"] == 50
    assert doc["bootstrap_seed"] == 9
    doc_default = compute_metrics(_mixed_events())
    assert doc_default["min_group_n"] == 1
    assert doc_default["bootstrap_reps"] == 0
