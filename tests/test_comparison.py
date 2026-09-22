"""Tests for cross-system comparison views in the report."""

import pytest

from raimonitor.metrics import compute_metrics
from raimonitor.report import build_report
from raimonitor.schema import normalize_event


def _events(system, drift=False):
    events = []
    for day in range(1, 15):
        for i in range(10):
            group = "Female" if i % 2 else "Male"
            if drift and group == "Female" and day > 7:
                decision = "0"
            else:
                decision = "1" if (group == "Male" or i % 4 == 0) else "0"
            events.append(normalize_event(
                {"timestamp": f"2026-09-{day:02d}T00:00:00Z", "system": system,
                 "decision": decision, "outcome": decision,
                 "groups": {"sex": group}},
                "decision_log",
            ))
    return events


def _doc():
    return compute_metrics(_events("loan-v1") + _events("loan-v2", drift=True),
                           window="7D")


def test_cross_system_charts_present():
    html = build_report(_doc(), [])
    assert "Disparate impact ratio (sex) — all systems" in html
    assert "TPR gap (sex) — all systems" in html
    assert "FPR gap (sex) — all systems" in html
    assert "Decision rate (sex=Female) — all systems" in html
    assert "Decision rate (sex=Male) — all systems" in html
    # Both systems appear as chart series.
    assert "loan-v1" in html and "loan-v2" in html


def test_cross_system_comparison_table():
    html = build_report(_doc(), [])
    assert "Cross-system comparison" in html
    # Latest window per system with DIR columns.
    assert "DIR (sex)" in html


def test_per_system_latest_sections():
    html = build_report(_doc(), [])
    assert "Latest window — loan-v1" in html
    assert "Latest window — loan-v2" in html
    assert "Group breakdown — loan-v1 / sex" in html


def test_insufficient_evidence_rendered():
    doc = compute_metrics(_events("loan-v1"), window="7D", min_group_n=1000)
    html = build_report(doc, [])
    assert "insufficient evidence" in html
    assert "n &lt; 1000" in html


def test_ci_rendered_when_enabled():
    doc = compute_metrics(_events("loan-v1"), window="7D", bootstrap_reps=50,
                          bootstrap_seed=1)
    html = build_report(doc, [])
    # CI formatted as "0.625 (0.50-0.75)" style.
    assert "95% CI" in html
    assert "(" in html and ")" in html


def test_refresh_tag_only_when_requested():
    html = build_report(_doc(), [], refresh_seconds=5)
    assert 'http-equiv="refresh" content="5"' in html
    html2 = build_report(_doc(), [])
    assert 'http-equiv="refresh"' not in html2


def test_report_still_works_single_system():
    # Session-1 assertions keep holding on the new layout.
    doc = compute_metrics(_events("loan-v1"), window="7D")
    html = build_report(doc, [])
    assert "Responsible AI monitoring dashboard" in html
    assert "Disparate impact ratio (sex)" in html
    assert "data:image/png;base64," in html
    assert "No incidents raised." in html
    assert "Female" in html and "Male" in html
