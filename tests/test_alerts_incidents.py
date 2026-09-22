"""Tests for alerting rules and the incident log."""

import pytest

from raimonitor.alerts import evaluate_rules, validate_rule
from raimonitor.incidents import acknowledge, append_incidents, load_incidents


def _windows(dir_values, system="loan-v1"):
    windows = []
    for i, dir_value in enumerate(dir_values):
        windows.append({
            "window_start": f"2026-09-{i + 1:02d}T00:00:00Z",
            "system": system,
            "n": 100,
            "decision_rate": 0.5,
            "groups": {"sex": {"disparate_impact_ratio": dir_value, "values": {}}},
            "tpr_gap": {}, "fpr_gap": {},
        })
    return {"windows": windows}


RULE = {"name": "dir-below-80", "metric": "dir", "group_attr": "sex",
        "op": "lt", "threshold": 0.8, "persistence": 2, "severity": "critical"}


def test_rule_fires_after_persistence_met():
    incidents = evaluate_rules(_windows([0.9, 0.7, 0.6]), [RULE])
    assert len(incidents) == 1  # fires on window 3, the 2nd consecutive breach
    assert incidents[0]["window_start"] == "2026-09-03T00:00:00Z"
    assert incidents[0]["observed"] == pytest.approx(0.6)
    assert incidents[0]["severity"] == "critical"
    assert incidents[0]["status"] == "open"


def test_single_noisy_window_does_not_fire():
    assert evaluate_rules(_windows([0.9, 0.7, 0.95]), [RULE]) == []


def test_streak_resets_when_condition_clears():
    incidents = evaluate_rules(_windows([0.7, 0.9, 0.7, 0.6]), [RULE])
    assert len(incidents) == 1
    assert incidents[0]["window_start"] == "2026-09-04T00:00:00Z"


def test_incident_ids_deterministic():
    first = evaluate_rules(_windows([0.7, 0.6]), [RULE])
    second = evaluate_rules(_windows([0.7, 0.6]), [RULE])
    assert [i["id"] for i in first] == [i["id"] for i in second]


def test_volume_rule():
    rule = {"name": "volume-spike", "metric": "volume",
            "op": "gt", "threshold": 150, "persistence": 1}
    windows = _windows([0.9, 0.9])
    windows["windows"][1]["n"] = 200
    incidents = evaluate_rules(windows, [rule])
    assert len(incidents) == 1
    assert incidents[0]["observed"] == 200


def test_validate_rule_rejects_bad_input():
    with pytest.raises(ValueError):
        validate_rule({"name": "x", "metric": "dir", "op": "lt", "threshold": 0.8})
    with pytest.raises(ValueError):
        validate_rule({**RULE, "metric": "vibes"})
    with pytest.raises(ValueError):
        validate_rule({**RULE, "op": "near"})


def test_incident_log_dedupes_and_acknowledges(tmp_path):
    log = tmp_path / "incidents.jsonl"
    incidents = evaluate_rules(_windows([0.7, 0.6]), [RULE])
    appended, skipped = append_incidents(incidents, log)
    assert (appended, skipped) == (1, 0)
    appended, skipped = append_incidents(incidents, log)
    assert (appended, skipped) == (0, 1)
    assert len(load_incidents(log)) == 1
    assert acknowledge(incidents[0]["id"], log, note="investigating")
    assert load_incidents(log)[0]["status"] == "acknowledged"
    assert not acknowledge("inc-does-not-exist", log)
