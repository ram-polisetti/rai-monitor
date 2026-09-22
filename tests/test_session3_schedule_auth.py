"""Tests for scheduled runs (stateful watermark) and server auth."""

import json
import os
import urllib.error
import urllib.request

import pytest

from raimonitor.cli import _resolve_auth_token, main
from raimonitor.server import LiveMonitor, LiveServer


def _jsonl_line(ts, decision, group, system="loan-v1"):
    return json.dumps({"timestamp": ts, "system": system,
                       "decision": decision, "outcome": decision,
                       "groups": {"sex": group}})


def _lines(start_day=1, days=4, per_day=10):
    lines = []
    for day in range(start_day, start_day + days):
        for i in range(per_day):
            group = "Female" if i % 2 else "Male"
            decision = 1 if group == "Male" else 0  # DIR = 0 -> fires
            lines.append(_jsonl_line(f"2026-09-{day:02d}T00:00:00Z",
                                     decision, group))
    return lines


def _setup(tmp_path, days=4):
    log = tmp_path / "decisions.jsonl"
    log.write_text("\n".join(_lines(days=days)) + "\n", encoding="utf-8")
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([
        {"name": "dir-low", "metric": "dir", "group_attr": "sex",
         "op": "lt", "threshold": 0.8, "persistence": 1,
         "severity": "critical"},
    ]), encoding="utf-8")
    return log, rules


def _schedule_args(tmp_path, log, rules):
    return ["schedule", "--input", str(log), "--rules", str(rules),
            "--state-dir", str(tmp_path / "state"),
            "--out-dir", str(tmp_path / "out")]


def test_schedule_consumes_incrementally(tmp_path):
    log, rules = _setup(tmp_path)
    assert main(_schedule_args(tmp_path, log, rules)) == 0
    out = tmp_path / "out"
    assert (out / "report.html").exists()
    assert (out / "metrics.json").exists()
    incidents_before = (tmp_path / "state" / "incidents.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert incidents_before, "expected incidents from the biased feed"
    # Second run with no new rows: nothing new, no duplicate incidents.
    assert main(_schedule_args(tmp_path, log, rules)) == 0
    incidents_after = (tmp_path / "state" / "incidents.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert incidents_after == incidents_before
    # Append more rows: only the new rows are processed.
    with open(log, "a", encoding="utf-8") as f:
        f.write("\n".join(_lines(start_day=5, days=4)) + "\n")
    assert main(_schedule_args(tmp_path, log, rules)) == 0
    status = json.loads((tmp_path / "state" / "state.json").read_text(
        encoding="utf-8"))
    assert status["events"] == 80
    assert status["offset"] == log.stat().st_size


def test_schedule_matches_serve_state(tmp_path):
    log, rules = _setup(tmp_path)
    assert main(_schedule_args(tmp_path, log, rules)) == 0
    # A live server pointed at the same state dir resumes cleanly.
    monitor = LiveMonitor(log, rules, tmp_path / "state")
    assert monitor.status()["events"] == 40
    assert monitor.poll() == 0


def _start_server(tmp_path, auth_token=None):
    monitor = LiveMonitor(*_setup(tmp_path)[:2], tmp_path / "state")
    server = LiveServer(monitor, host="127.0.0.1", port=0,
                        auth_token=auth_token)
    server.start()
    return server


def _get(server, path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(server.url + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_server_without_token_is_open(tmp_path):
    server = _start_server(tmp_path)
    try:
        status, _ = _get(server, "/api/status")
        assert status == 200
    finally:
        server.stop()


def test_server_auth_protects_all_routes(tmp_path):
    server = _start_server(tmp_path, auth_token="s3cret-token")
    try:
        for path in ("/", "/api/metrics", "/api/incidents", "/api/status"):
            status, _ = _get(server, path)
            assert status == 401, f"{path} without token should be 401"
            status, _ = _get(server, path, token="wrong")
            assert status == 401, f"{path} with wrong token should be 401"
            status, body = _get(server, path, token="s3cret-token")
            assert status == 200, f"{path} with token should be 200"
    finally:
        server.stop()


def test_server_auth_www_authenticate_header(tmp_path):
    server = _start_server(tmp_path, auth_token="tok")
    try:
        req = urllib.request.Request(server.url + "/api/status")
        try:
            urllib.request.urlopen(req, timeout=5)
            pytest.fail("expected 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert exc.headers.get("WWW-Authenticate") == "Bearer"
    finally:
        server.stop()


class _Args:
    def __init__(self, auth_token=None, auth_token_env=None):
        self.auth_token = auth_token
        self.auth_token_env = auth_token_env


def test_resolve_auth_token_prefers_flag_and_env():
    assert _resolve_auth_token(_Args(auth_token="abc")) == "abc"
    os.environ["RAIMONITOR_TEST_TOKEN"] = "from-env"
    try:
        assert _resolve_auth_token(
            _Args(auth_token_env="RAIMONITOR_TEST_TOKEN")) == "from-env"
        with pytest.raises(ValueError, match="not set"):
            _resolve_auth_token(_Args(auth_token_env="RAIMONITOR_NOPE_XYZ"))
        with pytest.raises(ValueError, match="only one"):
            _resolve_auth_token(_Args(auth_token="a",
                                      auth_token_env="RAIMONITOR_TEST_TOKEN"))
    finally:
        del os.environ["RAIMONITOR_TEST_TOKEN"]
    assert _resolve_auth_token(_Args()) is None
