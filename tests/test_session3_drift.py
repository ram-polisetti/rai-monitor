"""Tests for full-history drift: CUSUM change points + day-of-week baselines."""

import json

import pytest

from raimonitor.cli import main
from raimonitor.drift import (
    cusum,
    deseasonalize,
    detect_change_points,
    dow_baseline,
)


def test_cusum_detects_level_shift():
    values = [1.0] * 20 + [3.0] * 20
    assert cusum(values) == [20], "change point should be the shift index"


def test_cusum_no_false_positive_on_flat_series():
    values = [1.0 + 0.01 * ((i * 7) % 3 - 1) for i in range(40)]
    assert cusum(values) == []


def test_cusum_detects_shift_down():
    values = [5.0] * 20 + [1.0] * 20
    assert cusum(values) == [20]


def test_cusum_needs_data():
    with pytest.raises(ValueError):
        cusum([1.0, 2.0])
    with pytest.raises(ValueError):
        cusum([1.0] * 10)


def test_dow_baseline_and_fallback():
    # 2026-01-07 and 2026-01-14 are Wednesdays (weekday index 2).
    series = [(f"2026-01-{d:02d}T12:00:00Z", 10.0 if d % 7 else 50.0)
              for d in range(1, 15)]  # two weeks
    baseline = dow_baseline(series)
    assert baseline["_fallback_weekdays"] == []
    assert baseline[2] == pytest.approx(50.0)
    assert baseline[0] == pytest.approx(10.0)


def test_dow_baseline_falls_back_to_global_mean():
    series = [("2026-01-05T12:00:00Z", 4.0),   # Monday
              ("2026-01-12T12:00:00Z", 6.0)]  # Monday again
    baseline = dow_baseline(series)
    assert baseline[0] == pytest.approx(5.0)
    assert baseline[1] == pytest.approx(5.0)  # Tuesday: fallback
    assert "Tue" in baseline["_fallback_weekdays"]


def test_deseasonalize_removes_weekly_pattern():
    series = [(f"2026-01-{d:02d}T12:00:00Z", 10.0 if d % 7 else 50.0)
              for d in range(1, 22)]  # three weeks
    residuals = deseasonalize(series)
    assert all(abs(r) < 1e-9 for r in residuals)


def _metrics_doc(dir_values):
    windows = []
    for i, value in enumerate(dir_values):
        windows.append({
            "window_start": f"2026-01-{i + 1:02d}T00:00:00Z",
            "system": "loan-v1",
            "groups": {"sex": {"disparate_impact_ratio": value}},
        })
    return {"windows": windows}


def test_detect_change_points_finds_regime_change():
    doc = _metrics_doc([0.70, 0.68, 0.71, 0.69, 0.70, 0.55, 0.10, 0.12, 0.11])
    result = detect_change_points(doc["windows"], "dir", group_attr="sex")
    assert result["n_usable"] == 9
    assert result["change_points"], "expected the DIR collapse to be flagged"
    first = result["change_points"][0]
    assert first["window_start"][:10] == "2026-01-06"
    assert first["direction"] == "down"
    assert first["previous_value"] == pytest.approx(0.70)
    assert first["value"] == pytest.approx(0.55)


def test_detect_change_points_flat_history_is_quiet():
    doc = _metrics_doc([0.70] * 12)
    with pytest.raises(ValueError, match="non-zero variance"):
        detect_change_points(doc["windows"], "dir", group_attr="sex")


def test_detect_change_points_needs_group_attr():
    doc = _metrics_doc([0.7] * 5)
    with pytest.raises(ValueError, match="group_attr"):
        detect_change_points(doc["windows"], "dir")


def test_detect_change_points_skips_none_windows():
    windows = _metrics_doc([0.70, 0.71, None, 0.69, 0.70, 0.705,
                            0.695])["windows"]
    result = detect_change_points(windows, "dir", group_attr="sex")
    assert result["n_usable"] == 6
    assert result["change_points"] == []


def test_drift_cli(tmp_path, capsys):
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps(_metrics_doc(
        [0.70, 0.68, 0.71, 0.69, 0.70, 0.55, 0.10, 0.12, 0.11])),
        encoding="utf-8")
    rc = main(["drift", "--metrics", str(metrics), "--metric", "dir",
               "--group-attr", "sex"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "change point(s)" in out
    assert "2026-01-0" in out


def test_drift_cli_to_file(tmp_path):
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps(_metrics_doc([0.7] * 5 + [0.1] * 5)),
                       encoding="utf-8")
    out = tmp_path / "drift.json"
    rc = main(["drift", "--metrics", str(metrics), "--metric", "dir",
               "--group-attr", "sex", "--out", str(out)])
    assert rc == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["change_points"]
