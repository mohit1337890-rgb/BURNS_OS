"""
Budget Guard (BUILD PROMPT section 4.8) - tracks spend against the global
monthly cap and per-mission caps, computed fresh from the Ledger's own
cost_usd column (never a separately-maintained running total that could
drift from what was actually logged) - see spend_this_month()/
spend_for_mission() below, both pure SUM queries over ledger rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.ledger import LedgerEntry


@dataclass(frozen=True)
class BudgetStatus:
    spend_usd: float
    cap_usd: float
    pct_used: float
    warn: bool
    stop: bool


def _month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spend_this_month(session: Session, now: datetime | None = None) -> float:
    total = (
        session.query(func.coalesce(func.sum(LedgerEntry.cost_usd), 0.0))
        .filter(LedgerEntry.ts >= _month_start(now))
        .scalar()
    )
    return float(total)


def spend_for_mission(session: Session, mission_id: str) -> float:
    total = (
        session.query(func.coalesce(func.sum(LedgerEntry.cost_usd), 0.0))
        .filter(LedgerEntry.mission_id == mission_id)
        .scalar()
    )
    return float(total)


def check_monthly_budget(
    session: Session, monthly_cap_usd: float, warn_at_pct: float, stop_at_pct: float,
    now: datetime | None = None,
) -> BudgetStatus:
    spend = spend_this_month(session, now)
    pct = (spend / monthly_cap_usd * 100.0) if monthly_cap_usd > 0 else 100.0
    return BudgetStatus(
        spend_usd=spend, cap_usd=monthly_cap_usd, pct_used=pct,
        warn=pct >= warn_at_pct, stop=pct >= stop_at_pct,
    )


def check_mission_budget(
    session: Session, mission_id: str, mission_cap_usd: float,
    warn_at_pct: float, stop_at_pct: float,
) -> BudgetStatus:
    spend = spend_for_mission(session, mission_id)
    pct = (spend / mission_cap_usd * 100.0) if mission_cap_usd > 0 else 100.0
    return BudgetStatus(
        spend_usd=spend, cap_usd=mission_cap_usd, pct_used=pct,
        warn=pct >= warn_at_pct, stop=pct >= stop_at_pct,
    )


class BudgetExceededError(Exception):
    def __init__(self, status: BudgetStatus, scope: str):
        self.status = status
        self.scope = scope
        super().__init__(
            f"{scope} budget exceeded: ${status.spend_usd:.2f} spent of ${status.cap_usd:.2f} cap "
            f"({status.pct_used:.0f}%)."
        )


def enforce(
    session: Session, mission_id: str | None, monthly_cap_usd: float, mission_cap_usd: float,
    warn_at_pct: float, stop_at_pct: float,
) -> list[BudgetStatus]:
    """Called by the Gateway before executing ANY tier's action (even Tier
    0/1, since read-only actions still cost LLM tokens). Raises
    BudgetExceededError if either the global monthly cap or this mission's
    own cap has been hit - returns the statuses (for warn-level logging)
    otherwise.
    """
    statuses = []
    monthly = check_monthly_budget(session, monthly_cap_usd, warn_at_pct, stop_at_pct)
    statuses.append(monthly)
    if monthly.stop:
        raise BudgetExceededError(monthly, scope="Global monthly")

    if mission_id:
        mission = check_mission_budget(session, mission_id, mission_cap_usd, warn_at_pct, stop_at_pct)
        statuses.append(mission)
        if mission.stop:
            raise BudgetExceededError(mission, scope=f"Mission '{mission_id}'")

    return statuses
