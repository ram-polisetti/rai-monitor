"""Normalized decision-event schema and validation.

Every input adapter (decision logs, opsaudit reports, RAG audit trails)
produces events in this shape so the metrics engine, drift detector, and
alerting rules work on one canonical representation.

Event schema::

    {
        "timestamp": "2026-09-01T00:00:00Z",   # ISO-8601 UTC, required
        "system": "hiring-screen-v3",           # model/system name, required
        "decision": "1",                        # normalized to str, required
        "groups": {"sex": "Female"},            # group attribute -> value
        "outcome": "1",                         # ground truth, same coding; None if unknown
        "features": {"score": 0.72},            # numeric/categorical model features; optional
        "source": "decision_log",               # which adapter produced this event
    }

Decisions and outcomes are normalized to strings so binary (0/1), boolean,
and label decisions ("answer"/"refuse"/"escalate") all flow through the same
code path. A decision is "positive" when it equals the event's
``positive_label`` (default ``"1"``); callers working with label decisions
treat every distinct decision value as its own rate series.
"""

from __future__ import annotations

from datetime import datetime, timezone

REQUIRED_FIELDS = ("timestamp", "system", "decision")
OPTIONAL_FIELDS = ("groups", "outcome", "features", "source")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts ``Z`` suffixes and explicit offsets. Naive timestamps are
    assumed to be UTC.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_event(raw: dict, source: str) -> dict:
    """Validate and normalize one raw event dict into the canonical schema."""
    if not isinstance(raw, dict):
        raise ValueError(f"event must be a dict, got {type(raw).__name__}")
    for field in REQUIRED_FIELDS:
        if field not in raw or raw[field] is None or str(raw[field]) == "":
            raise ValueError(f"event missing required field: {field!r}")
    timestamp = parse_timestamp(str(raw["timestamp"]))
    groups = raw.get("groups") or {}
    if not isinstance(groups, dict):
        raise ValueError("event 'groups' must be a dict")
    features = raw.get("features") or {}
    if not isinstance(features, dict):
        raise ValueError("event 'features' must be a dict")
    outcome = raw.get("outcome")
    return {
        "timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "system": str(raw["system"]),
        "decision": str(raw["decision"]),
        "groups": {str(k): str(v) for k, v in groups.items()},
        "outcome": None if outcome is None else str(outcome),
        "features": dict(features),
        "source": source,
    }


def is_positive(event: dict, positive_label: str = "1") -> bool:
    """Return True when the event's decision equals the positive label."""
    return event["decision"] == positive_label
