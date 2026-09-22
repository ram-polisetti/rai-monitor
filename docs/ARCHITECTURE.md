# Architecture

## Pipeline

```
decision log / opsaudit report / RAG audit trail
        │ ingest.py  →  normalized events (schema.py)
        ▼
windows.py  →  epoch-aligned buckets (7D, 24h, ...)
        ▼
metrics.py  →  per (system, window): n, decision rate,
               per-group rates, DIR, TPR/FPR gaps, accuracy/precision
        ├─► drift.py  →  PSI vs baseline window, decision-rate shift
        ▼
alerts.py  →  threshold rules × persistence  →  incidents
        ▼
incidents.py  →  append-only incidents.jsonl (dedupe, acknowledge)
        ▼
report.py  →  self-contained report.html (matplotlib PNGs as base64)
```

`cli.py` exposes each stage (`ingest`, `metrics`, `alerts`, `report`) and a
full-pipeline `run`. Stages are independently re-runnable: metrics are a pure
function of events, alerts a pure function of metrics.

## Components

**schema.py** — the canonical event:
`timestamp` (ISO-8601 UTC), `system`, `decision` (string-normalized, so
`1`/`0` and `answer`/`refuse` flow through the same code),
`groups` (attr → value), `outcome` (optional ground truth, same coding),
`features` (optional numerics for drift), `source` (which adapter).

**ingest.py** — three adapters into that schema:
- `decision_log`: CSV columns `timestamp, system, decision, outcome?`,
  `group_<attr>` → groups, `feature_<name>` → features; JSONL accepts the
  same keys or nested `groups`/`features` dicts.
- `opsaudit`: an opsaudit JSON report is an aggregate, so each audited group
  becomes one *snapshot event* (selection rate, TPR/FPR, n in `features`;
  group label parsed into `groups`; report flags preserved). Repeated audits
  of one system form a monitorable time series.
- `rag_audit`: a rag-governance-demo `audit_log.jsonl` record becomes an
  event with `groups={"domain": ...}`, `features={"top_score": ...}`.

**windows.py** — windows align to the Unix epoch (multiples of the window
size), so bucketing is deterministic regardless of run time.

**metrics.py** — per window and system. DIR = min(group rate)/max(group
rate); `None` when fewer than two groups or the max rate is 0. Error-rate
gaps use only events with outcomes. Undefined values stay `None`.

**drift.py** — PSI between baseline and current windows for the decision
distribution and each numeric feature (decile bins from the baseline, so bin
edges can't move with the data). Interpretation: <0.1 no significant shift,
0.1–0.25 moderate, >0.25 significant. Plus raw decision-rate shift.

**alerts.py** — rules name a metric, comparison, threshold, persistence
(consecutive windows), and severity. Incident ids are
`sha256(rule|system|window)` truncated — re-running never duplicates.

**incidents.py** — append-only JSONL; `acknowledge` flips status in place.

**report.py** — one self-contained HTML file; charts are matplotlib PNGs
embedded as base64 (requires the `report` extra).

## Alerting logic

A rule with `persistence: N` fires on the Nth consecutive window where the
condition holds, and keeps firing per window until the condition clears.
This suppresses single-window noise at the cost of N−1 windows of delay —
the documented trade-off. Severity (`warning`/`critical`) is a label for
triage, not an automated action.

## Limitations (session 1)

- Batch-oriented: no streaming ingestion, no live server, no auth.
- Drift compares two windows, not full history; no seasonal decomposition.
- Notifications are log-only (no email/Slack/webhook).
- Intersectional groups must be pre-combined upstream (e.g. `group_sex_race`).
- PSI bins need ≥10 baseline observations per feature, else the feature is skipped.
- Small groups: no minimum-n guard in session 1 — noisy rates on tiny groups
  are the operator's to interpret (a `--min-group-n` is roadmap).

## Roadmap (future sessions)

1. Live dashboard server with streaming ingestion (watch a directory / tail a log).
2. Cross-system comparison views and per-group time-series beyond DIR.
3. `--min-group-n` guard and confidence intervals on windowed rates.
4. Notification delivery (email, Slack webhook) for critical incidents.
5. Full-history drift (CUSUM / change-point detection) and seasonality handling.
6. Signed incident log entries (hash-chained, like opsaudit's sign-off chain).
7. Scheduled runs (cron/CI) with stateful last-window watermark.
