"""Tests for the live monitoring server (streaming ingestion, watermark, API)."""

import json
import time
import urllib.error
import urllib.request

import pytest

from raimonitor.ingest import from_decision_log
from raimonitor.metrics import compute_metrics
from raimonitor.server import LiveMonitor, LiveServer

RULES = [
    {"name": "dir-low", "metric": "dir", "group_attr": "sex",
     "op": "lt", "threshold": 0.8, "persistence": 1, "severity": "warning"},
]


def _jsonl_line(ts, decision, group, system="loan-v1", outcome=None):
    record = {"timestamp": ts, "system": system, "decision": decision,
              "groups": {"sex": group}}
    if outcome is not None:
        record["outcome"] = outcome
    return json.dumps(record)


def _write_rules(path):
    path.write_text(json.dumps(RULES), encoding="utf-8")


def _biased_lines(start_day=1, days=8, per_day=10):
    lines = []
    for day in range(start_day, start_day + days):
        for i in range(per_day):
            group = "Female" if i % 2 else "Male"
            decision = 1 if group == "Male" else 0  # DIR = 0 -> fires
            lines.append(_jsonl_line(f"2026-09-{day:02d}T00:00:00Z", decision,
                                     group, outcome=decision))
    return lines


def _make_monitor(tmp_path, lines, window="7D", **kwargs):
    log = tmp_path / "decisions.jsonl"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rules = tmp_path / "rules.json"
    _write_rules(rules)
    state = tmp_path / "state"
    monitor = LiveMonitor(log, rules, state, window=window, **kwargs)
    return monitor, log, rules, state


def test_poll_tails_new_rows(tmp_path):
    monitor, log, _, state = _make_monitor(tmp_path, _biased_lines(days=4))
    assert monitor.poll() == 40
    assert monitor.status()["events"] == 40
    with open(log, "a", encoding="utf-8") as f:
        f.write("\n".join(_biased_lines(start_day=5, days=4)) + "\n")
    assert monitor.poll() == 40
    assert monitor.status()["events"] == 80
    # Watermark persisted: a fresh poll consumes nothing.
    assert monitor.poll() == 0
    saved = json.loads((state / "state.json").read_text(encoding="utf-8"))
    assert saved["offset"] == log.stat().st_size
    assert saved["events"] == 80


def test_streaming_matches_batch(tmp_path):
    lines = _biased_lines(days=8)
    monitor, _, _, _ = _make_monitor(tmp_path, lines)
    monitor.poll()
    batch_path = tmp_path / "batch.jsonl"
    batch_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events = from_decision_log(batch_path)
    expected = compute_metrics(events, window="7D")
    assert monitor.snapshot()["metrics"] == expected


def test_restart_resumes_without_duplicates(tmp_path):
    monitor, log, rules, state = _make_monitor(tmp_path, _biased_lines(days=8))
    monitor.poll()
    before = monitor.snapshot()
    assert before["status"]["incidents"] > 0

    restarted = LiveMonitor(log, rules, state, window="7D")
    assert restarted.status()["events"] == 80
    restarted.poll()  # nothing new; incidents must not duplicate
    after = restarted.snapshot()
    assert after["status"]["incidents"] == before["status"]["incidents"]
    assert after["metrics"] == before["metrics"]
    assert [i["id"] for i in after["incidents"]] == \
        [i["id"] for i in before["incidents"]]


def test_rotation_recovers(tmp_path):
    monitor, log, _, _ = _make_monitor(tmp_path, _biased_lines(days=4))
    assert monitor.poll() == 40
    log.write_text("\n".join(_biased_lines(days=2)) + "\n", encoding="utf-8")
    assert monitor.poll() == 20  # treated as a fresh log, no crash
    assert monitor.status()["events"] == 20


def test_csv_tailing(tmp_path):
    log = tmp_path / "decisions.csv"
    log.write_text(
        "timestamp,system,decision,outcome,group_sex\n"
        "2026-09-01T00:00:00Z,loan-v1,1,1,Male\n"
        "2026-09-01T00:00:00Z,loan-v1,0,0,Female\n",
        encoding="utf-8")
    rules = tmp_path / "rules.json"
    _write_rules(rules)
    monitor = LiveMonitor(log, rules, tmp_path / "state")
    assert monitor.poll() == 2
    with open(log, "a", encoding="utf-8") as f:
        f.write("2026-09-02T00:00:00Z,loan-v1,1,1,Male\n")
    assert monitor.poll() == 1
    assert monitor.status()["events"] == 3


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


def test_http_endpoints(tmp_path):
    monitor, _, _, _ = _make_monitor(tmp_path, _biased_lines(days=8))
    server = LiveServer(monitor, port=0, poll_interval=60)
    server.start()
    try:
        base = server.url
        status, ctype, body = _get(base + "/")
        assert status == 200
        assert "text/html" in ctype
        assert b"Responsible AI monitoring" in body
        assert b'http-equiv="refresh"' in body

        status, ctype, body = _get(base + "/api/metrics")
        assert status == 200 and "application/json" in ctype
        doc = json.loads(body)
        assert doc["windows"]

        status, _, body = _get(base + "/api/incidents")
        assert status == 200
        assert len(json.loads(body)) > 0

        status, _, body = _get(base + "/api/status")
        assert status == 200
        assert json.loads(body)["events"] == 80

        try:
            _get(base + "/nope")
            raise AssertionError("expected HTTPError")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.stop()


def test_serve_picks_up_live_appends(tmp_path):
    monitor, log, _, _ = _make_monitor(tmp_path, _biased_lines(days=4))
    server = LiveServer(monitor, port=0, poll_interval=0.05)
    server.start()
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write("\n".join(_biased_lines(start_day=5, days=4)) + "\n")
        deadline = 20.0
        while monitor.status()["events"] < 80 and deadline > 0:
            time.sleep(0.1)
            deadline -= 0.1
        assert monitor.status()["events"] == 80
        _, _, body = _get(server.url + "/api/status")
        assert json.loads(body)["events"] == 80
    finally:
        server.stop()


def test_serve_rules_reload_live(tmp_path):
    monitor, log, rules, _ = _make_monitor(tmp_path, _biased_lines(days=8))
    server = LiveServer(monitor, port=0, poll_interval=0.05)
    server.start()
    try:
        assert monitor.status()["incidents"] > 0
        # Loosen the rule so it stops firing on the next poll.
        rules.write_text(json.dumps([
            {"name": "dir-low", "metric": "dir", "group_attr": "sex",
             "op": "lt", "threshold": 0.0, "persistence": 1,
             "severity": "warning"},
        ]), encoding="utf-8")
        with open(log, "a", encoding="utf-8") as f:
            f.write(_jsonl_line("2026-09-09T00:00:00Z", 1, "Male", outcome=1) + "\n")
        before = monitor.status()["incidents"]
        deadline = 20.0
        while deadline > 0 and monitor.status()["events"] < 81:
            time.sleep(0.1)
            deadline -= 0.1
        # No new incident from the reloaded rule (threshold 0.0 never fires
        # on DIR >= 0... note DIR can be 0.0, and 0.0 < 0.0 is False).
        assert monitor.status()["incidents"] == before
    finally:
        server.stop()
