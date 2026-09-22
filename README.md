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

### Live monitoring

```bash
# Terminal 1: start the live server (tails decisions.jsonl, serves the dashboard)
raimonitor serve --input decisions.jsonl --rules examples/sample_rules.json \
  --state-dir live_state/ --port 8080 --interval 5

# Terminal 2: append decisions as they happen — the dashboard updates live
cat new_decisions.jsonl >> decisions.jsonl
```

Open http://127.0.0.1:8080 — the page auto-refreshes. JSON API:
`/api/metrics`, `/api/incidents`, `/api/status`. The server keeps a
byte-offset watermark in `live_state/state.json`, so restarts resume where
they left off without duplicating incidents (incident ids are
deterministic). The rules file is re-read on every poll, so thresholds can
be tuned without restarting.

### Evidence-quality guards

```bash
raimonitor run --input decisions.csv --format decision_log --window 7D \
  --rules rules.json --out-dir out/ \
  --min-group-n 50 --bootstrap 200 --bootstrap-seed 42
```

- `--min-group-n`: group values with fewer events are flagged
  **insufficient evidence** — their rates are reported as `None`, excluded
  from DIR/gap computations, and can never trip an alert rule. No more
  noisy rates on tiny groups.
- `--bootstrap N`: attaches 95% bootstrap confidence intervals to
  windowed decision rates and per-group TPR/FPR (deterministic for a
  fixed `--bootstrap-seed`; the CIs are identical in batch and live-server
  runs because each (window, system) pair gets its own seeded RNG).

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

`examples/adult_validation.py` streams the [UCI Adult](https://archive.ics.uci.edu/dataset/2/adult) census dataset (retrieved 2026-09-22) as a 60-day simulated production feed with an injected day-41 policy change. Observed: baseline DIR(sex) 0.65–0.71 already trips the 0.8 rule (true positive on real data); after the policy change DIR collapses to 0.08–0.13 and the TPR-gap rule fires. Session 2 extends it: `--min-group-n 100` flags the small race groups as insufficient evidence (9/9 windows for Amer-Indian-Eskimo and Other, 2 for Asian-Pacific-Islander) with identical incident counts, and `raimonitor serve` reproduces the batch metrics byte-for-byte over 30,000 streamed events, with no duplicate incidents after restart. Full methodology and numbers: `docs/VALIDATION.md`.

## Project layout

- `src/raimonitor/` — the package (`schema`, `ingest`, `windows`, `metrics`, `drift`, `alerts`, `incidents`, `report`, `evidence`, `server`, `cli`)
- `tests/` — 65 tests, all deterministic
- `docs/ARCHITECTURE.md` — component design, alerting logic, limitations
- `docs/VALIDATION.md` — real-data validation methodology and results
- `examples/` — sample data, rules, and the Adult validation script

## Limitations (session 2)

- Live server has no auth — bind to localhost or put it behind a reverse proxy.
- Drift detection compares two windows (baseline vs current), not full history.
- Incident notifications are log-only — no email/Slack/webhook delivery.
- Group attributes are treated as categorical; intersectional groups must be pre-combined upstream.
- CSV watch files must be line-oriented (no embedded newlines); JSONL is recommended for streaming.

See `docs/ARCHITECTURE.md` for the roadmap. Metrics are signals for human review, not verdicts.

## License

Apache-2.0. Copyright 2026 Ram Charan Satya Sai Teja Polisetti.
