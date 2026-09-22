"""Command-line interface for raimonitor.

Subcommands mirror the pipeline stages so each step can be run, inspected,
and re-run independently::

    raimonitor ingest  --input decisions.csv --format decision_log --out events.jsonl
    raimonitor metrics --events events.jsonl --window 7D --group-attr sex --out metrics.json
    raimonitor alerts  --metrics metrics.json --rules rules.json --incidents incidents.jsonl
    raimonitor report  --metrics metrics.json --incidents incidents.jsonl --out report.html
    raimonitor run     --input decisions.csv --format decision_log --window 7D \\
                       --rules rules.json --out-dir out/
    raimonitor serve   --input decisions.jsonl --rules rules.json \\
                       --state-dir state/ --port 8080
    raimonitor notify  --incidents incidents.jsonl --config notify.json \\
                       --state notified.json [--dry-run]
    raimonitor drift   --metrics metrics.json --metric dir --group-attr sex
    raimonitor verify-log --incidents incidents.jsonl
    raimonitor schedule --input decisions.jsonl --rules rules.json \\
                       --state-dir state/ --out-dir out/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .alerts import evaluate_rules
from .drift import detect_change_points
from .incidents import append_incidents, load_incidents, verify_log
from .ingest import ingest, read_events, write_events
from .metrics import compute_metrics
from .notifications import load_notify_config, notify_incidents
from .report import load_json, write_report
from .server import LiveMonitor, LiveServer


def _add_evidence_flags(p) -> None:
    p.add_argument("--min-group-n", type=int, default=1,
                   help="group values with fewer events are flagged as "
                        "insufficient evidence (default: 1, no guard)")
    p.add_argument("--bootstrap", type=int, default=0, metavar="REPS",
                   help="bootstrap reps for 95%% CIs on windowed rates "
                        "(default: 0, off)")
    p.add_argument("--bootstrap-seed", type=int, default=0,
                   help="seed for deterministic bootstrap CIs")


def _cmd_ingest(args) -> int:
    events = ingest(args.input, args.format)
    write_events(events, args.out)
    print(f"ingested {len(events)} events -> {args.out}")
    return 0


def _cmd_metrics(args) -> int:
    events = read_events(args.events)
    doc = compute_metrics(events, window=args.window,
                          positive_label=args.positive_label,
                          min_group_n=args.min_group_n,
                          bootstrap_reps=args.bootstrap,
                          bootstrap_seed=args.bootstrap_seed)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"computed {len(doc['windows'])} window(s) -> {args.out}")
    return 0


def _cmd_alerts(args) -> int:
    metrics_doc = load_json(args.metrics)
    rules = load_json(args.rules)
    if isinstance(rules, dict):
        rules = rules.get("rules", [])
    new_incidents = evaluate_rules(metrics_doc, rules)
    appended, skipped = append_incidents(new_incidents, args.incidents)
    print(f"raised {len(new_incidents)} incident(s): "
          f"{appended} appended, {skipped} already logged -> {args.incidents}")
    return 0


def _cmd_report(args) -> int:
    metrics_doc = load_json(args.metrics)
    incidents = load_incidents(args.incidents) if args.incidents else []
    write_report(metrics_doc, incidents, args.out, title=args.title)
    print(f"wrote dashboard -> {args.out}")
    return 0


def _cmd_run(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    metrics_path = out_dir / "metrics.json"
    incidents_path = out_dir / "incidents.jsonl"
    report_path = out_dir / "report.html"

    events = ingest(args.input, args.format)
    write_events(events, events_path)
    print(f"[1/4] ingested {len(events)} events")

    doc = compute_metrics(events, window=args.window,
                          positive_label=args.positive_label,
                          min_group_n=args.min_group_n,
                          bootstrap_reps=args.bootstrap,
                          bootstrap_seed=args.bootstrap_seed)
    metrics_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"[2/4] computed {len(doc['windows'])} window(s)")

    rules = load_json(args.rules)
    if isinstance(rules, dict):
        rules = rules.get("rules", [])
    new_incidents = evaluate_rules(doc, rules)
    appended, skipped = append_incidents(new_incidents, incidents_path)
    print(f"[3/4] {len(new_incidents)} incident(s) raised "
          f"({appended} new, {skipped} already logged)")

    write_report(doc, load_incidents(incidents_path), report_path)
    print(f"[4/4] dashboard -> {report_path}")
    return 0


def _resolve_auth_token(args) -> str | None:
    """Bearer token for `serve`: explicit flag wins, else an env var name."""
    if args.auth_token and args.auth_token_env:
        raise ValueError("use only one of --auth-token and --auth-token-env")
    if args.auth_token:
        return args.auth_token
    if args.auth_token_env:
        import os
        token = os.environ.get(args.auth_token_env)
        if not token:
            raise ValueError(
                f"environment variable {args.auth_token_env} is not set")
        return token
    return None


def _add_auth_flags(p) -> None:
    p.add_argument("--auth-token", default=None,
                   help="bearer token required by the dashboard and API "
                        "(prefer --auth-token-env: tokens in argv are "
                        "visible to other local users via ps)")
    p.add_argument("--auth-token-env", default=None, metavar="VAR",
                   help="read the bearer token from environment variable VAR")


def _cmd_notify(args) -> int:
    incidents = load_incidents(args.incidents)
    config = load_notify_config(args.config)
    state_path = Path(args.state) if args.state else None
    notified: set[str] = set()
    if state_path and state_path.exists():
        notified = set(json.loads(state_path.read_text(encoding="utf-8")))
    pending = [i for i in incidents if i["id"] not in notified]
    deliveries = notify_incidents(pending, config, dry_run=args.dry_run)
    sent = sum(1 for d in deliveries if d["status"] in ("sent", "dry-run"))
    failed = sum(1 for d in deliveries if d["status"] == "error")
    skipped = sum(1 for d in deliveries if d["status"] == "skipped")
    for d in deliveries:
        if d["status"] == "error":
            print(f"  ERROR {d['channel']}/{d['incident']}: {d['error']}")
    if not args.dry_run and state_path:
        for d in deliveries:
            if d["status"] == "sent":
                notified.add(d["incident"])
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(sorted(notified), indent=2) + "\n",
                              encoding="utf-8")
    print(f"notify: {sent} delivered, {failed} failed, {skipped} skipped "
          f"({len(pending)} unnotified incident(s) considered)"
          + (" [dry-run]" if args.dry_run else ""))
    return 1 if failed else 0


def _cmd_drift(args) -> int:
    doc = load_json(args.metrics)
    result = detect_change_points(
        doc.get("windows", []), args.metric, group_attr=args.group_attr,
        k=args.cusum_k, h=args.cusum_h,
        deseasonalize_dow=args.deseasonalize)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"wrote change-point findings -> {args.out}")
    else:
        print(json.dumps(result, indent=2))
    points = result["change_points"]
    print(f"{len(points)} change point(s) in {result['n_usable']} usable "
          f"window(s) for metric '{args.metric}'")
    for cp in points:
        print(f"  {cp['window_start'][:10]} {cp['direction']}: "
              f"{cp['previous_value']} -> {cp['value']}")
    return 0


def _cmd_verify_log(args) -> int:
    result = verify_log(args.incidents)
    print(f"records: {result['records']} "
          f"({result['signed']} signed, "
          f"{result['legacy_unsigned']} legacy unsigned)")
    if result["ok"]:
        print("incident log chain: OK")
        return 0
    print("incident log chain: BROKEN")
    for error in result["errors"]:
        print(f"  {error}")
    return 1


def _cmd_schedule(args) -> int:
    """One scheduled pass: consume new log rows, refresh metrics/report.

    Reuses the live monitor's watermark (byte offset + head hash in the
    state dir), so cron/CI invocations only process new rows and
    deterministic incident ids mean re-runs never re-alert.
    """
    monitor = LiveMonitor(
        args.input, args.rules, args.state_dir, format=args.format,
        window=args.window, positive_label=args.positive_label,
        min_group_n=args.min_group_n, bootstrap_reps=args.bootstrap,
        bootstrap_seed=args.bootstrap_seed, title=args.title)
    new_events = monitor.poll()
    snap = monitor.snapshot()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(
        json.dumps(snap["metrics"], indent=2) + "\n", encoding="utf-8")
    write_report(snap["metrics"], snap["incidents"], out_dir / "report.html",
                 title=args.title)
    status = snap["status"]
    print(f"schedule: {new_events} new event(s), "
          f"{status['incidents']} incident(s) total "
          f"({status['open_incidents']} open) -> {out_dir}")
    return 0


def _cmd_serve(args) -> int:
    auth_token = _resolve_auth_token(args)
    monitor = LiveMonitor(
        args.input, args.rules, args.state_dir, format=args.format,
        window=args.window, positive_label=args.positive_label,
        min_group_n=args.min_group_n, bootstrap_reps=args.bootstrap,
        bootstrap_seed=args.bootstrap_seed, title=args.title)
    server = LiveServer(monitor, host=args.host, port=args.port,
                        poll_interval=args.interval,
                        refresh_seconds=max(1, int(args.interval)),
                        auth_token=auth_token)
    server.start()
    print(f"raimonitor live dashboard: {server.url}")
    if auth_token:
        print("bearer-token auth enabled for dashboard and API")
    else:
        print("WARNING: no auth configured — bind to 127.0.0.1 only, "
              "or put the server behind a reverse proxy (see README)")
    print(f"watching {args.input} (poll every {args.interval}s); "
          f"state in {args.state_dir}")
    print("API: /api/metrics /api/incidents /api/status — Ctrl+C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="raimonitor",
        description="Responsible AI monitoring: fairness metrics, drift, and "
                    "alerting over production AI decision logs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="ingest a decision log / audit file into events")
    p.add_argument("--input", required=True)
    p.add_argument("--format", required=True,
                   choices=["decision_log", "opsaudit", "rag_audit"])
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("metrics", help="compute rolling window metrics")
    p.add_argument("--events", required=True)
    p.add_argument("--window", default="7D")
    p.add_argument("--positive-label", default="1")
    p.add_argument("--out", required=True)
    _add_evidence_flags(p)
    p.set_defaults(func=_cmd_metrics)

    p = sub.add_parser("alerts", help="evaluate alerting rules into the incident log")
    p.add_argument("--metrics", required=True)
    p.add_argument("--rules", required=True)
    p.add_argument("--incidents", required=True)
    p.set_defaults(func=_cmd_alerts)

    p = sub.add_parser("report", help="render the static HTML dashboard")
    p.add_argument("--metrics", required=True)
    p.add_argument("--incidents", default=None)
    p.add_argument("--out", required=True)
    p.add_argument("--title", default="Responsible AI monitoring dashboard")
    p.set_defaults(func=_cmd_report)

    p = sub.add_parser("run", help="run the full pipeline end to end")
    p.add_argument("--input", required=True)
    p.add_argument("--format", required=True,
                   choices=["decision_log", "opsaudit", "rag_audit"])
    p.add_argument("--window", default="7D")
    p.add_argument("--positive-label", default="1")
    p.add_argument("--rules", required=True)
    p.add_argument("--out-dir", required=True)
    _add_evidence_flags(p)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("serve", help="live dashboard: tail a decision log, "
                                     "recompute windows, serve auto-refreshing UI")
    p.add_argument("--input", required=True,
                   help="watched decision-log file (.jsonl or line-oriented .csv)")
    p.add_argument("--format", default="decision_log",
                   choices=["decision_log"])
    p.add_argument("--rules", required=True, help="alert rules JSON")
    p.add_argument("--state-dir", required=True,
                   help="watermark + accumulated events + incident log")
    p.add_argument("--window", default="7D")
    p.add_argument("--positive-label", default="1")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--interval", type=float, default=5.0,
                   help="seconds between log polls (also the page refresh)")
    p.add_argument("--title", default="Responsible AI monitoring — live")
    _add_evidence_flags(p)
    _add_auth_flags(p)
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("notify", help="deliver incidents via email / Slack "
                                      "webhook (off unless configured)")
    p.add_argument("--incidents", required=True)
    p.add_argument("--config", required=True,
                   help="notify config JSON (channels, min_severity)")
    p.add_argument("--state", default=None,
                   help="notified-ids state file (re-runs skip these)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be sent without sending")
    p.set_defaults(func=_cmd_notify)

    p = sub.add_parser("drift", help="CUSUM change-point detection over the "
                                     "full metric history of a metrics document")
    p.add_argument("--metrics", required=True)
    p.add_argument("--metric", required=True,
                   choices=["dir", "tpr_gap", "fpr_gap", "decision_rate",
                            "accuracy", "volume"])
    p.add_argument("--group-attr", default=None,
                   help="required for dir / tpr_gap / fpr_gap")
    p.add_argument("--cusum-k", type=float, default=0.5,
                   help="CUSUM slack in std units (default: 0.5)")
    p.add_argument("--cusum-h", type=float, default=5.0,
                   help="CUSUM decision threshold in std units (default: 5.0)")
    p.add_argument("--deseasonalize", action="store_true",
                   help="remove day-of-week baseline before detection")
    p.add_argument("--out", default=None)
    p.set_defaults(func=_cmd_drift)

    p = sub.add_parser("verify-log", help="verify the incident log hash chain")
    p.add_argument("--incidents", required=True)
    p.set_defaults(func=_cmd_verify_log)

    p = sub.add_parser("schedule", help="one scheduled pass: consume new log "
                                        "rows (stateful watermark), refresh "
                                        "metrics and the dashboard")
    p.add_argument("--input", required=True,
                   help="decision-log file (.jsonl or line-oriented .csv)")
    p.add_argument("--format", default="decision_log",
                   choices=["decision_log"])
    p.add_argument("--rules", required=True, help="alert rules JSON")
    p.add_argument("--state-dir", required=True,
                   help="watermark + accumulated events + incident log")
    p.add_argument("--out-dir", required=True,
                   help="where metrics.json and report.html are written")
    p.add_argument("--window", default="7D")
    p.add_argument("--positive-label", default="1")
    p.add_argument("--title", default="Responsible AI monitoring dashboard")
    _add_evidence_flags(p)
    p.set_defaults(func=_cmd_schedule)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"raimonitor: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
