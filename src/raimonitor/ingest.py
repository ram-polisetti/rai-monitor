"""Ingestion adapters: every supported input becomes normalized decision events.

Supported inputs
----------------
1. ``decision_log`` — CSV or JSONL production decision logs. CSV columns::

       timestamp, system, decision, outcome (optional),
       group_<attr> (each becomes a group attribute),
       feature_<name> (each becomes a model feature)

   JSONL records use the same keys, or nested ``groups`` / ``features`` dicts.
2. ``opsaudit`` — an opsaudit JSON report (``save_report(..., ".json")``).
   Each audited group becomes one *snapshot event* carrying the group's
   selection rate and the report's provenance timestamp, so repeated audits
   of the same system form a monitorable time series. The report's gate
   flags are preserved on the event for the incident log.
3. ``rag_audit`` — a rag-governance-demo audit trail JSONL (``audit_log.jsonl``):
   records carry ``ts``, ``decision`` (answer/refuse/escalate/...), ``domain``,
   ``top_score``, ``reason``. Each becomes an event with
   ``groups={"domain": domain}`` and ``features={"top_score": ...}``.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .schema import normalize_event


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
    return records


def _event_from_mapping(mapping: dict, source: str) -> dict:
    groups = dict(mapping.get("groups") or {})
    features = dict(mapping.get("features") or {})
    for key, value in mapping.items():
        if key.startswith("group_") and value not in (None, ""):
            groups[key[len("group_"):]] = value
        elif key.startswith("feature_") and value not in (None, ""):
            features[key[len("feature_"):]] = value
    raw = {
        "timestamp": mapping.get("timestamp"),
        "system": mapping.get("system", "unknown"),
        "decision": mapping.get("decision"),
        "groups": groups,
        "outcome": mapping.get("outcome"),
        "features": features,
    }
    return normalize_event(raw, source)


def from_decision_log(path: str | Path) -> list[dict]:
    """Ingest a decision-log CSV or JSONL file into normalized events."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        records = [
            {k: (v if v != "" else None) for k, v in row.items()} for row in rows
        ]
    elif suffix in (".jsonl", ".ndjson"):
        records = _read_jsonl(path)
    else:
        raise ValueError(
            f"unsupported decision-log format {suffix!r}; use .csv or .jsonl"
        )
    return [_event_from_mapping(record, "decision_log") for record in records]


def _parse_opsaudit_group_label(label: str) -> dict[str, str]:
    """Split an opsaudit combined group label (``"sex=Female|race=Black"``)."""
    groups: dict[str, str] = {}
    for part in str(label).split("|"):
        if "=" in part:
            attr, _, value = part.partition("=")
            groups[attr.strip()] = value.strip()
    return groups


def from_opsaudit_report(path: str | Path) -> list[dict]:
    """Ingest an opsaudit JSON report as per-group snapshot events.

    The report is an aggregate (not per-decision rows), so each audited group
    becomes one snapshot event. The event's ``features`` carry the group's
    selection rate, TPR/FPR, and n; ``groups`` carries the parsed group label.
    The report timestamp comes from the provenance block when present.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    provenance = payload.get("provenance") or {}
    timestamp = provenance.get("timestamp") or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    context = payload.get("context") or {}
    system = str(context.get("system") or context.get("model") or "opsaudit-audit")
    events = []
    for group in payload.get("groups", []):
        raw = {
            "timestamp": timestamp,
            "system": system,
            "decision": "1",
            "groups": _parse_opsaudit_group_label(group.get("group", "")),
            "outcome": None,
            "features": {
                "selection_rate": group.get("selection_rate"),
                "tpr": group.get("tpr"),
                "fpr": group.get("fpr"),
                "n": group.get("n"),
                "disparate_impact_ratio": payload.get("disparate_impact_ratio"),
            },
        }
        event = normalize_event(raw, "opsaudit")
        event["audit_flags"] = payload.get("flags", [])
        events.append(event)
    return events


def from_rag_audit_trail(path: str | Path) -> list[dict]:
    """Ingest a rag-governance-demo audit trail JSONL into normalized events."""
    records = _read_jsonl(Path(path))
    events = []
    for record in records:
        features: dict = {}
        if record.get("top_score") is not None:
            features["top_score"] = record["top_score"]
        groups: dict[str, str] = {}
        if record.get("domain"):
            groups["domain"] = str(record["domain"])
        raw = {
            "timestamp": record.get("ts") or record.get("timestamp"),
            "system": str(record.get("system") or "rag-governance-demo"),
            "decision": record.get("decision") or record.get("verdict") or "unknown",
            "groups": groups,
            "outcome": None,
            "features": features,
        }
        event = normalize_event(raw, "rag_audit")
        if record.get("reason"):
            event["reason"] = str(record["reason"])
        events.append(event)
    return events


INGESTORS = {
    "decision_log": from_decision_log,
    "opsaudit": from_opsaudit_report,
    "rag_audit": from_rag_audit_trail,
}


def ingest(path: str | Path, format: str) -> list[dict]:
    """Ingest ``path`` using the named adapter (``decision_log``/``opsaudit``/``rag_audit``)."""
    try:
        ingestor = INGESTORS[format]
    except KeyError:
        raise ValueError(
            f"unknown ingest format {format!r}; choose from {sorted(INGESTORS)}"
        ) from None
    return ingestor(path)


def write_events(events: list[dict], path: str | Path) -> Path:
    """Write normalized events as JSONL."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
    return path


def read_events(path: str | Path) -> list[dict]:
    """Read normalized events JSONL."""
    return _read_jsonl(Path(path))
