"""Notification delivery for critical incidents.

Incidents are only labels until someone reads them. This module delivers
them through two stdlib-only channels:

- **email** — SMTP via :mod:`smtplib` / :mod:`email.message`
- **slack** — incoming webhook via :mod:`urllib.request` POST

Notifications are **off by default**: nothing is sent unless a notify
config file enables a channel. A config looks like::

    {
      "min_severity": "critical",
      "email": {
        "smtp_host": "smtp.example.org", "smtp_port": 587, "use_tls": true,
        "username": "monitor@example.org", "password": "SECRET",
        "from": "raimonitor@example.org",
        "to": ["oncall@example.org"]
      },
      "slack": {"webhook_url": "https://hooks.slack.com/services/AAA/BBB/CCC"}
    }

Only the channels present in the config are used. Severity filtering uses
the ordering ``warning < critical``: with ``min_severity: "critical"`` only
critical incidents are delivered.

**Credential hygiene.** Secrets (SMTP password, webhook URL) are never
written to logs or to delivery records. :func:`redacted_config` returns a
log-safe copy of the config, and every error path redacts secrets before
the message leaves this module. Prefer storing the config file with
``0600`` permissions and loading secrets from a vault or environment
rather than committing them.

**Dry-run.** :func:`notify_incidents(..., dry_run=True)` (or
``raimonitor notify --dry-run``) builds the exact messages and reports
what *would* be sent, without touching the network.
"""

from __future__ import annotations

import json
import smtplib
import urllib.request
from email.message import EmailMessage
from pathlib import Path

_SEVERITY_ORDER = {"warning": 0, "critical": 1}
_SECRET_KEYS = {"password", "webhook_url"}


def load_notify_config(path: str | Path) -> dict:
    """Load and validate a notify config file."""
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("notify config must be a JSON object")
    min_severity = config.get("min_severity", "critical")
    if min_severity not in _SEVERITY_ORDER:
        raise ValueError(f"unknown min_severity {min_severity!r}")
    email = config.get("email")
    if email is not None:
        for field in ("smtp_host", "from", "to"):
            if not email.get(field):
                raise ValueError(f"email config missing required field: {field!r}")
        if not isinstance(email["to"], list) or not email["to"]:
            raise ValueError("email 'to' must be a non-empty list")
    slack = config.get("slack")
    if slack is not None and not slack.get("webhook_url"):
        raise ValueError("slack config missing 'webhook_url'")
    if email is None and slack is None:
        raise ValueError("notify config enables no channels "
                         "(need 'email' and/or 'slack')")
    return config


def redacted_config(config: dict) -> dict:
    """Return a log-safe copy of the config with secrets redacted."""
    redacted = json.loads(json.dumps(config))
    for channel in ("email", "slack"):
        section = redacted.get(channel)
        if isinstance(section, dict):
            for key in _SECRET_KEYS:
                if key in section:
                    section[key] = "***"
    return redacted


def _redact_error(message: str, config: dict) -> str:
    """Scrub secret values out of an error message before it propagates."""
    for channel in ("email", "slack"):
        section = config.get(channel) or {}
        for key in _SECRET_KEYS:
            secret = section.get(key)
            if secret:
                message = message.replace(str(secret), "***")
    return message


def _wanted(incident: dict, min_severity: str) -> bool:
    return _SEVERITY_ORDER.get(incident.get("severity", "warning"), 0) >= \
        _SEVERITY_ORDER[min_severity]


def _subject(incident: dict) -> str:
    return (f"[raimonitor:{incident.get('severity', 'warning')}] "
            f"{incident.get('rule')} on {incident.get('system')} "
            f"(window {incident.get('window_start', '')[:10]})")


def _body(incident: dict) -> str:
    lines = [
        _subject(incident),
        "",
        f"incident id : {incident.get('id')}",
        f"metric      : {incident.get('metric')}"
        + (f" ({incident.get('group_attr')})" if incident.get("group_attr") else ""),
        f"observed    : {incident.get('observed')} "
        f"(threshold {incident.get('op')} {incident.get('threshold')})",
        f"raised at   : {incident.get('raised_at', 'n/a')}",
    ]
    return "\n".join(lines) + "\n"


def _send_email(incident: dict, email_cfg: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = _subject(incident)
    msg["From"] = email_cfg["from"]
    msg["To"] = ", ".join(email_cfg["to"])
    msg.set_content(_body(incident))
    host = email_cfg["smtp_host"]
    port = int(email_cfg.get("smtp_port", 587))
    if email_cfg.get("use_tls", True):
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        if email_cfg.get("username"):
            server.login(email_cfg["username"], email_cfg.get("password", ""))
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _send_slack(incident: dict, slack_cfg: dict) -> None:
    payload = json.dumps({"text": _body(incident)}).encode("utf-8")
    request = urllib.request.Request(
        slack_cfg["webhook_url"], data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in (200, 201, 204):
            raise RuntimeError(f"slack webhook returned HTTP {response.status}")


def notify_incidents(incidents: list[dict], config: dict,
                     dry_run: bool = False,
                     already_notified: frozenset = frozenset()) -> list[dict]:
    """Deliver incidents through the configured channels.

    Returns one delivery record per (incident, channel) pair::

        {"channel": "email", "incident": "<id>", "status": "sent",
         "to": "oncall@example.org"}            # or "dry-run" / "error"

    ``status`` is ``"skipped"`` for incidents below ``min_severity`` and
    ``"already-notified"`` for (channel, incident) pairs in
    ``already_notified`` (per-channel dedupe state, so a failed channel
    is retried on the next run instead of being marked done by a sibling
    channel's success). Secrets never appear in the returned records.
    Network errors are captured per incident (``status: "error"``)
    rather than aborting the batch — one unreachable channel must not
    swallow the rest.
    """
    min_severity = config.get("min_severity", "critical")
    channels = []
    if config.get("email"):
        channels.append("email")
    if config.get("slack"):
        channels.append("slack")
    deliveries = []
    for incident in incidents:
        if not _wanted(incident, min_severity):
            deliveries.append({"channel": None, "incident": incident.get("id"),
                               "status": "skipped",
                               "reason": f"severity below {min_severity}"})
            continue
        for channel in channels:
            record = {"channel": channel, "incident": incident.get("id")}
            if (channel, incident.get("id")) in already_notified:
                record["status"] = "already-notified"
            elif dry_run:
                record["status"] = "dry-run"
                record["subject"] = _subject(incident)
            else:
                try:
                    if channel == "email":
                        _send_email(incident, config["email"])
                        record["to"] = list(config["email"]["to"])
                    else:
                        _send_slack(incident, config["slack"])
                    record["status"] = "sent"
                except Exception as exc:
                    record["status"] = "error"
                    record["error"] = _redact_error(str(exc), config)
            deliveries.append(record)
    return deliveries
