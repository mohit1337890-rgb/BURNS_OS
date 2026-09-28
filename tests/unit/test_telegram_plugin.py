"""
Direct unit tests for gateway/plugins/telegram.py - closes a gap found
during the 2026-09-29 security-claims audit: the module's own docstring
claims TELEGRAM_BOT_TOKEN "never appears in a Ledger entry's detail text",
but TelegramPlugin itself had zero direct tests (only ever exercised
through a _FakePlugin substitute in gateway/core_execute.py's tests) - so
nothing actually proved the real implementation upholds that claim.
"""

from __future__ import annotations

from dataclasses import dataclass

from gateway.plugins.telegram import TelegramPlugin


@dataclass
class _FakeResponse:
    status_code: int
    text: str = ""


def test_fails_closed_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    result = TelegramPlugin().execute({"text": "hi"})
    assert result.ok is False
    assert "not configured" in result.detail.lower()


def test_success_detail_contains_no_token_and_no_message_content(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111111:AAAA-super-secret-token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")

    def fake_post(url, json=None, timeout=None):
        assert "111111:AAAA-super-secret-token" in url  # the real call DOES need it in the URL...
        return _FakeResponse(status_code=200)

    monkeypatch.setattr("gateway.plugins.telegram.requests.post", fake_post)
    result = TelegramPlugin().execute({"text": "the secret plan is codename firefly"})

    assert result.ok is True
    assert "111111:AAAA-super-secret-token" not in result.detail  # ...but never leaks into what gets logged
    assert "firefly" not in result.detail  # message content isn't echoed back either, just its length


def test_failure_detail_does_not_echo_telegram_response_body(monkeypatch):
    """Deliberately doesn't pass resp.text through verbatim - Telegram
    error bodies can, in some failure modes, include request params back."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111111:AAAA-super-secret-token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")

    def fake_post(url, json=None, timeout=None):
        return _FakeResponse(status_code=400, text="chat_id=123&text=the secret plan is codename firefly")

    monkeypatch.setattr("gateway.plugins.telegram.requests.post", fake_post)
    result = TelegramPlugin().execute({"text": "the secret plan is codename firefly"})

    assert result.ok is False
    assert "firefly" not in result.detail
    assert "400" in result.detail


def test_fails_closed_on_network_exception(monkeypatch):
    import requests

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111111:token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")

    def fake_post(url, json=None, timeout=None):
        raise requests.ConnectionError("network unreachable")

    monkeypatch.setattr("gateway.plugins.telegram.requests.post", fake_post)
    result = TelegramPlugin().execute({"text": "hi"})
    assert result.ok is False
