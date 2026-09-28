"""
Ledger - append-only, hash-chained record of every action the Gateway takes.

Design (BUILD PROMPT section 4.3): each row's hash covers its own fields PLUS
the previous row's hash, so altering or deleting any historical row breaks
the chain from that point forward - verify_chain() below detects this. The
SQLAlchemy model deliberately has no update()/delete() path anywhere in this
module - the ONLY write operation exposed is append_entry(). The production
Postgres role Burns OS connects as should additionally be granted INSERT+
SELECT only (no UPDATE/DELETE) - see db/migrations/ - as defense in depth
beyond what this Python layer alone can guarantee.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

GENESIS_HASH = "0" * 64


class Base(DeclarativeBase):
    pass


class LedgerEntry(Base):
    __tablename__ = "ledger"
    # Each hash extends the chain at exactly one point - a DB-enforced
    # UNIQUE on prev_hash (not just app-level convention) is what makes
    # append_entry()'s read-latest-then-insert safe under genuinely
    # concurrent writers (found live, 2026-09-29: two threads both reading
    # the same "latest" row before either committed produced two rows with
    # the SAME prev_hash - a forked chain verify_chain() correctly flagged,
    # but only after the fact). See append_entry()'s retry loop.
    __table_args__ = (UniqueConstraint("prev_hash", name="uq_ledger_prev_hash"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    mission_id = Column(String, nullable=True)
    agent_role = Column(String, nullable=False)
    action = Column(String, nullable=False)
    tier = Column(Integer, nullable=False)
    input_summary = Column(String, nullable=False)
    tool = Column(String, nullable=False)
    approved_by = Column(String, nullable=True)
    approval_id = Column(String, nullable=True)
    result = Column(String, nullable=False)
    cost_usd = Column(Float, nullable=False, default=0.0)
    evidence_links = Column(JSON, nullable=False, default=list)
    prev_hash = Column(String(64), nullable=False)
    hash = Column(String(64), nullable=False)


@dataclass(frozen=True)
class LedgerEntryInput:
    mission_id: Optional[str]
    agent_role: str
    action: str
    tier: int
    input_summary: str
    tool: str
    result: str
    approved_by: Optional[str] = None
    approval_id: Optional[str] = None
    cost_usd: float = 0.0
    evidence_links: Optional[list[str]] = None


def _normalize_ts(ts: datetime) -> str:
    """Canonical, DB-round-trip-safe string form of a timestamp.

    SQLite has no native datetime type - SQLAlchemy stores it as text and,
    critically, drops tzinfo on the way back out, so a timezone-AWARE
    datetime used at write time comes back timezone-NAIVE after a
    session.refresh()/re-query. Without this normalization, .isoformat()
    would produce a different string (missing the "+00:00" suffix) at
    verify time than it did at write time, and verify_chain() would report
    every single row as tampered even when nothing changed - caught by
    test_verify_chain_ok_on_untampered_ledger. Treat a naive datetime as
    already-UTC (true for every datetime this module ever produces, since
    append_entry always uses datetime.now(timezone.utc)) and always emit
    the same UTC-aware ISO string either way.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.isoformat()


def _canonical_payload(ts: datetime, entry: LedgerEntryInput, prev_hash: str) -> str:
    """Deterministic JSON serialization used for hashing - key order and
    float/str formatting must never vary between the write path and any
    later re-verification, or verify_chain() would report false breaks.
    """
    payload = {
        "ts": _normalize_ts(ts),
        "mission_id": entry.mission_id,
        "agent_role": entry.agent_role,
        "action": entry.action,
        "tier": entry.tier,
        "input_summary": entry.input_summary,
        "tool": entry.tool,
        "approved_by": entry.approved_by,
        "approval_id": entry.approval_id,
        "result": entry.result,
        "cost_usd": entry.cost_usd,
        "evidence_links": entry.evidence_links or [],
        "prev_hash": prev_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_hash(ts: datetime, entry: LedgerEntryInput, prev_hash: str) -> str:
    return hashlib.sha256(_canonical_payload(ts, entry, prev_hash).encode("utf-8")).hexdigest()


def get_engine(db_url: str):
    return create_engine(db_url, future=True)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def get_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, future=True)


def _latest_hash(session: Session) -> str:
    latest = session.query(LedgerEntry).order_by(LedgerEntry.id.desc()).first()
    return latest.hash if latest else GENESIS_HASH


def append_entry(session: Session, entry: LedgerEntryInput, ts: Optional[datetime] = None, _max_attempts: int = 8) -> LedgerEntry:
    """The ONLY write path onto the ledger. Always appends after the
    current latest row's hash - there is no function anywhere in this
    module to edit or remove an existing row.

    Retries on a prev_hash collision (LedgerEntry.uq_ledger_prev_hash) -
    two genuinely concurrent callers can both read the same "latest" hash
    before either commits; the DB-level UNIQUE constraint is what actually
    catches that (found live, 2026-09-29, via a real two-thread Postgres
    test - see core/ledger.py's LedgerEntry.__table_args__ comment), and
    this loop is what turns "one of them fails" into "both succeed, in
    some order" - append_entry() must never itself lose a caller's write.
    """
    ts = ts or datetime.now(timezone.utc)
    for attempt in range(_max_attempts):
        prev_hash = _latest_hash(session)
        entry_hash = compute_hash(ts, entry, prev_hash)

        row = LedgerEntry(
            ts=ts,
            mission_id=entry.mission_id,
            agent_role=entry.agent_role,
            action=entry.action,
            tier=entry.tier,
            input_summary=entry.input_summary,
            tool=entry.tool,
            approved_by=entry.approved_by,
            approval_id=entry.approval_id,
            result=entry.result,
            cost_usd=entry.cost_usd,
            evidence_links=entry.evidence_links or [],
            prev_hash=prev_hash,
            hash=entry_hash,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            continue
        session.refresh(row)
        return row
    raise RuntimeError(f"append_entry: lost the prev_hash race {_max_attempts} times in a row - unexpectedly high contention.")


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    total_entries: int
    first_broken_id: Optional[int]
    reason: Optional[str]


def verify_chain(session: Session) -> ChainVerification:
    """Recomputes every row's hash from its own stored fields and compares
    it against both the stored hash AND the next row's stored prev_hash -
    this is what `burns ledger verify` (scripts/ledger_verify.py) calls.
    """
    rows = session.query(LedgerEntry).order_by(LedgerEntry.id.asc()).all()
    expected_prev = GENESIS_HASH
    for row in rows:
        if row.prev_hash != expected_prev:
            return ChainVerification(
                ok=False, total_entries=len(rows), first_broken_id=row.id,
                reason=f"Row {row.id}'s prev_hash does not match the previous row's actual hash - chain broken.",
            )
        recomputed = compute_hash(
            row.ts,
            LedgerEntryInput(
                mission_id=row.mission_id, agent_role=row.agent_role, action=row.action,
                tier=row.tier, input_summary=row.input_summary, tool=row.tool,
                result=row.result, approved_by=row.approved_by, approval_id=row.approval_id,
                cost_usd=row.cost_usd, evidence_links=row.evidence_links,
            ),
            row.prev_hash,
        )
        if recomputed != row.hash:
            return ChainVerification(
                ok=False, total_entries=len(rows), first_broken_id=row.id,
                reason=f"Row {row.id}'s stored hash does not match its own recomputed content hash - row was altered.",
            )
        expected_prev = row.hash
    return ChainVerification(ok=True, total_entries=len(rows), first_broken_id=None, reason=None)
