"""
TestClient-based tests for dashboard/app.py - the full HTTP surface
(setup, login, approvals decide, CSRF, sessions) exercised over real
FastAPI request/response handling, same pattern as
tests/unit/test_gateway_app.py.
"""

from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core import approvals, ledger, policy_engine
from dashboard import app as dashboard_app
from dashboard import auth as dashboard_auth
from gateway import plugins

REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")  # TestClient talks plain http
    yield


@pytest.fixture(autouse=True)
def configured_app(env):
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ledger.init_db(engine)
    factory = ledger.get_session_factory(engine)
    dashboard_app.configure_session_factory(factory)
    dashboard_app.configure_policy(policy_engine.load_policy())
    dashboard_app.configure_anchor_sinks([])
    dashboard_app._COOKIE_SECURE = False
    yield
    plugins._REGISTRY.clear()


@pytest.fixture
def client():
    return TestClient(dashboard_app.app)


@pytest.fixture
def db_session(configured_app):
    factory = dashboard_app._session_factory
    s = factory()
    yield s
    s.close()


def _bootstrap_and_login(client: TestClient, db_session, *, with_totp: bool = False) -> str | None:
    dashboard_auth.bootstrap_owner_account(db_session, username="mohit", password="hunter2-very-secret-pw")
    totp_secret = None
    if with_totp:
        owner = dashboard_auth.get_owner_account(db_session)
        totp_secret, _ = dashboard_auth.start_totp_enrollment(owner)
        dashboard_auth.confirm_totp_enrollment(db_session, owner, totp_secret, pyotp.TOTP(totp_secret).now())
    resp = client.post("/login", data={
        "username": "mohit", "password": "hunter2-very-secret-pw",
        "totp_code": pyotp.TOTP(totp_secret).now() if totp_secret else "",
    }, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return totp_secret


def _get_csrf(client: TestClient) -> str:
    # /command always renders csrf_token (even with zero pending approvals,
    # unlike /approvals whose token only appears inside the per-approval
    # loop) - the secret itself is per-SESSION, not per-page, so any page
    # that renders it works for any form on any page in that session.
    resp = client.get("/command")
    assert resp.status_code == 200
    # crude but effective extraction from the rendered form
    text = resp.text
    marker = 'name="csrf_token" value="'
    start = text.index(marker) + len(marker)
    return text[start:text.index('"', start)]


# --- setup --------------------------------------------------------------------------

def test_no_owner_redirects_to_setup(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/owner"


def test_setup_owner_creates_account_then_redirects_to_totp(client):
    resp = client.post("/setup/owner", data={"username": "mohit", "password": "hunter2-very-secret-pw", "confirm_password": "hunter2-very-secret-pw"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup/totp"


def test_setup_owner_refuses_mismatched_passwords(client):
    resp = client.post("/setup/owner", data={"username": "mohit", "password": "hunter2-very-secret-pw", "confirm_password": "different"})
    assert resp.status_code == 200
    assert "do not match" in resp.text.lower()


def test_setup_owner_refuses_a_second_time(client, db_session):
    dashboard_auth.bootstrap_owner_account(db_session, username="mohit", password="hunter2-very-secret-pw")
    resp = client.get("/setup/owner", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --- login (item E: unauthenticated / wrong password / wrong TOTP) --------------------

def test_unauthenticated_home_redirects_to_login(client, db_session):
    dashboard_auth.bootstrap_owner_account(db_session, username="mohit", password="hunter2-very-secret-pw")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_wrong_password_refused(client, db_session):
    dashboard_auth.bootstrap_owner_account(db_session, username="mohit", password="hunter2-very-secret-pw")
    resp = client.post("/login", data={"username": "mohit", "password": "wrong", "totp_code": ""})
    assert resp.status_code == 401


def test_login_wrong_totp_refused(client, db_session):
    _bootstrap_and_login  # noqa - not used, building manually below
    owner = dashboard_auth.bootstrap_owner_account(db_session, username="mohit", password="hunter2-very-secret-pw")
    secret, _ = dashboard_auth.start_totp_enrollment(owner)
    dashboard_auth.confirm_totp_enrollment(db_session, owner, secret, pyotp.TOTP(secret).now())
    resp = client.post("/login", data={"username": "mohit", "password": "hunter2-very-secret-pw", "totp_code": "000000"})
    assert resp.status_code == 401


def test_login_success_sets_cookie_and_reaches_home(client, db_session):
    _bootstrap_and_login(client, db_session)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Pending approvals" in resp.text


def test_expired_session_redirects_to_login(client, db_session, monkeypatch):
    _bootstrap_and_login(client, db_session)
    # Force every session lookup to see a far-future "now" past the idle timeout.
    import datetime as dt
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=dashboard_auth.SESSION_IDLE_TIMEOUT_MINUTES + 5)

    real_get_valid_session = dashboard_auth.get_valid_session
    monkeypatch.setattr(dashboard_app.dashboard_auth, "get_valid_session", lambda s, t, now=None: real_get_valid_session(s, t, now=future))
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --- CSRF (item E) ----------------------------------------------------------------

def test_missing_csrf_token_refuses_approval_decision(client, db_session):
    """An empty csrf_token is rejected by FastAPI's own Form(...) validation
    (422, "Field required" - FastAPI treats an empty form value as absent
    for a required field) before core.approvals ever runs - still a real
    rejection, just via a different layer than a forged-but-present token
    (see the next test, which gets this module's own 403)."""
    _bootstrap_and_login(client, db_session)
    policy = dashboard_app.get_policy()
    from gateway import core_execute
    outcome = core_execute.request_action(db_session, policy, agent_role="chief-of-staff", mission_id=None, action="send_message", params={"text": "hi"}, input_summary="test")
    resp = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "approve"})  # csrf_token omitted entirely
    assert resp.status_code == 422
    updated = approvals.get_approval(db_session, outcome.approval_id)
    assert updated.status == approvals.ApprovalStatus.PENDING.value


def test_forged_csrf_token_refuses_approval_decision(client, db_session):
    _bootstrap_and_login(client, db_session)
    policy = dashboard_app.get_policy()
    from gateway import core_execute
    outcome = core_execute.request_action(db_session, policy, agent_role="chief-of-staff", mission_id=None, action="send_message", params={"text": "hi"}, input_summary="test")
    resp = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "approve", "csrf_token": "totally-forged-value"})
    assert resp.status_code == 403
    updated = approvals.get_approval(db_session, outcome.approval_id)
    assert updated.status == approvals.ApprovalStatus.PENDING.value


# --- approvals: Tier-2 reject/approve (acceptance test B) --------------------------

class _FakePlugin:
    def __init__(self):
        self.calls = []

    def execute(self, params):
        self.calls.append(params)
        return plugins.PluginResult(ok=True, detail="fake sent", cost_usd=0.0)


def test_tier2_reject_via_dashboard_does_not_execute(client, db_session):
    _bootstrap_and_login(client, db_session)
    fake = _FakePlugin()
    plugins.register("send_message", fake)
    policy = dashboard_app.get_policy()
    from gateway import core_execute
    outcome = core_execute.request_action(db_session, policy, agent_role="chief-of-staff", mission_id=None, action="send_message", params={"text": "hi"}, input_summary="test")

    csrf = _get_csrf(client)
    resp = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "reject", "csrf_token": csrf})
    assert resp.status_code == 200
    assert fake.calls == []
    updated = approvals.get_approval(db_session, outcome.approval_id)
    assert updated.status == approvals.ApprovalStatus.REJECTED.value


def test_tier2_approve_via_dashboard_executes_and_is_verifiable(client, db_session):
    _bootstrap_and_login(client, db_session)
    fake = _FakePlugin()
    plugins.register("send_message", fake)
    policy = dashboard_app.get_policy()
    from gateway import core_execute
    outcome = core_execute.request_action(db_session, policy, agent_role="chief-of-staff", mission_id=None, action="send_message", params={"text": "hi"}, input_summary="test")

    csrf = _get_csrf(client)
    resp = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "approve", "csrf_token": csrf})
    assert resp.status_code == 200
    assert fake.calls == [{"text": "hi"}]
    assert ledger.verify_chain(db_session).ok


# --- Tier-3 requires fresh TOTP (acceptance test C) --------------------------------

def test_tier3_approve_without_totp_refused(client, db_session):
    _bootstrap_and_login(client, db_session, with_totp=True)
    plugins.register("place_trade", _FakePlugin())
    policy = dashboard_app.get_policy()
    from gateway import core_execute
    outcome = core_execute.request_action(db_session, policy, agent_role="risk-manager", mission_id=None, action="place_trade", params={"symbol": "EURUSD"}, input_summary="test")

    csrf = _get_csrf(client)
    resp = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "approve", "csrf_token": csrf, "totp_code": ""})
    assert "Refused" in resp.text
    updated = approvals.get_approval(db_session, outcome.approval_id)
    assert updated.status == approvals.ApprovalStatus.PENDING.value


def test_tier3_approve_with_fresh_totp_succeeds(client, db_session):
    totp_secret = _bootstrap_and_login(client, db_session, with_totp=True)
    plugins.register("place_trade", _FakePlugin())
    policy = dashboard_app.get_policy()
    from gateway import core_execute
    outcome = core_execute.request_action(db_session, policy, agent_role="risk-manager", mission_id=None, action="place_trade", params={"symbol": "EURUSD"}, input_summary="test")

    csrf = _get_csrf(client)
    resp = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "approve", "csrf_token": csrf, "totp_code": pyotp.TOTP(totp_secret).now()})
    assert resp.status_code == 200
    updated = approvals.get_approval(db_session, outcome.approval_id)
    assert updated.status == approvals.ApprovalStatus.APPROVED.value  # cooling period still pending, not yet EXECUTED


# --- double-approve (acceptance test F) --------------------------------------------

def test_double_approve_via_dashboard_executes_once(client, db_session):
    _bootstrap_and_login(client, db_session)
    fake = _FakePlugin()
    plugins.register("send_message", fake)
    policy = dashboard_app.get_policy()
    from gateway import core_execute
    outcome = core_execute.request_action(db_session, policy, agent_role="chief-of-staff", mission_id=None, action="send_message", params={"text": "hi"}, input_summary="test")

    csrf = _get_csrf(client)
    r1 = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "approve", "csrf_token": csrf})
    r2 = client.post(f"/approvals/{outcome.approval_id}/decide", data={"decision": "approve", "csrf_token": csrf})
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(fake.calls) == 1


# --- command box --------------------------------------------------------------------

def test_command_box_logs_but_does_not_execute(client, db_session):
    _bootstrap_and_login(client, db_session)
    csrf = _get_csrf(client)
    resp = client.post("/command", data={"text": "deploy the app to production", "csrf_token": csrf})
    assert resp.status_code == 200
    assert "not executed" in resp.text.lower() or "Logged" in resp.text
    from dashboard.models import CommandRequest
    rows = db_session.query(CommandRequest).all()
    assert len(rows) == 1
    assert rows[0].text == "deploy the app to production"
