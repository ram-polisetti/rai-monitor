# Changelog

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
