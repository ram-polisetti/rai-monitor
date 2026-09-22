"""raimonitor — Responsible AI monitoring dashboard.

Ingest production AI decision logs, compute rolling fairness and performance
metrics over time windows, detect distribution drift, raise persistent
threshold alerts into an incident log, render a static HTML dashboard, and
serve a live-updating dashboard over a tailed decision log.

Built by an operator, for operators.
"""

from .alerts import evaluate_rules
from .drift import detect_drift, psi
from .evidence import bootstrap_rate_ci, new_rng, window_rng
from .ingest import (
    from_decision_log,
    from_opsaudit_report,
    from_rag_audit_trail,
    write_events,
)
from .metrics import compute_metrics
from .server import LiveMonitor, LiveServer
from .windows import bucketize, parse_window

__version__ = "0.3.0"
__all__ = [
    "LiveMonitor",
    "LiveServer",
    "bootstrap_rate_ci",
    "bucketize",
    "compute_metrics",
    "detect_drift",
    "evaluate_rules",
    "from_decision_log",
    "from_opsaudit_report",
    "from_rag_audit_trail",
    "new_rng",
    "parse_window",
    "psi",
    "window_rng",
    "write_events",
]
