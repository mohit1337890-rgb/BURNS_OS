"""
The Approvals Bot's actual polling logic, split so it's testable without a
real Telegram connection:

- dispatch_update(): pure per-update logic (ignore anything that isn't a
  callback_query, otherwise hand it to bot.handle_callback_query).
- run_poll_loop(): the loop body, with fetch_updates injected as a
  callable so a test can supply a scripted, finite sequence of update
  batches (real usage: __main__.py wires this to a genuine Telegram
  getUpdates long-poll call, an infinite loop not exercised by tests -
  see docs/KNOWN_LIMITS.md).
"""

from __future__ import annotations

from typing import Callable, Optional

from sqlalchemy.orm import sessionmaker

from approvals_bot import bot as approvals_bot_module
from approvals_bot.bot import TelegramClient


def dispatch_update(session, client: TelegramClient, update: dict, owner_chat_id: str):
    """Returns None for any update that isn't a callback_query (e.g. a
    plain text message - the Approvals Bot only ever acts on button
    presses, per design; free-form chat is Hermes's job, a different bot
    entirely per BUILD PROMPT section 4.4's "separate from any Hermes chat
    bot")."""
    cb = update.get("callback_query")
    if cb is None:
        return None

    message = cb.get("message") or {}
    return approvals_bot_module.handle_callback_query(
        session, client,
        callback_query_id=cb.get("id", ""),
        from_chat_id=(cb.get("from") or {}).get("id", ""),
        owner_chat_id=owner_chat_id,
        message_id=message.get("message_id", 0),
        original_text=message.get("text", ""),
        callback_data=cb.get("data", ""),
    )


def run_poll_loop(
    session_factory: sessionmaker,
    client: TelegramClient,
    fetch_updates: Callable[[int], list[dict]],
    owner_chat_id: str,
    max_iterations: Optional[int] = None,
) -> int:
    """max_iterations=None is the real infinite loop (__main__.py); a test
    passes a small number and a fetch_updates that returns progressively
    fewer/empty batches, so the loop naturally completes within that bound
    rather than needing to be force-broken from outside. Returns the final
    `offset` reached (Telegram's own de-duplication convention: the next
    getUpdates call should ask for update_id >= offset, i.e. strictly
    after the highest update_id already processed).
    """
    offset = 0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        session = session_factory()
        try:
            updates = fetch_updates(offset)
            for update in updates:
                dispatch_update(session, client, update, owner_chat_id)
                offset = max(offset, update.get("update_id", -1) + 1)
        finally:
            session.close()
        iterations += 1
    return offset
