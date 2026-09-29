"""
Direct unit tests for core/approval_channels.py - the channel-agnostic
owner-authorization interface introduced 2026-09-29 when Telegram was
postponed in favor of the Web Dashboard as the primary approval channel.
"""

from __future__ import annotations

import pyotp
import pytest

from core import approvals, ledger
from core.approval_channels import DashboardChannel, TelegramChannel
from dashboard import auth as dashboard_auth

REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
}


@pytest.fixture
def approval(session):
    return approvals.create_approval(
        session, mission_id=None, agent_role="chief-of-staff", action="send_message", tier=2,
        params_summary="channel test", params={"text": "hi"}, expire_after_hours=24, cooling_minutes=0,
    )


@pytest.fixture
def tier3_approval(session):
    return approvals.create_approval(
        session, mission_id=None, agent_role="risk-manager", action="place_trade", tier=3,
        params_summary="channel test tier3", params={"symbol": "EURUSD"}, expire_after_hours=24, cooling_minutes=10,
    )


# --- TelegramChannel -------------------------------------------------------

def test_telegram_channel_authorizes_a_matching_chat_id(session, approval, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    result = TelegramChannel().authorize_decision(session, approval, chat_id="123")
    assert result.ok is True


def test_telegram_channel_refuses_a_mismatched_chat_id(session, approval, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    result = TelegramChannel().authorize_decision(session, approval, chat_id="999")
    assert result.ok is False
    assert "does not match" in result.reason.lower()


def test_telegram_channel_refuses_when_no_chat_id_configured(session, approval, monkeypatch):
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    result = TelegramChannel().authorize_decision(session, approval, chat_id="123")
    assert result.ok is False


def test_telegram_channel_refuses_when_no_chat_id_given(session, approval, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    result = TelegramChannel().authorize_decision(session, approval)
    assert result.ok is False


# --- decide_approval() integration - proves it truly calls the channel, not a stub --

def test_decide_approval_via_telegram_channel_end_to_end(session, approval, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    updated = approvals.decide_approval(
        session, approval.id, decided_by="Mohit", channel=TelegramChannel(), approve=True, chat_id="123",
    )
    assert updated.status == approvals.ApprovalStatus.APPROVED.value


def test_decide_approval_refuses_unauthorized_telegram_chat_id(session, approval, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "123")
    with pytest.raises(approvals.NotOwnerError):
        approvals.decide_approval(
            session, approval.id, decided_by="intruder", channel=TelegramChannel(), approve=True, chat_id="999",
        )
    unchanged = approvals.get_approval(session, approval.id)
    assert unchanged.status == approvals.ApprovalStatus.PENDING.value


# --- DashboardChannel -------------------------------------------------------------

@pytest.fixture
def dashboard_session_token(session):
    raw_token, _ = dashboard_auth.create_session(session)
    return raw_token


def test_dashboard_channel_refuses_with_no_session_token(session, approval):
    result = DashboardChannel().authorize_decision(session, approval)
    assert result.ok is False
    assert "no dashboard session" in result.reason.lower()


def test_dashboard_channel_refuses_a_bogus_session_token(session, approval):
    result = DashboardChannel().authorize_decision(session, approval, session_token="not-a-real-token")
    assert result.ok is False


def test_dashboard_channel_authorizes_tier2_with_just_a_valid_session(session, approval, dashboard_session_token):
    result = DashboardChannel().authorize_decision(session, approval, session_token=dashboard_session_token)
    assert result.ok is True


def test_dashboard_channel_refuses_tier3_without_totp(session, tier3_approval, dashboard_session_token):
    result = DashboardChannel().authorize_decision(session, tier3_approval, session_token=dashboard_session_token)
    assert result.ok is False
    assert "totp" in result.reason.lower()


def test_dashboard_channel_refuses_tier3_with_wrong_totp(session, tier3_approval, dashboard_session_token):
    owner = dashboard_auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    secret, _ = dashboard_auth.start_totp_enrollment(owner)
    dashboard_auth.confirm_totp_enrollment(session, owner, secret, pyotp.TOTP(secret).now())

    result = DashboardChannel().authorize_decision(session, tier3_approval, session_token=dashboard_session_token, totp_code="000000")
    assert result.ok is False


def test_dashboard_channel_authorizes_tier3_with_a_fresh_totp_code(session, tier3_approval, dashboard_session_token):
    owner = dashboard_auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    secret, _ = dashboard_auth.start_totp_enrollment(owner)
    dashboard_auth.confirm_totp_enrollment(session, owner, secret, pyotp.TOTP(secret).now())

    result = DashboardChannel().authorize_decision(session, tier3_approval, session_token=dashboard_session_token, totp_code=pyotp.TOTP(secret).now())
    assert result.ok is True


def test_decide_approval_via_dashboard_channel_tier2_end_to_end(session, approval, dashboard_session_token):
    updated = approvals.decide_approval(
        session, approval.id, decided_by="Mohit (dashboard)", channel=DashboardChannel(),
        approve=True, session_token=dashboard_session_token,
    )
    assert updated.status == approvals.ApprovalStatus.APPROVED.value


def test_decide_approval_via_dashboard_channel_tier3_requires_totp(session, tier3_approval, dashboard_session_token):
    with pytest.raises(approvals.NotOwnerError):
        approvals.decide_approval(
            session, tier3_approval.id, decided_by="Mohit (dashboard)", channel=DashboardChannel(),
            approve=True, session_token=dashboard_session_token,
        )
    unchanged = approvals.get_approval(session, tier3_approval.id)
    assert unchanged.status == approvals.ApprovalStatus.PENDING.value
