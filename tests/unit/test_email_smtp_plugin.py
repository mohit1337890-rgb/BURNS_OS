"""
Direct unit tests for gateway/plugins/email_smtp.py - closes a gap found
during the 2026-09-29 security-claims audit (same class of gap as
gateway/plugins/telegram.py): SmtpEmailPlugin was only ever exercised via
a fake substitute elsewhere, never directly - nothing actually proved
SMTP_PASSWORD never leaks into a PluginResult.detail.
"""

from __future__ import annotations

from gateway.plugins.email_smtp import SmtpEmailPlugin

REAL_ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "bot@example.com",
    "SMTP_PASSWORD": "hunter2-super-secret",
}


class _FakeSmtpServer:
    sent_messages: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        _FakeSmtpServer.sent_messages.append(msg)


def _set_env(monkeypatch):
    for k, v in REAL_ENV.items():
        monkeypatch.setenv(k, v)


def test_fails_closed_when_smtp_env_incomplete(monkeypatch):
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    result = SmtpEmailPlugin().execute({"to": "client@example.com", "subject": "x", "body": "y"})
    assert result.ok is False
    assert "not configured" in result.detail.lower()


def test_success_detail_never_contains_the_smtp_password(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.setattr("gateway.plugins.email_smtp.smtplib.SMTP", _FakeSmtpServer)
    result = SmtpEmailPlugin().execute({"to": "client@example.com", "subject": "Notes", "body": "hi there"})
    assert result.ok is True
    assert "hunter2-super-secret" not in result.detail


def test_failure_detail_never_contains_the_smtp_password_either(monkeypatch):
    _set_env(monkeypatch)

    class _FailingSmtp(_FakeSmtpServer):
        def login(self, user, password):
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, f"auth failed for password {password}".encode())

    monkeypatch.setattr("gateway.plugins.email_smtp.smtplib.SMTP", _FailingSmtp)
    result = SmtpEmailPlugin().execute({"to": "client@example.com", "subject": "x", "body": "y"})
    assert result.ok is False
    # Real bug found here live (2026-09-29): some SMTP server error
    # responses echo the client's own request data back verbatim, and the
    # original code passed str(exc) straight into detail unfiltered - a
    # genuine password leak into the Ledger. Fixed with explicit redaction
    # in gateway/plugins/email_smtp.py.
    assert "hunter2-super-secret" not in result.detail
    assert "REDACTED" in result.detail
