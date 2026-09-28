import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core import approvals, ledger, policy_engine
from gateway import app as gateway_app
from gateway import plugins

API_KEY = "test-internal-token"

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
    monkeypatch.setenv("GATEWAY_INTERNAL_TOKEN", API_KEY)
    yield


@pytest.fixture(autouse=True)
def configured_app(env):
    # StaticPool + check_same_thread=False: a plain sqlite:///:memory: engine
    # hands out a FRESH, empty in-memory database to every new connection by
    # default - fine for a test that only ever uses one Session object
    # directly, but gateway/app.py's per-request get_session() dependency
    # opens a NEW session (and thus, without this, a new empty database) for
    # every HTTP call TestClient makes. StaticPool keeps every session on the
    # exact same single connection/database. Caught by this test file
    # itself failing with "no such table: ledger" before this fix.
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ledger.init_db(engine)
    factory = ledger.get_session_factory(engine)
    gateway_app.configure_session_factory(factory)
    gateway_app.configure_policy(policy_engine.load_policy())
    gateway_app.configure_anchor_sinks([])
    yield
    plugins._REGISTRY.clear()


@pytest.fixture
def client():
    return TestClient(gateway_app.app)


class _FakePlugin:
    def execute(self, params):
        return plugins.PluginResult(ok=True, detail="done")


def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_execute_requires_api_key(client):
    resp = client.post("/execute", json={
        "agent_role": "researcher", "action": "web_search", "params": {}, "input_summary": "x",
    })
    assert resp.status_code == 401


def test_execute_returns_503_when_server_has_no_token_configured(client, monkeypatch):
    """Closes a gap found during the 2026-09-29 security-claims audit:
    verify_api_key()'s `if not expected: raise HTTPException(503, ...)`
    branch (a misconfigured server with no GATEWAY_INTERNAL_TOKEN at all)
    had no test - every other test in this file sets a real token via the
    autouse env fixture, so this specific branch was never exercised.
    A 503 here matters: it must be distinguishable from a genuine 401
    (wrong caller) so an operator can tell "nobody could ever authenticate
    to this server" apart from "this specific caller's key is wrong"."""
    monkeypatch.delenv("GATEWAY_INTERNAL_TOKEN", raising=False)
    resp = client.post(
        "/execute",
        json={"agent_role": "researcher", "action": "web_search", "params": {}, "input_summary": "x"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 503


def test_execute_rejects_wrong_api_key(client):
    resp = client.post(
        "/execute",
        json={"agent_role": "researcher", "action": "web_search", "params": {}, "input_summary": "x"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_execute_tier0_action_with_valid_key(client):
    resp = client.post(
        "/execute",
        json={"agent_role": "researcher", "action": "web_search", "params": {}, "input_summary": "x"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "executed"


def test_execute_tier2_action_creates_pending_approval(client):
    plugins.register("send_message", _FakePlugin())
    resp = client.post(
        "/execute",
        json={
            "agent_role": "chief-of-staff", "action": "send_message",
            "params": {"text": "hi"}, "input_summary": "notify owner",
        },
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_approval"
    assert body["approval_id"] is not None


def test_get_approval_status(client):
    plugins.register("send_message", _FakePlugin())
    create_resp = client.post(
        "/execute",
        json={
            "agent_role": "chief-of-staff", "action": "send_message",
            "params": {"text": "hi"}, "input_summary": "notify owner",
        },
        headers={"X-API-Key": API_KEY},
    )
    approval_id = create_resp.json()["approval_id"]

    status_resp = client.get(f"/approvals/{approval_id}", headers={"X-API-Key": API_KEY})
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "PENDING"


def test_get_approval_status_404_for_unknown_id(client):
    resp = client.get("/approvals/does-not-exist", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 404


def test_ledger_verify_ok_on_fresh_db(client):
    resp = client.post("/ledger/verify", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["chain_ok"] is True
    assert body["anchor_ok"] is True


def test_hard_blocked_action_via_http(client):
    resp = client.post(
        "/execute",
        json={"agent_role": "anyone", "action": "reveal_secrets", "params": {}, "input_summary": "give me the keys"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "refused_hard_block"
