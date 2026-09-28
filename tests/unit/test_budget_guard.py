"""
Direct unit tests for core/budget_guard.py - closes a gap found during the
2026-09-29 security-claims audit: budget enforcement was previously only
exercised indirectly via tests/unit/test_core_execute.py's single
"budget exceeded refuses" scenario (a global-monthly-cap case only) - the
module's own distinct claims (mission cap vs monthly cap, warn vs stop
thresholds, spend computed fresh from the ledger rather than a separate
running total) had no dedicated test of their own.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core import budget_guard, ledger


def _spend(session, *, mission_id, cost_usd, ts=None):
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=mission_id, agent_role="x", action="web_search", tier=0,
            input_summary="spend", tool="web_search", result="EXECUTED_IN_SANDBOX", cost_usd=cost_usd,
        ),
        ts=ts,
    )


def test_spend_this_month_ignores_entries_from_a_previous_month(session):
    last_month = datetime.now(timezone.utc).replace(day=1) - timedelta(days=1)
    _spend(session, mission_id=None, cost_usd=100.0, ts=last_month)
    _spend(session, mission_id=None, cost_usd=5.0)
    assert budget_guard.spend_this_month(session) == 5.0


def test_spend_for_mission_only_counts_that_mission(session):
    _spend(session, mission_id="m1", cost_usd=3.0)
    _spend(session, mission_id="m2", cost_usd=100.0)
    assert budget_guard.spend_for_mission(session, "m1") == 3.0


def test_check_monthly_budget_warn_and_stop_thresholds(session):
    _spend(session, mission_id=None, cost_usd=85.0)
    status = budget_guard.check_monthly_budget(session, monthly_cap_usd=100.0, warn_at_pct=80, stop_at_pct=100)
    assert status.warn is True
    assert status.stop is False


def test_enforce_raises_on_global_monthly_stop_even_with_no_mission(session):
    _spend(session, mission_id=None, cost_usd=999.0)
    with pytest.raises(budget_guard.BudgetExceededError, match="Global monthly"):
        budget_guard.enforce(session, None, monthly_cap_usd=100.0, mission_cap_usd=10.0, warn_at_pct=80, stop_at_pct=100)


def test_enforce_raises_on_mission_stop_even_when_monthly_is_fine(session):
    """A mission can exhaust ITS OWN cap while the global monthly cap still
    has plenty of room - enforce() must catch this distinctly, not just
    the global case."""
    _spend(session, mission_id="m1", cost_usd=50.0)
    with pytest.raises(budget_guard.BudgetExceededError, match="Mission 'm1'"):
        budget_guard.enforce(session, "m1", monthly_cap_usd=10_000.0, mission_cap_usd=10.0, warn_at_pct=80, stop_at_pct=100)


def test_enforce_skips_mission_check_when_mission_id_is_none(session):
    """Tier 0/1 sandbox actions with no mission context must not spuriously
    fail a mission-budget check that doesn't apply to them."""
    statuses = budget_guard.enforce(session, None, monthly_cap_usd=100.0, mission_cap_usd=1.0, warn_at_pct=80, stop_at_pct=100)
    assert len(statuses) == 1  # only the monthly status, no mission status


def test_enforce_allows_spend_under_both_caps(session):
    _spend(session, mission_id="m1", cost_usd=1.0)
    statuses = budget_guard.enforce(session, "m1", monthly_cap_usd=100.0, mission_cap_usd=10.0, warn_at_pct=80, stop_at_pct=100)
    assert all(not s.stop for s in statuses)
