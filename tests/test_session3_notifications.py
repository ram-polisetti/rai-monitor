"""Tests for notification delivery (email/SMTP, Slack webhook, dry-run)."""

import json
from unittest import mock

import pytest

from raimonitor.cli import main
from raimonitor.notifications import (
    _redact_error,
    load_notify_config,
    notify_incidents,
    redacted_config,
)

INCIDENTS = [
    {"id": "inc-1", "rule": "dir-below-80", "system": "loan-v1",
     "window_start": "2026-09-08T00:00:00Z", "metric": "dir",
     "group_attr": "sex", "observed": 0.5, "op": "lt",
     "threshold": 0.8, "severity": "critical", "status": "open",
     "raised_at": "2026-09-09T00:00:00Z"},
    {"id": "inc-2", "rule": "vol-spike", "system": "loan-v1",
     "window_start": "2026-09-08T00:00:00Z", "metric": "volume",
     "group_attr": None, "observed": 9999, "op": "gt",
     "threshold": 5000, "severity": "warning", "status": "open",
     "raised_at": "2026-09-09T00:00:00Z"},
]

CONFIG = {
    "min_severity": "critical",
    "email": {"smtp_host": "smtp.example.org", "smtp_port": 587,
              "use_tls": True, "username": "mon@example.org",
              "password": "s3cret-pw", "from": "raimonitor@example.org",
              "to": ["oncall@example.org"]},
    "slack": {"webhook_url": "https://hooks.slack.com/services/T/B/s3cret"},
}


def test_load_notify_config_requires_a_channel(tmp_path):
    cfg = tmp_path / "notify.json"
    cfg.write_text(json.dumps({"min_severity": "critical"}), encoding="utf-8")
    with pytest.raises(ValueError, match="no channels"):
        load_notify_config(cfg)


def test_load_notify_config_validates_email(tmp_path):
    cfg = tmp_path / "notify.json"
    cfg.write_text(json.dumps({"email": {"smtp_host": "x"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field"):
        load_notify_config(cfg)


def test_redacted_config_hides_secrets():
    redacted = redacted_config(CONFIG)
    assert redacted["email"]["password"] == "***"
    assert redacted["slack"]["webhook_url"] == "***"
    assert redacted["email"]["smtp_host"] == "smtp.example.org"  # non-secret kept
    # original untouched
    assert CONFIG["email"]["password"] == "s3cret-pw"


def test_redact_error_scrubs_secrets_from_messages():
    msg = _redact_error("login failed for mon@example.org with s3cret-pw "
                        "posting to https://hooks.slack.com/services/T/B/s3cret",
                        CONFIG)
    assert "s3cret-pw" not in msg
    assert "services/T/B/s3cret" not in msg
    assert "***" in msg


def test_dry_run_sends_nothing_and_reports():
    with mock.patch("raimonitor.notifications._send_email") as send_email, \
         mock.patch("raimonitor.notifications._send_slack") as send_slack:
        deliveries = notify_incidents(INCIDENTS, CONFIG, dry_run=True)
    send_email.assert_not_called()
    send_slack.assert_not_called()
    by_inc = {}
    for d in deliveries:
        by_inc.setdefault(d["incident"], []).append(d)
    # critical incident -> dry-run on both channels
    assert {d["status"] for d in by_inc["inc-1"]} == {"dry-run"}
    assert {d["channel"] for d in by_inc["inc-1"]} == {"email", "slack"}
    # warning incident below min_severity -> skipped
    assert by_inc["inc-2"][0]["status"] == "skipped"


def test_email_sends_and_slack_posts():
    with mock.patch("smtplib.SMTP") as smtp_cls, \
         mock.patch("urllib.request.urlopen") as urlopen:
        server = smtp_cls.return_value
        response = mock.MagicMock()
        response.status = 200
        urlopen.return_value.__enter__.return_value = response
        deliveries = notify_incidents([INCIDENTS[0]], CONFIG)
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("mon@example.org", "s3cret-pw")
    assert server.send_message.call_count == 1
    sent_msg = server.send_message.call_args[0][0]
    assert "dir-below-80" in sent_msg["Subject"]
    assert "oncall@example.org" in sent_msg["To"]
    assert urlopen.call_count == 1
    request = urlopen.call_args[0][0]
    assert request.full_url == "https://hooks.slack.com/services/T/B/s3cret"
    payload = json.loads(request.data.decode())
    assert "dir-below-80" in payload["text"]
    assert all(d["status"] == "sent" for d in deliveries)


def test_channel_error_does_not_abort_batch():
    with mock.patch("smtplib.SMTP", side_effect=OSError("conn refused")), \
         mock.patch("urllib.request.urlopen") as urlopen:
        response = mock.MagicMock()
        response.status = 200
        urlopen.return_value.__enter__.return_value = response
        deliveries = notify_incidents([INCIDENTS[0]], CONFIG)
    statuses = {(d["channel"], d["status"]) for d in deliveries}
    assert ("email", "error") in statuses
    assert ("slack", "sent") in statuses
    err = next(d for d in deliveries if d["status"] == "error")
    assert "conn refused" in err["error"]


def _write_incidents_log(path):
    records = [dict(i) for i in INCIDENTS]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")


def test_notify_cli_dry_run_and_state(tmp_path):
    log = tmp_path / "incidents.jsonl"
    _write_incidents_log(log)
    cfg = tmp_path / "notify.json"
    cfg.write_text(json.dumps(CONFIG), encoding="utf-8")
    state = tmp_path / "notified.json"
    rc = main(["notify", "--incidents", str(log), "--config", str(cfg),
               "--state", str(state), "--dry-run"])
    assert rc == 0
    # dry-run must not record notified ids
    assert not state.exists()
    # real run with mocked transports records the critical incident only
    with mock.patch("smtplib.SMTP"), \
         mock.patch("urllib.request.urlopen") as urlopen:
        response = mock.MagicMock()
        response.status = 200
        urlopen.return_value.__enter__.return_value = response
        rc = main(["notify", "--incidents", str(log), "--config", str(cfg),
                   "--state", str(state)])
    assert rc == 0
    notified = json.loads(state.read_text(encoding="utf-8"))
    assert notified == ["inc-1"]
    # second run: nothing new to notify
    with mock.patch("smtplib.SMTP") as smtp_cls, \
         mock.patch("urllib.request.urlopen") as urlopen2:
        rc = main(["notify", "--incidents", str(log), "--config", str(cfg),
                   "--state", str(state)])
    assert rc == 0
    smtp_cls.assert_not_called()
    urlopen2.assert_not_called()
