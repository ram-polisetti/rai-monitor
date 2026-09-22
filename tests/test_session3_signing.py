"""Tests for the hash-chained (tamper-evident) incident log."""

import json

import pytest

from raimonitor.cli import main
from raimonitor.incidents import (
    append_incidents,
    load_incidents,
    sign_record,
    verify_log,
    acknowledge,
)

INCIDENTS = [
    {"id": "inc-a", "rule": "dir-below-80", "system": "loan-v1",
     "window_start": "2026-09-01T00:00:00Z", "metric": "dir",
     "group_attr": "sex", "observed": 0.5, "op": "lt",
     "threshold": 0.8, "severity": "critical", "status": "open"},
    {"id": "inc-b", "rule": "dir-below-80", "system": "loan-v1",
     "window_start": "2026-09-08T00:00:00Z", "metric": "dir",
     "group_attr": "sex", "observed": 0.4, "op": "lt",
     "threshold": 0.8, "severity": "critical", "status": "open"},
]


def test_appended_records_are_chained(tmp_path):
    log = tmp_path / "incidents.jsonl"
    append_incidents(INCIDENTS, log)
    records = load_incidents(log)
    assert len(records) == 2
    assert records[0]["prev_hash"] == "GENESIS"
    assert records[1]["prev_hash"] == records[0]["record_hash"]
    assert all(r["record_hash"] for r in records)
    result = verify_log(log)
    assert result == {"ok": True, "records": 2, "signed": 2,
                      "legacy_unsigned": 0, "errors": []}


def test_sign_record_is_deterministic():
    first = sign_record({"id": "x"}, "GENESIS")
    second = sign_record({"id": "x"}, "GENESIS")
    assert first["record_hash"] == second["record_hash"]


def test_tampered_field_breaks_chain(tmp_path):
    log = tmp_path / "incidents.jsonl"
    append_incidents(INCIDENTS, log)
    lines = log.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["observed"] = 0.99  # attacker inflates the observed metric
    lines[1] = json.dumps(record)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_log(log)
    assert not result["ok"]
    assert any("record_hash mismatch" in e for e in result["errors"])


def test_reordered_log_breaks_chain(tmp_path):
    log = tmp_path / "incidents.jsonl"
    append_incidents(INCIDENTS, log)
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    result = verify_log(log)
    assert not result["ok"]
    assert any("prev_hash mismatch" in e for e in result["errors"])


def test_legacy_unsigned_records_do_not_fail_verification(tmp_path):
    log = tmp_path / "incidents.jsonl"
    log.write_text(json.dumps({"id": "old", "status": "open"}) + "\n",
                   encoding="utf-8")
    append_incidents(INCIDENTS, log)  # new records chain from GENESIS
    result = verify_log(log)
    assert result["ok"]
    assert result["legacy_unsigned"] == 1
    assert result["signed"] == 2


def test_acknowledge_rechains_and_still_verifies(tmp_path):
    log = tmp_path / "incidents.jsonl"
    append_incidents(INCIDENTS, log)
    before = [r["record_hash"] for r in load_incidents(log)]
    assert acknowledge("inc-a", log, note="investigating")
    records = load_incidents(log)
    assert records[0]["status"] == "acknowledged"
    assert records[0]["ack_note"] == "investigating"
    # the ack visibly re-chained from the modified record onward
    assert records[0]["record_hash"] != before[0]
    assert records[1]["record_hash"] != before[1]
    assert records[1]["prev_hash"] == records[0]["record_hash"]
    assert verify_log(log)["ok"]


def test_verify_log_cli(tmp_path, capsys):
    log = tmp_path / "incidents.jsonl"
    append_incidents(INCIDENTS, log)
    assert main(["verify-log", "--incidents", str(log)]) == 0
    assert "OK" in capsys.readouterr().out
    # tamper, then verify fails
    lines = log.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["severity"] = "warning"
    lines[0] = json.dumps(record)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert main(["verify-log", "--incidents", str(log)]) == 1
    assert "BROKEN" in capsys.readouterr().out


def test_verify_log_cli_missing_file(tmp_path):
    assert main(["verify-log",
                 "--incidents", str(tmp_path / "nope.jsonl")]) == 0
