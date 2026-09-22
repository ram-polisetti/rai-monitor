"""Tests for schema normalization and window bucketing."""

import pytest

from raimonitor.schema import is_positive, normalize_event, parse_timestamp
from raimonitor.windows import bucketize, parse_window, window_start_for


def _raw(**overrides):
    base = {
        "timestamp": "2026-09-01T12:00:00Z",
        "system": "demo",
        "decision": 1,
        "groups": {"sex": "Female"},
        "outcome": 1,
        "features": {"score": 0.5},
    }
    base.update(overrides)
    return base


def test_normalize_event_happy_path():
    event = normalize_event(_raw(), "decision_log")
    assert event["timestamp"] == "2026-09-01T12:00:00Z"
    assert event["decision"] == "1"
    assert event["outcome"] == "1"
    assert event["groups"] == {"sex": "Female"}
    assert event["source"] == "decision_log"


def test_normalize_event_missing_required():
    with pytest.raises(ValueError, match="decision"):
        normalize_event({"timestamp": "2026-09-01T00:00:00Z", "system": "s"}, "x")


def test_normalize_event_label_decisions():
    event = normalize_event(_raw(decision="refuse", outcome=None), "rag_audit")
    assert event["decision"] == "refuse"
    assert event["outcome"] is None
    assert not is_positive(event)
    assert is_positive(event, positive_label="refuse")


def test_parse_timestamp_offsets():
    assert parse_timestamp("2026-09-01T12:00:00+02:00").hour == 10
    assert parse_timestamp("2026-09-01 12:00:00").tzinfo is not None


def test_parse_window():
    assert parse_window("7D").days == 7
    assert parse_window("24h").total_seconds() == 86400
    assert parse_window("30m").total_seconds() == 1800
    with pytest.raises(ValueError):
        parse_window("fortnight")


def test_window_start_for_epoch_aligned():
    # 7-day windows are aligned to the Unix epoch (a Thursday), so the
    # window containing 2026-09-03 starts on 2026-09-03 itself.
    start = window_start_for("2026-09-03T15:30:00Z", parse_window("7D"))
    assert start.strftime("%Y-%m-%dT%H:%M:%SZ") == "2026-09-03T00:00:00Z"


def test_bucketize_sorted_and_deterministic():
    events = [
        normalize_event(_raw(timestamp="2026-09-08T01:00:00Z"), "x"),
        normalize_event(_raw(timestamp="2026-09-01T01:00:00Z"), "x"),
    ]
    buckets = bucketize(events, parse_window("7D"))
    assert list(buckets) == ["2026-08-27T00:00:00Z", "2026-09-03T00:00:00Z"]
    assert bucketize(events, parse_window("7D")) == buckets
