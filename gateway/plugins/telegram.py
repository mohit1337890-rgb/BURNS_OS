"""
Real Telegram send_message plugin (BUILD PROMPT section 4.1: "ship working
implementations for Telegram message"). This is the ONLY module in Burns OS
that reads TELEGRAM_BOT_TOKEN - it never appears in a Ledger entry's detail
text (see execute() below, which logs the message length, not its content
or the token).
"""

from __future__ import annotations

import os

import requests

from gateway.plugins import PluginResult

_TELEGRAM_API_BASE = "https://api.telegram.org"
_TIMEOUT_SECONDS = 15


class TelegramPlugin:
    def execute(self, params: dict) -> PluginResult:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = params.get("chat_id") or os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")
        text = params.get("text", "")

        if not token or not chat_id:
            # Fails closed, not silently - matches core/ledger.py's own
            # "never claim something works unless it actually ran" ethos.
            # See docs/KNOWN_LIMITS.md: these are pending owner-provided
            # values.
            return PluginResult(
                ok=False,
                detail="TELEGRAM_BOT_TOKEN and/or TELEGRAM_OWNER_CHAT_ID not configured - message NOT sent.",
            )
        if not text:
            return PluginResult(ok=False, detail="No text provided - message NOT sent.")

        url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
        try:
            resp = requests.post(
                url, json={"chat_id": chat_id, "text": text}, timeout=_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            return PluginResult(ok=False, detail=f"Telegram request failed: {exc}")

        if resp.status_code != 200:
            # Deliberately don't echo resp.text verbatim - Telegram error
            # bodies can, in some failure modes, include request params
            # back; keep this to the status code only.
            return PluginResult(ok=False, detail=f"Telegram API returned HTTP {resp.status_code}.")

        return PluginResult(ok=True, detail=f"Message sent ({len(text)} chars).")
