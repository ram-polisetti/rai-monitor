# raimonitor — Responsible AI monitoring dashboard

> **Monitor production AI decisions for fairness drift, catch regressions, and keep an incident trail.** Built by an operator, for operators.

`raimonitor` ingests production AI decision logs, computes rolling fairness and performance metrics over time windows, detects distribution drift, raises persistent threshold alerts into an append-only incident log, and renders a self-contained HTML dashboard. It is AI-governance tooling for the whole lifecycle — not just the pre-deployment gate.

It speaks the fleet's language natively: decision-log CSV/JSONL plus first-class adapters for [opsaudit](https://github.com/ram-polisetti/opsaudit) audit reports and [rag-governance-demo](https://github.com/ram-polisetti/rag-governance-demo) audit trails.

## Quickstart (under a minute)

```bash
pip install "git+https://github.com/ram-polisetti/rai-monitor.git"
pip install "raimonitor[report] @ git+https://github.com/ram-polisetti/rai-monitor.git"  # charts

raimonitor run --input examples/sample_decisions.csv --format decision_log \
  --window 7D --rules examples/sample_rules.json --out-dir out/
```

This ingests 840 decisions, computes weekly fairness metrics, evaluates the alert rules, and writes `out/report.html` — a dashboard with DIR time-series, per-group breakdowns, and the incident list. The sample data contains an injected policy drift on day 15; the pipeline raises a `dir-below-80` incident for it.

Or run the stages separately:

```bash
raimonitor ingest  --input decisions.csv --format decision_log --out events.jsonl
raimonitor metrics --events events.jsonl --window 7D --out metrics.json
raimonitor alerts  --metrics metrics.json --rules rules.json --incidents incidents.jsonl
raimonitor report  --metrics metrics.json --incidents incidents.jsonl --out report.html
```

## Input formats

| `--format`      | Input | Notes |
|-----------------|-------|-------|
| `decision_log`  | CSV or JSONL | Columns: `timestamp, system, decision, outcome` (optional), `group_<attr>` per group attribute, `feature_<name>` per model feature. JSONL may use nested `groups`/`features` dicts. |
| `opsaudit`      | opsaudit JSON report | Each audited group becomes a snapshot event, so repeated audits form a time series. |
| `rag_audit`     | rag-governance-demo `audit_log.jsonl` | Each query decision becomes an event grouped by `domain`. |

All inputs normalize to one event schema — see `src/raimonitor/schema.py` and `docs/ARCHITECTURE.md`.

## Alert rules

```json
[
  {"name": "dir-below-80", "metric": "dir", "group_attr": "region",
   "op": "lt", "threshold": 0.8, "persistence": 2, "severity": "critical"}
]
```

Metrics: `dir`, `tpr_gap`, `fpr_gap`, `decision_rate`, `accuracy`, `volume`. A rule fires only after its condition holds for `persistence` consecutive windows — one noisy window pages nobody. Incidents are deterministic (id = hash of rule + system + window) and append-only; acknowledge with `raimonitor`'s incident log tooling (see `src/raimonitor/incidents.py`).

## Real-world validation

`examples/adult_validation.py` streams the [UCI Adult](https://archive.ics.uci.edu/dataset/2/adult) census dataset (retrieved 2026-09-22) as a 60-day simulated production feed with an injected day-41 policy change. Observed: baseline DIR(sex) 0.65–0.71 already trips the 0.8 rule (true positive on real data); after the policy change DIR collapses to 0.08–0.13 and the TPR-gap rule fires. Full methodology and numbers: `docs/VALIDATION.md`.

## Project layout

- `src/raimonitor/` — the package (`schema`, `ingest`, `windows`, `metrics`, `drift`, `alerts`, `incidents`, `report`, `cli`)
- `tests/` — 38 tests, all deterministic
- `docs/ARCHITECTURE.md` — component design, alerting logic, limitations
- `docs/VALIDATION.md` — real-data validation methodology and results
- `examples/` — sample data, rules, and the Adult validation script

## Limitations (session 1)

- Static HTML report only — no live server, no streaming ingestion, no auth.
- Metrics are computed per system; cross-system comparison views are not built yet.
- Drift detection compares two windows (baseline vs current), not full history.
- Incident notifications are log-only — no email/Slack/webhook delivery.
- Group attributes are treated as categorical; intersectional groups must be pre-combined upstream.

See `docs/ARCHITECTURE.md` for the roadmap. Metrics are signals for human review, not verdicts.

## License

Apache-2.0. Copyright 2026 Ram Charan Satya Sai Teja Polisetti.
