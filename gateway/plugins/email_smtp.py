"""Real send_email plugin over SMTP (stdlib smtplib - no extra dependency)."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from gateway.plugins import PluginResult

_TIMEOUT_SECONDS = 20


class SmtpEmailPlugin:
    def execute(self, params: dict) -> PluginResult:
        host = os.environ.get("SMTP_HOST", "")
        port_raw = os.environ.get("SMTP_PORT", "")
        user = os.environ.get("SMTP_USER", "")
        password = os.environ.get("SMTP_PASSWORD", "")

        to_addr = params.get("to", "")
        subject = params.get("subject", "")
        body = params.get("body", "")

        if not (host and port_raw and user and password):
            return PluginResult(ok=False, detail="SMTP not configured (SMTP_HOST/PORT/USER/PASSWORD) - email NOT sent.")
        if not to_addr:
            return PluginResult(ok=False, detail="No 'to' address provided - email NOT sent.")

        try:
            port = int(port_raw)
        except ValueError:
            return PluginResult(ok=False, detail=f"SMTP_PORT '{port_raw}' is not a valid integer.")

        msg = EmailMessage()
        msg["From"] = user
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            with smtplib.SMTP(host, port, timeout=_TIMEOUT_SECONDS) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            # Found during the 2026-09-29 security-claims audit: some SMTP
            # server error responses (e.g. a malformed/hostile server's
            # AUTH failure text) can echo the client's own request data
            # back verbatim - str(exc) is not a safe place to assume
            # SMTP_PASSWORD can never appear. Explicit redaction, not just
            # "this shouldn't happen" - this detail string is what lands
            # in the Ledger's result field (design rule 10: secrets never
            # appear in logs).
            detail = f"SMTP send failed: {exc}"
            if password and password in detail:
                detail = detail.replace(password, "[REDACTED]")
            return PluginResult(ok=False, detail=detail)

        return PluginResult(ok=True, detail=f"Email sent to {to_addr} (subject: {subject[:60]!r}).")
