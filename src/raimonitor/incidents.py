"""Incident log: append-only, hash-chained JSONL storage for raised incidents.

Incidents are the durable output of the alerting rules. The log is
append-only — acknowledging an incident rewrites its ``status`` field but
never deletes the record, so the full history of what fired and when is
preserved for audit.

**Tamper evidence.** Every record carries ``prev_hash`` (the previous
record's ``record_hash``, or ``"GENESIS"`` for the first) and
``record_hash`` — the SHA-256 of the record's canonical JSON *without*
``record_hash`` itself. Any edit, reorder, or deletion breaks the chain,
and ``verify_log`` / ``raimonitor verify-log`` reports it. Records written
before signing existed verify as ``legacy_unsigned``: readable, but not
covered by the chain. ``acknowledge`` re-chains the log from the modified
record onward (documented, not silent) so the file always verifies after a
legitimate status change.

What chaining does *not* cover: truncation of the tail is invisible to the
chain alone (the remaining records still link correctly). Operators who
need that guarantee should record the expected record count externally
(e.g. in the scheduled-run state file) and compare on verify.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "GENESIS"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(record: dict) -> str:
    """Canonical JSON of a record excluding its own ``record_hash``."""
    body = {k: v for k, v in record.items() if k != "record_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def sign_record(record: dict, prev_hash: str) -> dict:
    """Return a copy of ``record`` with ``prev_hash``/``record_hash`` set."""
    signed = {**record, "prev_hash": prev_hash}
    signed["record_hash"] = hashlib.sha256(
        _canonical(signed).encode("utf-8")).hexdigest()
    return signed


def _last_hash(path: Path) -> str:
    records = load_incidents(path)
    for record in reversed(records):
        if record.get("record_hash"):
            return record["record_hash"]
    return GENESIS


def append_incidents(incidents: list[dict], path: str | Path) -> tuple[int, int]:
    """Append incidents to the JSONL log, skipping ids already present.

    Each appended record is hash-chained to the previous signed record.
    Returns ``(appended, skipped)``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {inc["id"] for inc in load_incidents(path)}
    appended = skipped = 0
    prev_hash = _last_hash(path)
    with open(path, "a", encoding="utf-8") as f:
        for incident in incidents:
            if incident["id"] in existing:
                skipped += 1
                continue
            record = sign_record({**incident, "raised_at": _utcnow()},
                                 prev_hash)
            f.write(json.dumps(record) + "\n")
            prev_hash = record["record_hash"]
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


def verify_log(path: str | Path) -> dict:
    """Verify the hash chain of an incident log.

    Returns ``{"ok", "records", "signed", "legacy_unsigned", "errors"}``.
    ``ok`` is False when any signed record's hash or ``prev_hash`` linkage
    fails. Pre-signing records are reported under ``legacy_unsigned`` and
    do not fail verification.
    """
    records = load_incidents(path)
    errors: list[str] = []
    signed = 0
    legacy = 0
    prev_hash = GENESIS
    for i, record in enumerate(records):
        label = f"record {i} (id={record.get('id', '?')})"
        stored = record.get("record_hash")
        if not stored:
            legacy += 1
            continue
        signed += 1
        if record.get("prev_hash") != prev_hash:
            errors.append(f"{label}: prev_hash mismatch "
                          f"(chain broken before this record)")
        recomputed = hashlib.sha256(
            _canonical(record).encode("utf-8")).hexdigest()
        if recomputed != stored:
            errors.append(f"{label}: record_hash mismatch (record was modified)")
        prev_hash = stored
    return {"ok": not errors, "records": len(records), "signed": signed,
            "legacy_unsigned": legacy, "errors": errors}


def acknowledge(incident_id: str, path: str | Path, note: str = "") -> bool:
    """Mark an incident acknowledged. Returns True if the id was found.

    The log is re-chained from the modified record onward so the file
    still verifies afterwards; the re-chain is visible in the new hashes.
    """
    path = Path(path)
    incidents = load_incidents(path)
    found = changed_at = None
    for i, incident in enumerate(incidents):
        if incident["id"] == incident_id:
            incident["status"] = "acknowledged"
            incident["acknowledged_at"] = _utcnow()
            if note:
                incident["ack_note"] = note
            found = True
            changed_at = i
    if not found:
        return False
    # Re-chain from the modified record onward.
    prev_hash = GENESIS
    for i, incident in enumerate(incidents):
        if i < changed_at and incident.get("record_hash"):
            prev_hash = incident["record_hash"]
            continue
        if i >= changed_at and not incident.get("record_hash"):
            # Legacy unsigned record touched by ack: sign it so the chain
            # stays continuous; it is no longer reported as legacy.
            pass
        stripped = {k: v for k, v in incident.items()
                    if k not in ("prev_hash", "record_hash")}
        incidents[i] = sign_record(stripped, prev_hash)
        prev_hash = incidents[i]["record_hash"]
    with open(path, "w", encoding="utf-8") as f:
        for incident in incidents:
            f.write(json.dumps(incident) + "\n")
    return True
