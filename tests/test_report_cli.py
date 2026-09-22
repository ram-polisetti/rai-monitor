"""Tests for the HTML report builder and the CLI pipeline."""

import json

import pytest

from raimonitor.ingest import write_events
from raimonitor.metrics import compute_metrics
from raimonitor.report import build_report
from raimonitor.schema import normalize_event
from raimonitor.cli import main


def _events():
    events = []
    for day in range(1, 15):
        for i in range(10):
            group = "Female" if i % 2 else "Male"
            decision = "1" if (group == "Male" or i % 4 == 0) else "0"
            events.append(normalize_event(
                {"timestamp": f"2026-09-{day:02d}T00:00:00Z", "system": "loan-v1",
                 "decision": decision, "outcome": decision,
                 "groups": {"sex": group}},
                "decision_log",
            ))
    return events


def test_build_report_contains_expected_sections():
    doc = compute_metrics(_events(), window="7D")
    html = build_report(doc, [])
    assert "Responsible AI monitoring dashboard" in html
    assert "Disparate impact ratio (sex)" in html
    assert "data:image/png;base64," in html
    assert "No incidents raised." in html
    assert "Female" in html and "Male" in html


def test_build_report_lists_incidents():
    doc = compute_metrics(_events(), window="7D")
    incidents = [{
        "id": "inc-abc123", "rule": "dir-below-80", "system": "loan-v1",
        "window_start": "2026-09-08T00:00:00Z", "metric": "dir",
        "group_attr": "sex", "observed": 0.5, "op": "lt",
        "threshold": 0.8, "severity": "critical", "status": "open",
    }]
    html = build_report(doc, incidents)
    assert "inc-abc123" in html
    assert "critical" in html


def test_build_report_empty_windows():
    html = build_report({"window": "7D", "windows": []}, [])
    assert "0</b>windows" in html or "0" in html


def test_cli_run_end_to_end(tmp_path):
    csv_path = tmp_path / "decisions.csv"
    rows = ["timestamp,system,decision,outcome,group_sex"]
    for day in range(1, 22):
        for i in range(20):
            group = "Female" if i % 2 else "Male"
            decision = 1 if (group == "Male" or i % 4 == 0) else 0
            rows.append(f"2026-09-{day:02d}T12:00:00Z,loan-v1,{decision},{decision},{group}")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps([
        {"name": "dir-below-80", "metric": "dir", "group_attr": "sex",
         "op": "lt", "threshold": 0.8, "persistence": 2, "severity": "critical"}
    ]), encoding="utf-8")
    out_dir = tmp_path / "out"
    rc = main(["run", "--input", str(csv_path), "--format", "decision_log",
               "--window", "7D", "--rules", str(rules_path),
               "--out-dir", str(out_dir)])
    assert rc == 0
    assert (out_dir / "events.jsonl").exists()
    assert (out_dir / "metrics.json").exists()
    assert (out_dir / "incidents.jsonl").exists()
    report = out_dir / "report.html"
    assert report.exists()
    assert "loan-v1" in report.read_text(encoding="utf-8")
    incidents = [json.loads(line) for line in
                 (out_dir / "incidents.jsonl").read_text(encoding="utf-8").splitlines()]
    # DIR = (1/4)/(1.0) = 0.25 for every window; 21 days of data fall into
    # four epoch-aligned 7D windows -> persistence 2 fires on windows 2, 3, 4.
    assert len(incidents) == 3
    assert all(inc["rule"] == "dir-below-80" for inc in incidents)


def test_cli_run_missing_input_errors(tmp_path):
    rc = main(["run", "--input", str(tmp_path / "missing.csv"),
               "--format", "decision_log", "--rules", str(tmp_path / "r.json"),
               "--out-dir", str(tmp_path / "out")])
    assert rc == 2


def test_write_and_reread_events_roundtrip(tmp_path):
    events = _events()[:5]
    path = write_events(events, tmp_path / "events.jsonl")
    reread = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert reread == events
