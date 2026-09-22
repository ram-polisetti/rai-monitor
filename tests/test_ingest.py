"""Tests for the ingestion adapters."""

import json

from raimonitor.ingest import (
    from_decision_log,
    from_opsaudit_report,
    from_rag_audit_trail,
    ingest,
)


def test_decision_log_csv(tmp_path):
    csv_path = tmp_path / "log.csv"
    csv_path.write_text(
        "timestamp,system,decision,outcome,group_sex,group_race,feature_score\n"
        "2026-09-01T00:00:00Z,loan-v1,1,1,Female,Black,0.72\n"
        "2026-09-01T01:00:00Z,loan-v1,0,,Male,White,0.31\n",
        encoding="utf-8",
    )
    events = from_decision_log(csv_path)
    assert len(events) == 2
    assert events[0]["groups"] == {"sex": "Female", "race": "Black"}
    assert events[0]["features"] == {"score": "0.72"}
    assert events[1]["outcome"] is None


def test_decision_log_jsonl_nested(tmp_path):
    jsonl_path = tmp_path / "log.jsonl"
    jsonl_path.write_text(
        json.dumps({"timestamp": "2026-09-01T00:00:00Z", "system": "s",
                    "decision": "approve",
                    "groups": {"region": "south"}}) + "\n",
        encoding="utf-8",
    )
    events = from_decision_log(jsonl_path)
    assert events[0]["decision"] == "approve"
    assert events[0]["groups"] == {"region": "south"}


def test_ingest_dispatch_and_unknown_format(tmp_path):
    csv_path = tmp_path / "log.csv"
    csv_path.write_text("timestamp,system,decision\n2026-09-01T00:00:00Z,s,1\n",
                        encoding="utf-8")
    assert len(ingest(csv_path, "decision_log")) == 1
    try:
        ingest(csv_path, "nope")
    except ValueError as exc:
        assert "unknown ingest format" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_opsaudit_report(tmp_path):
    report = {
        "groups": [
            {"group": "sex=Female", "n": 100, "selection_rate": 0.2,
             "tpr": 0.7, "fpr": 0.1, "precision": 0.8, "accuracy": 0.85},
            {"group": "sex=Male", "n": 120, "selection_rate": 0.5,
             "tpr": 0.8, "fpr": 0.1, "precision": 0.85, "accuracy": 0.87},
        ],
        "disparate_impact_ratio": 0.4,
        "flags": [{"check": "dir", "status": "FAIL"}],
        "provenance": {"timestamp": "2026-09-10T00:00:00Z"},
        "context": {"system": "hiring-screen-v3"},
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    events = from_opsaudit_report(path)
    assert len(events) == 2
    assert events[0]["timestamp"] == "2026-09-10T00:00:00Z"
    assert events[0]["groups"] == {"sex": "Female"}
    assert events[0]["features"]["selection_rate"] == 0.2
    assert events[0]["audit_flags"] == [{"check": "dir", "status": "FAIL"}]
    assert events[0]["source"] == "opsaudit"


def test_rag_audit_trail(tmp_path):
    trail = tmp_path / "audit_log.jsonl"
    trail.write_text(
        json.dumps({"ts": "2026-09-01T00:00:00Z", "query": "q",
                    "decision": "refuse", "domain": "lending",
                    "top_score": 0.42, "reason": "low evidence"}) + "\n",
        encoding="utf-8",
    )
    events = from_rag_audit_trail(trail)
    assert len(events) == 1
    assert events[0]["decision"] == "refuse"
    assert events[0]["groups"] == {"domain": "lending"}
    assert events[0]["features"] == {"top_score": 0.42}
    assert events[0]["reason"] == "low evidence"
    assert events[0]["source"] == "rag_audit"
