"""Incident log: append-only JSONL storage for raised incidents.

Incidents are the durable output of the alerting rules. The log is
append-only — acknowledging an incident rewrites its ``status`` field but
never deletes the record, so the full history of what fired and when is
preserved for audit.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_incidents(incidents: list[dict], path: str | Path) -> tuple[int, int]:
    """Append incidents to the JSONL log, skipping ids already present.

    Returns ``(appended, skipped)``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {inc["id"] for inc in load_incidents(path)}
    appended = skipped = 0
    with open(path, "a", encoding="utf-8") as f:
        for incident in incidents:
            if incident["id"] in existing:
                skipped += 1
                continue
            record = {**incident, "raised_at": _utcnow()}
            f.write(json.dumps(record) + "\n")
            existing.add(incident["id"])
            appended += 1
    return appended, skipped


def load_incidents(path: str | Path) -> list[dict]:
    """Load all incidents from the JSONL log (empty list if missing)."""
    path = Path(path)
    if not path.exists():
        return []
    incidents = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                incidents.append(json.loads(line))
    return incidents


def acknowledge(incident_id: str, path: str | Path, note: str = "") -> bool:
    """Mark an incident acknowledged. Returns True if the id was found."""
    path = Path(path)
    incidents = load_incidents(path)
    found = False
    for incident in incidents:
        if incident["id"] == incident_id:
            incident["status"] = "acknowledged"
            incident["acknowledged_at"] = _utcnow()
            if note:
                incident["ack_note"] = note
            found = True
    if found:
        with open(path, "w", encoding="utf-8") as f:
            for incident in incidents:
                f.write(json.dumps(incident) + "\n")
    return found
