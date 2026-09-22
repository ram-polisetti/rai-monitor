# Changelog

## 0.3.0 — 2026-09-22 (session 3)

- **Notification delivery** (`raimonitor notify`, `notifications.py`):
  email via stdlib SMTP and Slack via incoming webhooks, driven by a
  config file (`{email|sms-disabled, slack}: enabled, severity
  thresholds, SMTP/webhook settings`). Delivery is opt-in and off by
  default; dry-run mode prints the rendered message without sending.
  SMTP passwords and webhook URLs are passed by environment variable and
  never logged (redacted in errors); a failing channel never blocks the
  others. CLI keeps a notified-ID state file so re-runs don't resend.
- **Full-history drift** (`drift.py`, `raimonitor drift`): two-sided
  tabular CUSUM (k=0.5, h=5.0, Phase-I reference = first max(3, n//4)
  values) reports the first window of each new regime and re-baselines
  after each alarm so a sustained shift alarms exactly once.
  Day-of-week baselines + deseasonalization strip weekly seasonality
  before detection; thin weekdays fall back to the global mean
  (documented).
- **Tamper-evident incident log** (`incidents.py`): every record now
  carries `prev_hash` + `record_hash` (SHA-256 chain, canonical JSON).
  `raimonitor verify-log` re-hashes the chain and exits non-zero on any
  tamper (edited field, reorder, deletion); legacy unsigned records from
  earlier sessions are still readable and reported separately.
  `acknowledge` re-chains from the edited record. Known limitation:
  tail truncation is only caught against an externally stored expected
  record count.
- **Scheduled runs** (`raimonitor schedule`): a one-shot cron-friendly
  run that reuses the live monitor's state dir — byte-offset watermark,
  incremental window recompute, refreshed `metrics.json`/`report.html`;
  deterministic incident ids keep re-runs duplicate-free.
- **Live-server auth** (`server.py`): optional bearer token protects the
  dashboard and every API route (`--auth-token` / safer
  `--auth-token-env`, `hmac.compare_digest`, `WWW-Authenticate: Bearer`
  on 401). No-auth mode still works but prints a warning; add TLS via a
  reverse proxy.
- **Rule validation hardening** (`alerts.py`): `validate_rule` now
  rejects non-numeric thresholds (previously a string threshold raised
  `TypeError` inside the comparison at evaluation time) and rejects
  non-integer `persistence` values instead of silently truncating via
  `int()` (bools also rejected). Found by an Ollama Cloud
  `gpt-oss:120b` code review; the same review's claim that
  `evaluate_rules` mutates the caller's document via `sort()` was
  checked against the code and is a false positive (it sorts
  per-system lists built fresh with `setdefault`).
- Real-world validation extended: CUSUM over the Adult DIR(sex) history
  localizes the day-41 policy change to the **2026-02-05 window** (the
  first window containing day 41), and the batch incident log verifies
  as an intact hash chain. See `docs/VALIDATION.md`.
- Tests: 102/102 passing (65 session-1/2 + 37 new).

## 0.2.0 — 2026-09-22 (session 2)

- **Live dashboard server** (`raimonitor serve`): tails a decision-log
  file (JSONL or line-oriented CSV), recomputes only the touched windows,
  re-evaluates rules (rules file is live-reloaded), and serves an
  auto-refreshing dashboard plus a JSON API
  (`/api/metrics`, `/api/incidents`, `/api/status`) on stdlib
  `ThreadingHTTPServer` — zero new dependencies. Byte-offset watermark in
  `state.json` survives restarts; rotation/rewrite detection; deterministic
  incident ids mean restarts never duplicate incidents.
- **Cross-system comparison views**: DIR / TPR-gap / FPR-gap time-series
  with one line per system, per-group decision-rate time-series per
  (attribute, value), a cross-system latest-window comparison table, and
  per-system latest-window sections — in both the static report and the
  live dashboard.
- **Evidence-quality guards**: `--min-group-n` flags small group values as
  insufficient evidence (rates become explicit `None`, excluded from DIR,
  gaps, and alert rules); `--bootstrap N --bootstrap-seed S` attaches
  deterministic 95% bootstrap CIs to windowed decision rates and per-group
  TPR/FPR. CIs use per-(window, system) seeded RNGs, so batch and
  live-server runs produce identical numbers.
- Real-world validation extended: guards flag exactly the small race
  groups in the Adult feed with incident counts unchanged (10); the live
  server reproduces the batch metrics document byte-for-byte over 30,000
  streamed events and a restart produces no duplicate incidents.
  See `docs/VALIDATION.md`.
- Tests: 65/65 passing (38 session-1 + 27 new).

## 0.1.0 — 2026-09-22 (session 1)

Initial build:

- Ingestion adapters: decision-log CSV/JSONL, opsaudit JSON reports
  (per-group snapshot events), rag-governance-demo audit trails — all
  normalized to one event schema.
- Rolling metrics engine: epoch-aligned windows (`7D`, `24h`, `30m`, ...);
  per-window volume, positive-decision rate, per-group rates, disparate
  impact ratio, TPR/FPR gaps, accuracy/precision where outcomes exist.
  Deterministic; undefined rates are `None`, never silent defaults.
- Drift detection: Population Stability Index on the decision distribution
  and numeric features (baseline-decile bins), plus absolute decision-rate
  shift.
- Alerting: threshold rules with consecutive-window persistence
  (`dir`, `tpr_gap`, `fpr_gap`, `decision_rate`, `accuracy`, `volume`);
  deterministic incident ids; append-only JSONL incident log with dedupe
  and acknowledge.
- Static HTML dashboard: DIR/decision-rate/volume time-series (matplotlib
  PNGs embedded as base64), latest-window per-group tables, incident list.
- CLI: `ingest`, `metrics`, `alerts`, `report`, and full-pipeline `run`.
- Real-world validation: UCI Adult dataset streamed as a 60-day production
  feed with an injected day-41 policy change; dashboard catches the
  standing disparity (DIR 0.65–0.71) and the regression (DIR 0.08–0.13).
  See `docs/VALIDATION.md`.
- Tests: 38/38 passing. GitHub Actions CI on Python 3.10–3.12.