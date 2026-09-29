"""
Gateway HTTP server (BUILD PROMPT section 4.1 / STEP 3): a thin FastAPI
wrapper around gateway/core_execute.py's already-tested logic. Local
API-key auth (a single shared secret, GATEWAY_INTERNAL_TOKEN, checked via
the X-API-Key header) - this is meant to run bound to 127.0.0.1 or inside a
docker-compose internal network, never exposed to the public internet
directly (see docker-compose.yml, written but not yet run - Docker/WSL2
aren't installed on this dev machine, see docs/KNOWN_LIMITS.md).

The DB session factory and PolicyDocument are injected via
configure_session_factory()/configure_policy() rather than constructed at
import time, so tests can point this at an in-memory SQLite engine and a
policy built from test env vars instead of a real Postgres connection -
see tests/unit/test_gateway_app.py.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Generator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from core import app_config, approvals, ledger, ledger_anchor, policy_engine
from gateway import core_execute, registry_bootstrap

_session_factory: Optional[sessionmaker] = None
_policy: Optional[policy_engine.PolicyDocument] = None
_anchor_sinks: list = []


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Wires the app from real environment config when run for real
    (`uvicorn gateway.app:app`, or the Dockerfile's CMD) - NOT exercised by
    tests, which call configure_session_factory()/configure_policy()
    directly before making any request and so already have
    _session_factory/_policy set by the time this would run. Skipping when
    already configured means this hook can never silently overwrite a
    test's deliberate in-memory SQLite setup with a real Postgres
    connection attempt.
    """
    if _session_factory is None or _policy is None:
        core_config = app_config.load_core_config()  # raises ConfigError with every missing var if incomplete
        engine = ledger.get_engine(core_config.database_url)
        # Deliberately NOT calling ledger.init_db(engine)/create_all() here -
        # schema ownership belongs to Alembic (db/migrations/) against a real
        # Postgres DB. create_all() would race Alembic's own CREATE TABLE
        # statements (whichever runs first makes the other fail) and, worse,
        # would silently produce a ledger table with none of migration
        # 0002's append-only trigger/REVOKE protection. Tests bypass this
        # lifespan entirely (they call configure_session_factory() directly
        # against their own already-init_db()'d SQLite engine).
        configure_session_factory(ledger.get_session_factory(engine))

        policy = policy_engine.load_policy()
        configure_policy(policy)

        sinks = [ledger_anchor.FileAnchorSink(core_config.anchor_path)]
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            sinks.append(ledger_anchor.TelegramAnchorSink())
        configure_anchor_sinks(sinks)

        registry_bootstrap.bootstrap(policy, session_factory=_session_factory)
    yield


app = FastAPI(title="Burns Gateway", lifespan=_lifespan)


def configure_session_factory(factory: sessionmaker) -> None:
    global _session_factory
    _session_factory = factory


def configure_policy(policy: policy_engine.PolicyDocument) -> None:
    global _policy
    _policy = policy


def configure_anchor_sinks(sinks: list) -> None:
    global _anchor_sinks
    _anchor_sinks = sinks


def get_session() -> Generator[Session, None, None]:
    if _session_factory is None:
        raise RuntimeError("Session factory not configured - call configure_session_factory() first.")
    session = _session_factory()
    try:
        yield session
    finally:
        session.close()


def get_policy() -> policy_engine.PolicyDocument:
    if _policy is None:
        raise RuntimeError("Policy not configured - call configure_policy() first.")
    return _policy


def verify_api_key(x_api_key: str = Header(default="")) -> None:
    expected = os.environ.get("GATEWAY_INTERNAL_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="GATEWAY_INTERNAL_TOKEN not configured on the server.")
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


class ExecuteRequest(BaseModel):
    agent_role: str
    mission_id: Optional[str] = None
    action: str
    params: dict = {}
    input_summary: str


class ExecuteResponse(BaseModel):
    status: str
    detail: str
    ledger_entry_id: Optional[int] = None
    approval_id: Optional[str] = None


@app.post("/execute", response_model=ExecuteResponse, dependencies=[Depends(verify_api_key)])
def execute(req: ExecuteRequest, session: Session = Depends(get_session), policy: policy_engine.PolicyDocument = Depends(get_policy)):
    outcome = core_execute.request_action(
        session, policy, agent_role=req.agent_role, mission_id=req.mission_id,
        action=req.action, params=req.params, input_summary=req.input_summary,
    )
    return ExecuteResponse(
        status=outcome.status, detail=outcome.detail,
        ledger_entry_id=outcome.ledger_entry_id, approval_id=outcome.approval_id,
    )


class ApprovalStatusResponse(BaseModel):
    id: str
    status: str
    tier: int
    action: str
    agent_role: str
    mission_id: Optional[str]
    expires_at: str
    cooling_until: Optional[str]


@app.get("/approvals/{approval_id}", response_model=ApprovalStatusResponse, dependencies=[Depends(verify_api_key)])
def get_approval_status(approval_id: str, session: Session = Depends(get_session)):
    req = approvals.get_approval(session, approval_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"No approval with id {approval_id}.")
    return ApprovalStatusResponse(
        id=req.id, status=req.status, tier=req.tier, action=req.action, agent_role=req.agent_role,
        mission_id=req.mission_id, expires_at=req.expires_at.isoformat(),
        cooling_until=req.cooling_until.isoformat() if req.cooling_until else None,
    )


class LedgerVerifyResponse(BaseModel):
    chain_ok: bool
    chain_total_entries: int
    chain_first_broken_id: Optional[int]
    chain_reason: Optional[str]
    anchor_ok: bool
    anchor_checked: int
    anchor_reason: Optional[str]


@app.post("/ledger/verify", response_model=LedgerVerifyResponse, dependencies=[Depends(verify_api_key)])
def verify_ledger(session: Session = Depends(get_session)):
    chain_result = ledger.verify_chain(session)
    anchor_result = ledger_anchor.verify_against_anchors(session, _anchor_sinks)
    return LedgerVerifyResponse(
        chain_ok=chain_result.ok, chain_total_entries=chain_result.total_entries,
        chain_first_broken_id=chain_result.first_broken_id, chain_reason=chain_result.reason,
        anchor_ok=anchor_result.ok, anchor_checked=anchor_result.checked_anchors,
        anchor_reason=anchor_result.reason,
    )


@app.get("/health")
def health():
    # Deliberately no auth on /health - a liveness probe (docker-compose
    # healthcheck, Uptime Kuma) shouldn't need a secret, and it reveals
    # nothing sensitive (no ledger/approval content, just up/down + whether
    # config was loaded).
    return {
        "status": "ok",
        "session_factory_configured": _session_factory is not None,
        "policy_configured": _policy is not None,
    }
