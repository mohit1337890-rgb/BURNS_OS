"""
Ledger tail-truncation detection (closes KNOWN_LIMITS gap #1).

verify_chain() (core/ledger.py) proves the ledger is internally consistent -
no row was altered, no row was deleted from the MIDDLE (that breaks the
prev_hash link to the next row). It cannot catch someone deleting the LAST
N rows: the chain still looks perfectly valid, it just "ends early" - there
is nothing inside the database alone to compare the current tail against.

The fix: periodically write an ANCHOR - {last_id, last_hash, timestamp} for
whatever the current latest ledger row is - to a sink OUTSIDE the database
(a file today; Telegram once a bot token exists, behind the same
AnchorSink interface). verify_against_anchors() then checks that every
anchored (id, hash) pair still exists in the CURRENT ledger with the SAME
hash - if the anchored row is missing (its id no longer exists) or its hash
doesn't match, the tail was truncated (or otherwise tampered) after that
anchor was written.

The periodic (hourly) CALLING of write_anchor() is a real running process -
out of scope for this module, same as core/scheduler.py's Tier-3 execution
loop - this module is the pure, directly-testable logic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from core.ledger import LedgerEntry

DEFAULT_ANCHOR_PATH_ENV = "LEDGER_ANCHOR_PATH"


@dataclass(frozen=True)
class AnchorRecord:
    last_id: int
    last_hash: str
    timestamp: str


class AnchorSink(Protocol):
    def write(self, record: AnchorRecord) -> None: ...
    def read_all(self) -> list[AnchorRecord]: ...


class FileAnchorSink:
    """Appends one JSON-line record per anchor write to a file outside the
    database - deliberately append-only itself (opened with mode "a"), so
    the anchor history has the same "can't quietly rewrite the past"
    property the ledger itself has, just via the filesystem instead of a
    hash chain (a second, independent mechanism - the point is that an
    attacker would need to tamper with BOTH the DB and this file
    consistently, not just one).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def write(self, record: AnchorRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"last_id": record.last_id, "last_hash": record.last_hash, "timestamp": record.timestamp}) + "\n")

    def read_all(self) -> list[AnchorRecord]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                records.append(AnchorRecord(last_id=data["last_id"], last_hash=data["last_hash"], timestamp=data["timestamp"]))
        return records


class TelegramAnchorSink:
    """Second sink behind the same AnchorSink interface - sends each anchor
    to the owner's Telegram chat as a durable, human-visible record. Not
    exercised end-to-end (no real bot token yet - see docs/KNOWN_LIMITS.md);
    reuses gateway.plugins.telegram's plain HTTP call rather than adding a
    second Telegram client implementation.
    """

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self._token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")

    def write(self, record: AnchorRecord) -> None:
        if not self._token or not self._chat_id:
            return  # fails silently by design here - FileAnchorSink is the sink of record; this is best-effort
        import requests
        text = f"\U0001F4CC Ledger anchor: id={record.last_id} hash={record.last_hash[:12]}... at {record.timestamp}"
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text}, timeout=15,
            )
        except Exception:  # noqa: BLE001 - an anchor-notification failure must never break the anchor write itself
            pass

    def read_all(self) -> list[AnchorRecord]:
        # Telegram is not used as a read-back source of truth for
        # verification (that would require parsing chat history via the
        # Bot API, which has its own reliability caveats) - FileAnchorSink
        # is authoritative for verify_against_anchors(). Telegram is a
        # human-visible notification channel, a second independent copy an
        # attacker would also have to suppress, not a queryable database.
        return []


def write_anchor(session: Session, sinks: list[AnchorSink], now: datetime | None = None) -> AnchorRecord | None:
    """Writes the current latest ledger row's (id, hash) to every sink.
    Returns None (writes nothing) if the ledger is empty - there is nothing
    to anchor yet.
    """
    now = now or datetime.now(timezone.utc)
    latest = session.query(LedgerEntry).order_by(LedgerEntry.id.desc()).first()
    if latest is None:
        return None
    record = AnchorRecord(last_id=latest.id, last_hash=latest.hash, timestamp=now.isoformat())
    for sink in sinks:
        sink.write(record)
    return record


@dataclass(frozen=True)
class AnchorVerification:
    ok: bool
    checked_anchors: int
    failed_anchor: AnchorRecord | None
    reason: str | None


def verify_against_anchors(session: Session, sinks: list[AnchorSink]) -> AnchorVerification:
    """For every anchor ever written (across all given sinks' read_all()),
    confirms a ledger row with that id still exists and its hash still
    matches. The FIRST mismatch (in anchor-chronological order) is
    reported - that's the earliest point after which truncation/tampering
    could have occurred.
    """
    all_records: list[AnchorRecord] = []
    for sink in sinks:
        all_records.extend(sink.read_all())
    all_records.sort(key=lambda r: r.timestamp)

    checked = 0
    for record in all_records:
        checked += 1
        row = session.query(LedgerEntry).filter_by(id=record.last_id).first()
        if row is None:
            return AnchorVerification(
                ok=False, checked_anchors=checked, failed_anchor=record,
                reason=f"Anchored row id={record.last_id} no longer exists in the ledger - tail truncation detected.",
            )
        if row.hash != record.last_hash:
            return AnchorVerification(
                ok=False, checked_anchors=checked, failed_anchor=record,
                reason=f"Anchored row id={record.last_id} exists but its hash no longer matches the anchor - tampering detected.",
            )
    return AnchorVerification(ok=True, checked_anchors=checked, failed_anchor=None, reason=None)
