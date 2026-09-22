"""Real-world validation: UCI Adult dataset as a simulated production feed.

Methodology
-----------
- Dataset: UCI Adult / Census Income
  (https://archive.ics.uci.edu/dataset/2/adult, retrieved 2026-09-22).
  48,842 rows; after deterministic prep (strip whitespace, drop rows with
  "?" in the used columns) 32,561 rows are kept.
- Simulation: rows are deterministically shuffled (seed 42) and streamed as
  500 decisions/day over 60 days starting 2026-01-01, system "income-screen-v1".
- Predictor: a FIXED rule-based policy (no model training, per repo scope):
    days  1-40: predict ">50K" iff education-num >= 13 AND hours-per-week >= 40
    days 41-60: same rule, but for sex=Female the hours bar rises to >= 55
  The day-41 change emulates a real deployment event: a threshold tweak that
  disparately impacts one group.
- Ground truth: the actual income label. Groups: sex, race.
- Pipeline: raimonitor run --window 7D with rules:
    dir-below-80 (sex, DIR < 0.8, persistence 2, critical)
    tpr-gap-wide (sex, TPR gap > 0.2, persistence 2, warning)

Observed (2026-09-22 run): the baseline policy already carries disparate
impact on real data (DIR(sex) 0.65-0.71), so dir-below-80 fires from the
first windows — a true positive, not a false alarm. After the day-41 policy
change, DIR(sex) collapses to 0.08-0.13 and the TPR-gap rule fires on the
last two windows (observed gaps 0.361, 0.382). The dashboard catches both
the standing disparity and the deployment regression.

Run: python3 examples/adult_validation.py  (writes to examples/adult_out/)
"""

from __future__ import annotations

import csv
import json
import random
import subprocess
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "adult_out"
DATA_URL = "https://archive.ics.uci.edu/static/public/2/adult.zip"
ZIP_PATH = Path("/tmp/adult.zip")
START = date(2026, 1, 1)
DAYS = 60
PER_DAY = 500
SEED = 42
DRIFT_DAY = 41  # 1-based: policy change takes effect this day

COLUMNS = [
    "age", "workclass", "fnlwgt", "education", "education_num",
    "marital_status", "occupation", "relationship", "race", "sex",
    "capital_gain", "capital_loss", "hours_per_week", "native_country",
    "income",
]


def load_rows() -> list[dict]:
    if not ZIP_PATH.exists():
        import urllib.request
        print(f"downloading {DATA_URL} ...")
        urllib.request.urlretrieve(DATA_URL, ZIP_PATH)
    with zipfile.ZipFile(ZIP_PATH) as zf:
        text = zf.read("adult.data").decode()
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        values = [v.strip() for v in line.split(",")]
        row = dict(zip(COLUMNS, values))
        if any(row[c] == "?" for c in ("sex", "race", "education_num",
                                       "hours_per_week", "income")):
            continue
        row["education_num"] = int(row["education_num"])
        row["hours_per_week"] = int(row["hours_per_week"])
        rows.append(row)
    return rows


def predict(row: dict, day: int) -> int:
    """Fixed policy. From DRIFT_DAY the hours bar rises for Female applicants."""
    hours_bar = 40
    if day >= DRIFT_DAY and row["sex"] == "Female":
        hours_bar = 55
    return int(row["education_num"] >= 13 and row["hours_per_week"] >= hours_bar)


def main() -> int:
    rows = load_rows()
    print(f"prepared rows: {len(rows)}")
    rng = random.Random(SEED)
    rng.shuffle(rows)
    feed = rows[: DAYS * PER_DAY]

    OUT.mkdir(parents=True, exist_ok=True)
    log_path = OUT / "decisions.csv"
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "system", "decision", "outcome",
                         "group_sex", "group_race",
                         "feature_hours_per_week", "feature_education_num"])
        for idx, row in enumerate(feed):
            day = idx // PER_DAY + 1
            ts = (START + timedelta(days=day - 1)).strftime("%Y-%m-%dT12:00:00Z")
            decision = predict(row, day)
            outcome = int(row["income"] == ">50K")
            writer.writerow([ts, "income-screen-v1", decision, outcome,
                             row["sex"], row["race"],
                             row["hours_per_week"], row["education_num"]])
    print(f"wrote {len(feed)} decisions -> {log_path}")

    rules_path = OUT / "rules.json"
    rules_path.write_text(json.dumps([
        {"name": "dir-below-80", "metric": "dir", "group_attr": "sex",
         "op": "lt", "threshold": 0.8, "persistence": 2, "severity": "critical"},
        {"name": "tpr-gap-wide", "metric": "tpr_gap", "group_attr": "sex",
         "op": "gt", "threshold": 0.2, "persistence": 2, "severity": "warning"},
    ], indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        [sys.executable, "-m", "raimonitor.cli", "run",
         "--input", str(log_path), "--format", "decision_log",
         "--window", "7D", "--rules", str(rules_path),
         "--out-dir", str(OUT / "pipeline")],
        check=True,
    )

    # Summarize: DIR(sex) per window + incidents.
    metrics = json.loads((OUT / "pipeline" / "metrics.json").read_text())
    print("\nwindow_start  n      DIR(sex)")
    for w in metrics["windows"]:
        dir_sex = (w["groups"].get("sex") or {}).get("disparate_impact_ratio")
        print(f"{w['window_start'][:10]}  {w['n']:5d}  "
              f"{'n/a' if dir_sex is None else f'{dir_sex:.3f}'}")
    incidents_path = OUT / "pipeline" / "incidents.jsonl"
    incidents = [json.loads(line) for line in
                 incidents_path.read_text(encoding="utf-8").splitlines()]
    print(f"\nincidents raised: {len(incidents)}")
    for inc in incidents:
        print(f"  {inc['id']} {inc['rule']} window={inc['window_start'][:10]} "
              f"observed={inc['observed']:.3f} severity={inc['severity']}")
    print(f"\ndashboard: {OUT / 'pipeline' / 'report.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
