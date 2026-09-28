"""
Approvals Bot (BUILD PROMPT section 4.4) - a Telegram bot SEPARATE from any
Hermes chat bot, whose only job is Approval Cards with Approve/Reject
buttons, restricted to TELEGRAM_OWNER_CHAT_ID.

TelegramClient is a Protocol so the decision-handling logic
(handle_callback_query) is testable with a fake client, with no real
network call and no real bot token needed - see
tests/unit/test_approvals_bot.py. RealTelegramClient is the actual
HTTP implementation, used only when this module is run for real (not yet -
see docs/KNOWN_LIMITS.md: TELEGRAM_BOT_TOKEN/TELEGRAM_OWNER_CHAT_ID are
still pending).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import requests
from sqlalchemy.orm import Session

from approvals_bot.formatting import (
    build_approval_card_text,
    build_approval_keyboard,
    build_outcome_text,
    parse_callback_data,
)
from core import approvals
from core.approvals import ApprovalRequest
from gateway import core_execute

_TELEGRAM_API_BASE = "https://api.telegram.org"
_TIMEOUT_SECONDS = 15


class TelegramClient(Protocol):
    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None: ...
    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None: ...
    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None: ...


class RealTelegramClient:
    """The actual HTTP implementation, using TELEGRAM_BOT_TOKEN from the
    environment - the same credential-isolation rule as
    gateway/plugins/telegram.py: this is the only module allowed to read
    it.
    """

    def __init__(self, token: str | None = None):
        self._token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")

    def _url(self, method: str) -> str:
        return f"{_TELEGRAM_API_BASE}/bot{self._token}/{method}"

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
        body = {"chat_id": chat_id, "text": text}
        if reply_markup:
            body["reply_markup"] = reply_markup
        requests.post(self._url("sendMessage"), json=body, timeout=_TIMEOUT_SECONDS)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        body = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        requests.post(self._url("answerCallbackQuery"), json=body, timeout=_TIMEOUT_SECONDS)

    def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        requests.post(
            self._url("editMessageText"),
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
            timeout=_TIMEOUT_SECONDS,
        )


@dataclass(frozen=True)
class HandledCallbackResult:
    ok: bool
    detail: str


def send_approval_card(client: TelegramClient, owner_chat_id: str, req: ApprovalRequest) -> None:
    client.send_message(
        owner_chat_id, build_approval_card_text(req), reply_markup=build_approval_keyboard(req),
    )


def handle_callback_query(
    session: Session,
    client: TelegramClient,
    *,
    callback_query_id: str,
    from_chat_id: str,
    owner_chat_id: str,
    message_id: int,
    original_text: str,
    callback_data: str,
) -> HandledCallbackResult:
    """The single entry point the real long-polling loop (not built yet)
    would call for every incoming callback_query update. Enforces
    owner-only BEFORE parsing/acting on the callback data at all - see
    design rule: "only TELEGRAM_OWNER_CHAT_ID can act".
    """
    if str(from_chat_id) != str(owner_chat_id):
        # Deliberately vague to whoever pressed the button - do not
        # confirm/deny that a valid approval even exists, and do not
        # execute anything.
        client.answer_callback_query(callback_query_id, text="Not authorized.")
        return HandledCallbackResult(ok=False, detail=f"Rejected: sender chat_id {from_chat_id} is not the owner.")

    try:
        decision, approval_id = parse_callback_data(callback_data)
    except Exception as exc:  # noqa: BLE001 - malformed input from Telegram, never a reason to crash the bot
        client.answer_callback_query(callback_query_id, text="Malformed request.")
        return HandledCallbackResult(ok=False, detail=str(exc))

    try:
        approvals.decide_approval(
            session, approval_id, decided_by="Mohit", owner_chat_id=owner_chat_id,
            approve=(decision == "approve"),
        )
    except approvals.ApprovalError as exc:
        client.answer_callback_query(callback_query_id, text=str(exc))
        return HandledCallbackResult(ok=False, detail=str(exc))

    if decision == "approve":
        outcome = core_execute.execute_approved_action(session, approval_id)
        result_detail = outcome.detail
    else:
        result_detail = "Action will not be executed."

    client.answer_callback_query(callback_query_id, text="Recorded.")
    client.edit_message_text(
        from_chat_id, message_id,
        build_outcome_text(original_text, decided_by_label="Mohit", decision=decision, result_detail=result_detail),
    )
    return HandledCallbackResult(ok=True, detail=result_detail)
