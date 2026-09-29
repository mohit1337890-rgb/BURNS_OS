"""
Unit tests for gateway/mcp_auth.py - the bearer-token auth middleware for
the Gateway's MCP streamable-http transport (Milestone 2). Tests the
middleware in isolation against a trivial fake inner ASGI app, so these
don't need a real MCP protocol handshake to verify the security-critical
property: missing/wrong token -> 401, never reaching the inner app;
correct token -> the resolved agent_role is available via
get_current_agent_role() while the inner app runs, and NOT afterward.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gateway import mcp_auth

TOKENS = {"chief-token-abc": "chief_of_staff", "researcher-token-xyz": "researcher"}


def _inner_app():
    async def endpoint(request):
        return JSONResponse({"resolved_role": mcp_auth.get_current_agent_role()})

    return Starlette(routes=[Route("/mcp", endpoint)])


@pytest.fixture
def client():
    wrapped = mcp_auth.HermesBearerAuthMiddleware(_inner_app(), TOKENS)
    return TestClient(wrapped)


def test_missing_authorization_header_is_401(client):
    resp = client.get("/mcp")
    assert resp.status_code == 401


def test_wrong_bearer_token_is_401(client):
    resp = client.get("/mcp", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_non_bearer_authorization_scheme_is_401(client):
    resp = client.get("/mcp", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401


def test_chief_token_resolves_to_chief_of_staff_role(client):
    resp = client.get("/mcp", headers={"Authorization": "Bearer chief-token-abc"})
    assert resp.status_code == 200
    assert resp.json()["resolved_role"] == "chief_of_staff"


def test_researcher_token_resolves_to_researcher_role(client):
    resp = client.get("/mcp", headers={"Authorization": "Bearer researcher-token-xyz"})
    assert resp.status_code == 200
    assert resp.json()["resolved_role"] == "researcher"


def test_agent_a_token_cannot_resolve_to_agent_b_role(client):
    """The literal 'a token from agent A cannot act as agent B' acceptance
    test - there is no way to request a specific role; the token IS the
    only input, so a chief token can never resolve to 'researcher' and
    vice versa. Confirmed by the two tests above already, this one makes
    the negative claim explicit."""
    resp = client.get("/mcp", headers={"Authorization": "Bearer chief-token-abc"})
    assert resp.json()["resolved_role"] != "researcher"


def test_contextvar_is_reset_after_the_request(client):
    """The contextvar must not leak into whatever runs next in the same
    process - each request gets its own scoped value."""
    client.get("/mcp", headers={"Authorization": "Bearer chief-token-abc"})
    assert mcp_auth.get_current_agent_role() is None


def test_load_hermes_tokens_from_env_requires_both(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_TOKEN_CHIEF", raising=False)
    monkeypatch.delenv("HERMES_MCP_TOKEN_RESEARCHER", raising=False)
    with pytest.raises(RuntimeError, match="HERMES_MCP_TOKEN_CHIEF"):
        mcp_auth.load_hermes_tokens_from_env()


def test_load_hermes_tokens_from_env_refuses_identical_tokens(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_TOKEN_CHIEF", "same-value")
    monkeypatch.setenv("HERMES_MCP_TOKEN_RESEARCHER", "same-value")
    with pytest.raises(RuntimeError, match="must not be the same"):
        mcp_auth.load_hermes_tokens_from_env()


def test_load_hermes_tokens_from_env_maps_correctly(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_TOKEN_CHIEF", "chief-xyz")
    monkeypatch.setenv("HERMES_MCP_TOKEN_RESEARCHER", "researcher-abc")
    tokens = mcp_auth.load_hermes_tokens_from_env()
    assert tokens == {"chief-xyz": "chief_of_staff", "researcher-abc": "researcher"}
