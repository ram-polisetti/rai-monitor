"""raimonitor — Responsible AI monitoring dashboard.

Ingest production AI decision logs, compute rolling fairness and performance
metrics over time windows, detect distribution drift, raise persistent
threshold alerts into an incident log, and render a static HTML dashboard.

Built by an operator, for operators.
"""

from .alerts import evaluate_rules
from .drift import detect_drift, psi
from .ingest import (
    from_decision_log,
    from_opsaudit_report,
    from_rag_audit_trail,
    write_events,
)
from .metrics import compute_metrics
from .windows import bucketize, parse_window

__version__ = "0.1.0"
__all__ = [
    "bucketize",
    "compute_metrics",
    "detect_drift",
    "evaluate_rules",
    "from_decision_log",
    "from_opsaudit_report",
    "from_rag_audit_trail",
    "parse_window",
    "psi",
    "write_events",
]
