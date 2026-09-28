"""
Real entry point: `python -m approvals_bot`.

Requires TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_CHAT_ID, and the same
POSTGRES_*/LEDGER_ANCHOR_PATH core vars core/app_config.py::load_core_config
requires (fails loudly at startup, listing everything missing, same as the
Gateway).

NOT live-tested (no real Telegram token on this dev machine - see
docs/KNOWN_LIMITS.md). approvals_bot/poller.py's dispatch/loop LOGIC is
real and unit-tested with a fake client + a scripted fetch_updates; only
the actual Telegram getUpdates HTTP call and the "run forever" outer loop
below are unexercised.
"""

from __future__ import annotations

import sys
import time

import requests

from approvals_bot import poller
from approvals_bot.bot import RealTelegramClient
from core import app_config, ledger

_LONG_POLL_TIMEOUT_SECONDS = 30
_ERROR_BACKOFF_SECONDS = 5


def _fetch_updates(token: str, offset: int) -> list[dict]:
    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"offset": offset, "timeout": _LONG_POLL_TIMEOUT_SECONDS},
        timeout=_LONG_POLL_TIMEOUT_SECONDS + 10,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def main() -> int:
    try:
        config = app_config.load_core_config()
    except app_config.ConfigError as exc:
        print(f"[APPROVALS_BOT] Refusing to start: {exc}", file=sys.stderr)
        return 1

    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("[APPROVALS_BOT] Refusing to start: TELEGRAM_BOT_TOKEN not set.", file=sys.stderr)
        return 1

    engine = ledger.get_engine(config.database_url)
    session_factory = ledger.get_session_factory(engine)
    client = RealTelegramClient(token)

    print(f"[APPROVALS_BOT] Starting long-poll loop for owner chat {config.owner_chat_id}...")
    offset = 0
    while True:
        try:
            offset = poller.run_poll_loop(
                session_factory, client, lambda o: _fetch_updates(token, o),
                config.owner_chat_id, max_iterations=1,
            )
        except requests.RequestException as exc:
            print(f"[APPROVALS_BOT] Poll failed ({exc}); retrying in {_ERROR_BACKOFF_SECONDS}s.", file=sys.stderr)
            time.sleep(_ERROR_BACKOFF_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
