"""Threshold alerting with persistence, feeding the incident log.

A rule fires only when its condition holds for ``persistence`` consecutive
windows — a single noisy window does not page anyone. Supported metrics:

- ``dir`` — disparate impact ratio for a group attribute (needs ``group_attr``)
- ``tpr_gap`` / ``fpr_gap`` — error-rate gaps for a group attribute
- ``decision_rate`` — overall positive-decision rate
- ``accuracy`` — overall accuracy (windows with outcomes only)
- ``volume`` — event count ``n``

Rule shape::

    {"name": "dir-below-80",
     "metric": "dir", "group_attr": "sex",
     "op": "lt", "threshold": 0.8,
     "persistence": 2, "severity": "critical"}

Incidents are deterministic: the incident id is a hash of
(rule name, system, window start), so re-running the pipeline never
duplicates an incident for the same window.
"""

from __future__ import annotations

import hashlib

OPS = {
    "lt": lambda obs, thr: obs < thr,
    "le": lambda obs, thr: obs <= thr,
    "gt": lambda obs, thr: obs > thr,
    "ge": lambda obs, thr: obs >= thr,
}

METRICS = ("dir", "tpr_gap", "fpr_gap", "decision_rate", "accuracy", "volume")


def _observe(window_metrics: dict, rule: dict) -> float | None:
    metric = rule["metric"]
    if metric == "dir":
        attr = rule.get("group_attr")
        group = (window_metrics.get("groups") or {}).get(attr) or {}
        return group.get("disparate_impact_ratio")
    if metric in ("tpr_gap", "fpr_gap"):
        attr = rule.get("group_attr")
        return (window_metrics.get(metric) or {}).get(attr)
    if metric == "decision_rate":
        return window_metrics.get("decision_rate")
    if metric == "accuracy":
        return window_metrics.get("accuracy")
    if metric == "volume":
        return window_metrics.get("n")
    return None


def validate_rule(rule: dict) -> dict:
    """Check a rule dict and fill defaults (``persistence``=1)."""
    if not isinstance(rule, dict):
        raise ValueError("rule must be a dict")
    for field in ("name", "metric", "op", "threshold"):
        if field not in rule:
            raise ValueError(f"rule missing required field: {field!r}")
    if rule["metric"] not in METRICS:
        raise ValueError(f"unknown metric {rule['metric']!r}; choose from {METRICS}")
    if rule["metric"] in ("dir", "tpr_gap", "fpr_gap") and not rule.get("group_attr"):
        raise ValueError(f"metric {rule['metric']!r} requires 'group_attr'")
    if rule["op"] not in OPS:
        raise ValueError(f"unknown op {rule['op']!r}; choose from {sorted(OPS)}")
    persistence = int(rule.get("persistence", 1))
    if persistence < 1:
        raise ValueError("'persistence' must be >= 1")
    severity = rule.get("severity", "warning")
    if severity not in ("warning", "critical"):
        raise ValueError(f"unknown severity {severity!r}")
    return {**rule, "persistence": persistence, "severity": severity}


def _incident_id(rule_name: str, system: str, window_start: str) -> str:
    digest = hashlib.sha256(
        f"{rule_name}|{system}|{window_start}".encode()
    ).hexdigest()
    return f"inc-{digest[:12]}"


def evaluate_rules(metrics_doc: dict, rules: list[dict]) -> list[dict]:
    """Evaluate rules over ordered metric windows; return new incidents.

    Windows for each system are processed chronologically. A rule with
    ``persistence: N`` fires on the Nth consecutive window where the
    condition holds, and keeps firing on subsequent consecutive windows
    (one incident per window) until the condition clears.
    """
    rules = [validate_rule(r) for r in rules]
    windows = metrics_doc.get("windows", [])
    by_system: dict[str, list[dict]] = {}
    for window in windows:
        by_system.setdefault(window["system"], []).append(window)
    for system_windows in by_system.values():
        system_windows.sort(key=lambda w: w["window_start"])

    incidents: list[dict] = []
    for system, system_windows in sorted(by_system.items()):
        for rule in rules:
            streak = 0
            op = OPS[rule["op"]]
            for window in system_windows:
                observed = _observe(window, rule)
                if observed is not None and op(observed, rule["threshold"]):
                    streak += 1
                else:
                    streak = 0
                    continue
                if streak >= rule["persistence"]:
                    start = window["window_start"]
                    incidents.append({
                        "id": _incident_id(rule["name"], system, start),
                        "rule": rule["name"],
                        "system": system,
                        "window_start": start,
                        "metric": rule["metric"],
                        "group_attr": rule.get("group_attr"),
                        "observed": observed,
                        "op": rule["op"],
                        "threshold": rule["threshold"],
                        "severity": rule["severity"],
                        "status": "open",
                    })
    return incidents
