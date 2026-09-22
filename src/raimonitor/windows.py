"""Time-window bucketing for rolling metrics.

Windows are aligned to the Unix epoch (window boundaries fall on exact
multiples of the window size), which keeps bucketing deterministic
regardless of when the pipeline runs.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .schema import parse_timestamp

_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([mhdwMHDW])\s*$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_window(spec: str) -> timedelta:
    """Parse a window spec like ``"7D"``, ``"24h"``, ``"30m"`` into a timedelta."""
    match = _WINDOW_RE.match(spec)
    if not match:
        raise ValueError(
            f"invalid window spec {spec!r}; expected like '7D', '24h', '30m'"
        )
    amount, unit = int(match.group(1)), match.group(2).lower()
    if amount <= 0:
        raise ValueError(f"window amount must be positive, got {spec!r}")
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])


def window_start_for(timestamp: str, window: timedelta) -> datetime:
    """Return the epoch-aligned window start containing ``timestamp``."""
    dt = parse_timestamp(timestamp)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    size = int(window.total_seconds())
    index = int((dt - epoch).total_seconds()) // size
    return epoch + timedelta(seconds=index * size)


def bucketize(events: list[dict], window: timedelta) -> dict[str, list[dict]]:
    """Group normalized events by epoch-aligned window start (ISO string)."""
    buckets: dict[str, list[dict]] = {}
    for event in events:
        start = window_start_for(event["timestamp"], window)
        key = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        buckets.setdefault(key, []).append(event)
    return dict(sorted(buckets.items()))
