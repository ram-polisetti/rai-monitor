"""Live monitoring server with streaming ingestion.

Watches a decision-log file (JSONL recommended; CSV must be line-oriented —
no embedded newlines) for appended rows, recomputes the affected windows,
re-evaluates the alert rules, and serves a live-updating dashboard plus a
small JSON API::

    GET /              live dashboard (auto-refreshing HTML report)
    GET /api/metrics   the full windowed-metrics document
    GET /api/incidents the incident list
    GET /api/status    watermark, counts, last poll time

State lives in the state directory:

- ``events.jsonl`` — every normalized event consumed so far
- ``incidents.jsonl`` — the append-only incident log (shared with the CLI)
- ``state.json`` — the byte offset consumed from the watched log, so a
  restart resumes exactly where it left off

Restarts never duplicate incidents: incident ids are deterministic
(rule + system + window), and ``append_incidents`` skips ids already in
the log. The alert rules file is re-read on every poll, so an operator can
tune thresholds without restarting the server.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .alerts import evaluate_rules
from .evidence import window_rng
from .incidents import append_incidents, load_incidents
from .ingest import _event_from_mapping
from .metrics import compute_metrics, compute_window_metrics
from .report import build_report
from .windows import bucketize, parse_window


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LiveMonitor:
    """Streaming monitor: tails a log, keeps windowed metrics current."""

    def __init__(self, input_path: str | Path, rules_path: str | Path,
                 state_dir: str | Path, format: str = "decision_log",
                 window: str = "7D", positive_label: str = "1",
                 min_group_n: int = 1, bootstrap_reps: int = 0,
                 bootstrap_seed: int = 0,
                 title: str = "Responsible AI monitoring — live"):
        self.input_path = Path(input_path)
        self.rules_path = Path(rules_path)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if format not in ("decision_log",):
            raise ValueError(
                f"live monitoring supports 'decision_log' input, got {format!r}")
        self.format = format
        self.window = window
        self.positive_label = positive_label
        self.min_group_n = min_group_n
        self.bootstrap_reps = bootstrap_reps
        self.bootstrap_seed = bootstrap_seed
        self.title = title

        self._events_path = self.state_dir / "events.jsonl"
        self._incidents_path = self.state_dir / "incidents.jsonl"
        self._state_path = self.state_dir / "state.json"

        # window_start -> list of events (rebuilt from events.jsonl on start)
        self._buckets: dict[str, list[dict]] = {}
        self._events: list[dict] = []
        self._metrics: dict = {}
        self._incidents: list[dict] = []
        self._offset = 0
        self._head_hash: str | None = None
        self._csv_header_seen = False
        self._last_poll: str | None = None
        self._lock = threading.Lock()
        self._recover()

    # -- state ----------------------------------------------------------
    def _recover(self) -> None:
        """Rebuild in-memory state from the state directory (restart-safe)."""
        if self._events_path.exists():
            for line in self._events_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(json.loads(line))
        parsed = parse_window(self.window)
        for start, bucket in bucketize(self._events, parsed).items():
            self._buckets[start] = bucket
        state = {}
        if self._state_path.exists():
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        self._offset = int(state.get("offset", 0))
        self._head_hash = state.get("head_hash")
        self._csv_header_seen = bool(state.get("csv_header_seen", False))
        self._recompute_all()
        self._incidents = load_incidents(self._incidents_path)
        # Evaluate once on recovery so alerts stay current with the rules file.
        self._evaluate_rules()

    def _persist_state(self) -> None:
        self._state_path.write_text(json.dumps({
            "offset": self._offset,
            "head_hash": self._head_hash,
            "csv_header_seen": self._csv_header_seen,
            "events": len(self._events),
            "last_poll": self._last_poll,
        }, indent=2) + "\n", encoding="utf-8")

    def _persist_events(self) -> None:
        with open(self._events_path, "w", encoding="utf-8") as f:
            for event in self._events:
                f.write(json.dumps(event) + "\n")

    # -- ingestion ------------------------------------------------------
    def _reset_log_state(self) -> None:
        """Forget everything consumed from the current log file.

        Used when the watched file is rotated, truncated, or rewritten:
        the previous bytes are gone, so accumulated events from them would
        be stale. The incident log is untouched — history stays auditable.
        """
        self._offset = 0
        self._head_hash = None
        self._csv_header_seen = False
        if hasattr(self, "_header_fields"):
            del self._header_fields
        self._events = []
        self._buckets = {}
        self._persist_events()
        self._recompute_all()

    def _read_new_bytes(self) -> bytes:
        if not self.input_path.exists():
            return b""
        size = self.input_path.stat().st_size
        with open(self.input_path, "rb") as f:
            if size < self._offset:
                # Log was rotated or truncated: treat it as a fresh log.
                self._reset_log_state()
            elif size == self._offset and self._offset > 0:
                # Same size, no growth: a changed head means the file was
                # rewritten in place.
                head = f.read(min(1024, size))
                head_hash = hashlib.sha256(head).hexdigest()
                if self._head_hash is not None and head_hash != self._head_hash:
                    self._reset_log_state()
            if self._head_hash is None:
                f.seek(0)
                self._head_hash = hashlib.sha256(
                    f.read(min(1024, size))).hexdigest()
            f.seek(self._offset)
            chunk = f.read()
        return chunk

    def _parse_chunk(self, chunk: bytes) -> list[dict]:
        text = chunk.decode("utf-8")
        suffix = self.input_path.suffix.lower()
        if suffix == ".csv":
            return self._parse_csv_chunk(text)
        return self._parse_jsonl_chunk(text)

    def _parse_jsonl_chunk(self, text: str) -> list[dict]:
        events = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            events.append(_event_from_mapping(json.loads(line), "decision_log"))
        return events

    def _parse_csv_chunk(self, text: str) -> list[dict]:
        # Line-oriented tailing: CSV watch files must not contain embedded
        # newlines inside quoted fields.
        lines = [line for line in text.splitlines() if line.strip()]
        if not self._csv_header_seen:
            if not lines:
                return []
            self._header_fields = next(csv.reader(io.StringIO(lines[0])))
            lines = lines[1:]
            self._csv_header_seen = True
        header_fields = getattr(self, "_header_fields", None)
        if header_fields is None:
            raise RuntimeError("CSV header missing: start the log from offset 0")
        events = []
        for row in csv.reader(io.StringIO("\n".join(lines))):
            record = dict(zip(header_fields, row))
            record = {k: (v if v != "" else None) for k, v in record.items()}
            events.append(_event_from_mapping(record, "decision_log"))
        return events

    def poll(self) -> int:
        """Consume newly appended log rows; return the number of new events."""
        with self._lock:
            chunk = self._read_new_bytes()
            if not chunk.strip():
                self._last_poll = _utcnow()
                self._persist_state()
                return 0
            new_events = self._parse_chunk(chunk)
            self._offset += len(chunk)
            self._events.extend(new_events)
            self._persist_events()

            # Incremental metrics: only the touched windows are recomputed.
            parsed = parse_window(self.window)
            touched: set[str] = set()
            for start, bucket in bucketize(new_events, parsed).items():
                self._buckets.setdefault(start, []).extend(bucket)
                touched.add(start)
            for start in touched:
                self._recompute_window(start, parsed)

            self._evaluate_rules()
            self._last_poll = _utcnow()
            self._persist_state()
            return len(new_events)

    # -- metrics / alerts ------------------------------------------------
    def _window_metrics_for(self, start: str) -> list[dict]:
        bucket = self._buckets[start]
        by_system: dict[str, list[dict]] = {}
        for event in bucket:
            by_system.setdefault(event["system"], []).append(event)
        out = []
        for system in sorted(by_system):
            rng = (window_rng(self.bootstrap_seed, start, system)
                   if self.bootstrap_reps > 0 else None)
            metrics = compute_window_metrics(
                by_system[system], self.positive_label,
                min_group_n=self.min_group_n,
                bootstrap_reps=self.bootstrap_reps, rng=rng)
            out.append({"window_start": start, **metrics})
        return out

    def _recompute_window(self, start: str, parsed=None) -> None:
        windows = [w for w in self._metrics.get("windows", [])
                   if w["window_start"] != start]
        windows.extend(self._window_metrics_for(start))
        windows.sort(key=lambda w: (w["window_start"], w["system"]))
        self._metrics["windows"] = windows

    def _recompute_all(self) -> None:
        self._metrics = compute_metrics(
            self._events, window=self.window, positive_label=self.positive_label,
            min_group_n=self.min_group_n, bootstrap_reps=self.bootstrap_reps,
            bootstrap_seed=self.bootstrap_seed)

    def _evaluate_rules(self) -> None:
        rules = json.loads(self.rules_path.read_text(encoding="utf-8"))
        if isinstance(rules, dict):
            rules = rules.get("rules", [])
        new_incidents = evaluate_rules(self._metrics, rules)
        append_incidents(new_incidents, self._incidents_path)
        self._incidents = load_incidents(self._incidents_path)

    # -- read API (used by the HTTP layer and tests) ----------------------
    def snapshot(self) -> dict:
        """Thread-safe copy of the current dashboard state."""
        with self._lock:
            return {
                "metrics": json.loads(json.dumps(self._metrics)),
                "incidents": [dict(i) for i in self._incidents],
                "status": self.status(),
            }

    def status(self) -> dict:
        open_count = sum(1 for i in self._incidents
                         if i.get("status") == "open")
        return {
            "input": str(self.input_path),
            "log_offset_bytes": self._offset,
            "events": len(self._events),
            "windows": len(self._metrics.get("windows", [])),
            "incidents": len(self._incidents),
            "open_incidents": open_count,
            "last_poll": self._last_poll,
        }


def make_handler(monitor: LiveMonitor, refresh_seconds: int = 5):
    """Build an HTTP request handler class bound to ``monitor``."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the server quiet
            pass

        def _send(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            snap = monitor.snapshot()
            if self.path in ("/", "/index.html"):
                html = build_report(
                    snap["metrics"], snap["incidents"],
                    title=monitor.title, refresh_seconds=refresh_seconds)
                self._send(html.encode("utf-8"),
                            "text/html; charset=utf-8")
            elif self.path == "/api/metrics":
                self._send(json.dumps(snap["metrics"]).encode(),
                            "application/json")
            elif self.path == "/api/incidents":
                self._send(json.dumps(snap["incidents"]).encode(),
                            "application/json")
            elif self.path == "/api/status":
                self._send(json.dumps(snap["status"]).encode(),
                            "application/json")
            else:
                self.send_error(404, "not found")

    return Handler


class LiveServer:
    """HTTP server wrapping a :class:`LiveMonitor` with a poll loop."""

    def __init__(self, monitor: LiveMonitor, host: str = "127.0.0.1",
                 port: int = 8080, poll_interval: float = 5.0,
                 refresh_seconds: int = 5):
        self.monitor = monitor
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self._httpd = ThreadingHTTPServer(
            (host, port), make_handler(monitor, refresh_seconds))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.monitor.poll()
            except Exception as exc:  # never kill the loop on bad rows
                print(f"raimonitor serve: poll error: {exc}")

    def start(self) -> "LiveServer":
        poller = threading.Thread(target=self._poll_loop, daemon=True,
                                  name="raimonitor-poll")
        server = threading.Thread(target=self._httpd.serve_forever, daemon=True,
                                  name="raimonitor-http")
        self._threads = [poller, server]
        poller.start()
        # One poll up front so the dashboard is populated immediately.
        self.monitor.poll()
        server.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
