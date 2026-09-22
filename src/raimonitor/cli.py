"""Command-line interface for raimonitor.

Subcommands mirror the pipeline stages so each step can be run, inspected,
and re-run independently::

    raimonitor ingest  --input decisions.csv --format decision_log --out events.jsonl
    raimonitor metrics --events events.jsonl --window 7D --group-attr sex --out metrics.json
    raimonitor alerts  --metrics metrics.json --rules rules.json --incidents incidents.jsonl
    raimonitor report  --metrics metrics.json --incidents incidents.jsonl --out report.html
    raimonitor run     --input decisions.csv --format decision_log --window 7D \\
                       --rules rules.json --out-dir out/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .alerts import evaluate_rules
from .incidents import append_incidents, load_incidents
from .ingest import ingest, read_events, write_events
from .metrics import compute_metrics
from .report import load_json, write_report


def _cmd_ingest(args) -> int:
    events = ingest(args.input, args.format)
    write_events(events, args.out)
    print(f"ingested {len(events)} events -> {args.out}")
    return 0


def _cmd_metrics(args) -> int:
    events = read_events(args.events)
    doc = compute_metrics(events, window=args.window,
                          positive_label=args.positive_label)
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
                          positive_label=args.positive_label)
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
    p.set_defaults(func=_cmd_run)
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
