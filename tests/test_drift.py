"""Tests for drift detection."""

import pytest

from raimonitor.drift import detect_drift, psi
from raimonitor.schema import normalize_event


def test_psi_identical_distributions_near_zero():
    assert psi([50, 50], [50, 50]) < 1e-9


def test_psi_shifted_distributions_large():
    assert psi([90, 10], [50, 50]) > 0.25


def test_psi_rejects_mismatched_bins():
    with pytest.raises(ValueError):
        psi([1, 2], [1])


def _events(n_pos, n_neg, feature_base=0.5, start=1):
    events = []
    for i in range(n_pos + n_neg):
        decision = "1" if i < n_pos else "0"
        events.append(normalize_event(
            {
                "timestamp": f"2026-09-{(start + i) % 28 + 1:02d}T00:00:00Z",
                "system": "s",
                "decision": decision,
                "features": {"score": feature_base + (i % 5) * 0.01},
            },
            "decision_log",
        ))
    return events


def test_detect_drift_no_shift():
    base = _events(50, 50)
    curr = _events(50, 50, start=3)
    result = detect_drift(base, curr)
    assert result["decision_psi"] < 0.1
    assert result["decision_rate_shift"] == pytest.approx(0.0)


def test_detect_drift_decision_shift():
    base = _events(50, 50)
    curr = _events(80, 20, start=3)
    result = detect_drift(base, curr)
    assert result["decision_psi"] > 0.25
    assert result["decision_rate_shift"] == pytest.approx(0.3)


def test_detect_drift_feature_shift():
    base = _events(50, 50, feature_base=0.5)
    curr = _events(50, 50, feature_base=0.9, start=3)
    result = detect_drift(base, curr, features=["score"])
    assert result["feature_psi"]["score"] is not None
    assert result["feature_psi"]["score"] > 0.25


def test_detect_drift_empty_rejected():
    with pytest.raises(ValueError):
        detect_drift([], _events(10, 10))
