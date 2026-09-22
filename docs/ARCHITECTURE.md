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
rate); `None` when fewer than two groups have sufficient evidence or the
max rate is 0. Error-rate gaps use only events with outcomes. Undefined
values stay `None`. `min_group_n` flags small group values as
`insufficient_evidence` (rates become `None`, excluded from DIR/gaps);
`bootstrap_reps`/`bootstrap_seed` attach 95% bootstrap CIs to decision
rates and per-group TPR/FPR. CIs use a per-(window, system) seeded RNG
(`evidence.window_rng`), so results are identical whether computed in one
batch or incrementally by the live server.

**drift.py** — PSI between baseline and current windows for the decision
distribution and each numeric feature (decile bins from the baseline, so bin
edges can't move with the data). Interpretation: <0.1 no significant shift,
0.1–0.25 moderate, >0.25 significant. Plus raw decision-rate shift. Session
3 added full-history drift: two-sided tabular CUSUM (`cusum`,
`detect_change_points`, `raimonitor drift`) with a Phase-I reference
(first `max(3, n//4)` values), k=0.5, h=5.0; change points are reported as
the first window of the new regime and the reference re-baselines after
each alarm so a sustained shift alarms once. `dow_baseline` /
`deseasonalize` strip day-of-week seasonality; thin weekdays fall back to
the global mean (documented, not silent).

**alerts.py** — rules name a metric, comparison, threshold, persistence
(consecutive windows), and severity. Incident ids are
`sha256(rule|system|window)` truncated — re-running never duplicates.
`validate_rule` rejects unknown metrics/ops, non-numeric thresholds,
non-integer `persistence` (bools included — no silent `int()` coercion),
and unknown severities at config time, so bad rules fail fast instead of
raising `TypeError` mid-evaluation.

**incidents.py** — append-only JSONL. Every record carries `prev_hash`
and `record_hash` (SHA-256 over the canonical JSON of the record, chained
to the previous record; the first points at `"GENESIS"`). `verify_log`
re-hashes the whole chain: edited fields, reordered records, or deletions
fail verification (`raimonitor verify-log` exits 1). Records written
before signing (legacy) are still readable and reported as
`legacy_unsigned`. `acknowledge` updates a record's status and re-chains
from that record onward. Known limitation: truncating the tail of the log
is only detectable against an externally stored expected record count —
keep one (e.g. in your backup manifest) if that threat matters.

**notifications.py** — opt-in delivery of incidents (`raimonitor notify`).
Email via stdlib `smtplib`, Slack via incoming webhooks (`urllib`).
Config file enables each channel with a minimum severity; secrets come
from environment variables and are redacted from logs/errors. Dry-run
mode renders without sending. Channels fail independently — one failing
channel never blocks the others. The CLI's notified-state file tracks
per-(channel, incident) pairs, so a failed channel is retried on the next
run instead of being marked done by a sibling channel's success (bare
incident ids from older state files are honored as all-channels-done).

**report.py** — one self-contained HTML file; charts are matplotlib PNGs
embedded as base64 (requires the `report` extra). Session 2 added
cross-system comparison: DIR / TPR-gap / FPR-gap time-series with one line
per system, per-group decision-rate time-series per (attribute, value),
a cross-system latest-window comparison table, per-system latest-window
sections with bootstrap CIs and insufficient-evidence markers in the
group tables. `build_report(..., refresh_seconds=N)` adds a meta-refresh
tag for the live dashboard.

**evidence.py** — the evidence-quality guards: `min_group_n` (small
groups get `insufficient_evidence` flags and `None` rates, excluded from
DIR/gaps/alerts) and bootstrap 95% CIs on windowed rates with a
deterministic per-(window, system) seeded RNG.

**server.py** — live monitoring: `LiveMonitor` tails the watched
decision-log file (byte-offset watermark in `state.json`, rotation and
in-place-rewrite detection), ingests new rows, recomputes only the touched
windows, re-evaluates rules from the (live-reloadable) rules file, and
persists events + incidents. `LiveServer` serves the auto-refreshing
dashboard at `/` and a JSON API (`/api/metrics`, `/api/incidents`,
`/api/status`) on stdlib `ThreadingHTTPServer` — no extra dependencies.
Restarts resume from the watermark; incident ids are deterministic, so no
duplicates. Session 3: optional bearer-token auth (`--auth-token` /
`--auth-token-env`) guards `/` and every API route with
`hmac.compare_digest` (401 + `WWW-Authenticate: Bearer` on failure);
no-auth mode prints a startup warning. No TLS — terminate at a reverse
proxy. Session 3 also added `raimonitor schedule`: a cron-friendly
one-shot run reusing the monitor's state dir (watermark, incremental
recompute, refreshed `metrics.json`/`report.html`).

## Alerting logic

A rule with `persistence: N` fires on the Nth consecutive window where the
condition holds, and keeps firing per window until the condition clears.
This suppresses single-window noise at the cost of N−1 windows of delay —
the documented trade-off. Severity (`warning`/`critical`) is a label for
triage, not an automated action.

## Limitations (session 3)

- No TLS on the live server — bearer auth protects the routes, but
  terminate TLS at a reverse proxy for anything beyond localhost.
- Intersectional groups must be pre-combined upstream (e.g. `group_sex_race`).
- PSI bins need ≥10 baseline observations per feature, else the feature is skipped.
- CSV watch files must be line-oriented (no embedded newlines in quoted
  fields); JSONL is recommended for streaming.
- Bootstrap CIs are resampling-based and cost CPU proportional to
  reps × window size; tune `--bootstrap` for very large windows.
- Incident-log tail truncation needs an externally stored expected record
  count to detect.
- Notifications are one-way delivery; no escalation chains or on-call
  rotation integration.
- CUSUM needs ≥3 windows and non-zero variance; its change-point estimate
  is the first window of the new regime, with detection lag of a few
  windows for small shifts.

## Roadmap (future sessions)

1. ~~Live dashboard server with streaming ingestion (watch a directory / tail a log).~~ — done in session 2
2. ~~Cross-system comparison views and per-group time-series beyond DIR.~~ — done in session 2
3. ~~`--min-group-n` guard and confidence intervals on windowed rates.~~ — done in session 2
4. ~~Notification delivery (email, Slack webhook) for critical incidents.~~ — done in session 3
5. ~~Full-history drift (CUSUM / change-point detection) and seasonality handling.~~ — done in session 3
6. ~~Signed incident log entries (hash-chained, like opsaudit's sign-off chain).~~ — done in session 3
7. ~~Scheduled runs (cron/CI) with stateful last-window watermark.~~ — done in session 3
8. ~~Authentication / TLS termination guidance for the live server.~~ — done in session 3 (bearer auth; TLS via reverse proxy)

Candidate next work: multi-channel escalation (PagerDuty/OpsGenie),
on-call rotation integration, finer-grained API scopes, anomaly
detection on incident rates, and backtesting alert rules against
historical windows.
