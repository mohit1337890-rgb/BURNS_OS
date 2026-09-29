"""
Web Dashboard - the primary approval/control channel (Telegram postponed,
see core/approval_channels.py). FastAPI + server-rendered HTMX fragments,
no SPA build step. Security posture (all explicitly required, 2026-09-29):

- Binds 127.0.0.1 ONLY by default (docker-compose.yml) - phone access is
  via Tailscale (docs/DASHBOARD.md), never a public port.
- Single owner account: argon2 password + TOTP 2FA, enrolled via QR code.
- Tier-3 approvals require a FRESH TOTP code at decision time, not just a
  valid session (core/approval_channels.py::DashboardChannel).
- CSRF token (session-bound synchronizer pattern) required on every POST.
- Session cookie: HttpOnly, SameSite=Strict, Secure by default
  (DASHBOARD_COOKIE_SECURE=false only for plain-http-on-127.0.0.1 local
  testing - see .env's comment on that var).
- 30-minute idle timeout (dashboard.auth.SESSION_IDLE_TIMEOUT_MINUTES),
  login rate-limit + lockout (dashboard.auth.MAX_FAILED_LOGINS).
- Every login (success/fail), approval decision, and command-box entry is
  logged to the same hash-chained Ledger everything else uses.

Session factory / policy are injected via configure_*() (same pattern as
gateway/app.py) so tests can point this at an in-memory SQLite engine.
"""

from __future__ import annotations

import base64
import io
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Generator, Optional

import pyotp
import qrcode
from fastapi import Cookie, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from core import app_config, approvals, budget_guard, dlp, ledger, ledger_anchor, missions, policy_engine
from core.approval_channels import DashboardChannel
from dashboard import auth as dashboard_auth
from dashboard.models import CommandRequest, OwnerAccount

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_session_factory: Optional[sessionmaker] = None
_policy: Optional[policy_engine.PolicyDocument] = None
_anchor_sinks: list = []
_dashboard_channel = DashboardChannel()

SESSION_COOKIE_NAME = "burns_dashboard_session"
_COOKIE_SECURE = os.environ.get("DASHBOARD_COOKIE_SECURE", "true").lower() != "false"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if _session_factory is None or _policy is None:
        core_config = app_config.load_core_config()
        engine = ledger.get_engine(core_config.database_url)
        configure_session_factory(ledger.get_session_factory(engine))
        configure_policy(policy_engine.load_policy())
        sinks = [ledger_anchor.FileAnchorSink(core_config.anchor_path)]
        configure_anchor_sinks(sinks)
    yield


app = FastAPI(title="Burns OS Dashboard", lifespan=_lifespan)


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


def _set_session_cookie(response, raw_token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME, raw_token, httponly=True, samesite="strict",
        secure=_COOKIE_SECURE, max_age=dashboard_auth.SESSION_IDLE_TIMEOUT_MINUTES * 60,
    )


def _require_session(session: Session, raw_token: str | None):
    """Returns the DashboardSession row, or None if not authenticated -
    callers decide whether that means a redirect (page routes) or a 401
    (POST/API routes)."""
    if not raw_token:
        return None
    return dashboard_auth.get_valid_session(session, raw_token)


def _require_csrf(dash_session, submitted_token: str | None) -> bool:
    return dashboard_auth.verify_csrf(dash_session, submitted_token)


# --- setup (owner bootstrap + TOTP enrollment) -----------------------------------

@app.get("/setup/owner", response_class=HTMLResponse)
def setup_owner_get(request: Request, session: Session = Depends(get_session)):
    if dashboard_auth.get_owner_account(session) is not None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_owner.html", {"error": None})


@app.post("/setup/owner", response_class=HTMLResponse)
def setup_owner_post(request: Request, username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...), session: Session = Depends(get_session)):
    if dashboard_auth.get_owner_account(session) is not None:
        return RedirectResponse("/login", status_code=303)
    if password != confirm_password:
        return templates.TemplateResponse(request, "setup_owner.html", {"error": "Passwords do not match."})
    if len(password) < 12:
        return templates.TemplateResponse(request, "setup_owner.html", {"error": "Password must be at least 12 characters."})
    dashboard_auth.bootstrap_owner_account(session, username=username, password=password)
    return RedirectResponse("/setup/totp", status_code=303)


def _qr_png_b64(username: str, secret: str) -> str:
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=dashboard_auth.TOTP_ISSUER)
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@app.get("/setup/totp", response_class=HTMLResponse)
def setup_totp_get(request: Request, session: Session = Depends(get_session)):
    owner = dashboard_auth.get_owner_account(session)
    if owner is None:
        return RedirectResponse("/setup/owner", status_code=303)
    if owner.totp_secret:
        return RedirectResponse("/login", status_code=303)
    secret, qr_png = dashboard_auth.start_totp_enrollment(owner)
    return templates.TemplateResponse(request, "setup_totp.html", {"secret": secret, "qr_b64": base64.b64encode(qr_png).decode("ascii"), "error": None})


@app.post("/setup/totp", response_class=HTMLResponse)
def setup_totp_post(request: Request, secret: str = Form(...), code: str = Form(...), session: Session = Depends(get_session)):
    owner = dashboard_auth.get_owner_account(session)
    if owner is None:
        return RedirectResponse("/setup/owner", status_code=303)
    ok = dashboard_auth.confirm_totp_enrollment(session, owner, secret, code)
    if not ok:
        return templates.TemplateResponse(request, "setup_totp.html", {
            "secret": secret, "qr_b64": _qr_png_b64(owner.username, secret),
            "error": "Invalid code - try the current code from your authenticator app.",
        })
    return RedirectResponse("/login", status_code=303)


# --- login / logout ---------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, session: Session = Depends(get_session)):
    if dashboard_auth.get_owner_account(session) is None:
        return RedirectResponse("/setup/owner", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request, username: str = Form(...), password: str = Form(...), totp_code: str = Form(""),
    session: Session = Depends(get_session),
):
    result = dashboard_auth.attempt_login(session, username=username, password=password, totp_code=totp_code or None)
    if not result.ok:
        return templates.TemplateResponse(request, "login.html", {"error": result.reason}, status_code=401)
    raw_token, _ = dashboard_auth.create_session(session, ip_address=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"))
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, raw_token)
    return response


@app.post("/logout")
def logout_post(response: RedirectResponse = None, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    if session_token:
        dashboard_auth.invalidate_session(session, session_token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# --- home ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session), policy: policy_engine.PolicyDocument = Depends(get_policy)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)

    pending = session.query(approvals.ApprovalRequest).filter_by(status=approvals.ApprovalStatus.PENDING.value).order_by(approvals.ApprovalRequest.requested_at.desc()).all()
    monthly = budget_guard.check_monthly_budget(session, policy.monthly_budget_usd, policy.budget_warn_at_pct, policy.budget_stop_at_pct)
    chain_result = ledger.verify_chain(session)
    anchor_result = ledger_anchor.verify_against_anchors(session, _anchor_sinks)
    alerts = _recent_alerts(session, limit=5)

    return templates.TemplateResponse(request, "home.html", {
        "pending_count": len(pending), "pending": pending[:5],
        "monthly": monthly, "chain_ok": chain_result.ok, "anchor_ok": anchor_result.ok,
        "alerts": alerts, "csrf_token": dash_session.csrf_secret,
    })


_ALERT_PATTERNS = ("%REFUSED%", "%UNKNOWN_OUTCOME%", "%RECONCILED_FAILED%", "%dlp%")


def _recent_alerts(session: Session, *, limit: int = 50) -> list[ledger.LedgerEntry]:
    """Alerts page's source of truth: hard-block refusals, DLP refusals,
    UNKNOWN_OUTCOME reconciliations, budget stops, and anchor/chain
    failures are all just specific `result` patterns already in the
    Ledger - no separate alerts table to keep in sync."""
    filters = [ledger.LedgerEntry.result.ilike(p) for p in _ALERT_PATTERNS]
    combined = filters[0]
    for f in filters[1:]:
        combined = combined | f
    return (
        session.query(ledger.LedgerEntry)
        .filter(combined)
        .order_by(ledger.LedgerEntry.id.desc())
        .limit(limit)
        .all()
    )


# --- approvals ------------------------------------------------------------------------

@app.get("/approvals", response_class=HTMLResponse)
def approvals_list(request: Request, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    pending = session.query(approvals.ApprovalRequest).filter_by(status=approvals.ApprovalStatus.PENDING.value).order_by(approvals.ApprovalRequest.requested_at.desc()).all()
    return templates.TemplateResponse(request, "approvals.html", {"pending": pending, "csrf_token": dash_session.csrf_secret, "message": None})


@app.post("/approvals/{approval_id}/decide", response_class=HTMLResponse)
def approvals_decide(
    request: Request, approval_id: str, decision: str = Form(...), csrf_token: str = Form(...),
    totp_code: str = Form(""), session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    session: Session = Depends(get_session),
):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    if not _require_csrf(dash_session, csrf_token):
        pending = session.query(approvals.ApprovalRequest).filter_by(status=approvals.ApprovalStatus.PENDING.value).all()
        return templates.TemplateResponse(request, "approvals.html", {"pending": pending, "csrf_token": dash_session.csrf_secret, "message": "CSRF check failed - request refused."}, status_code=403)

    message: str
    try:
        updated = approvals.decide_approval(
            session, approval_id, decided_by=f"Mohit (dashboard, session {dash_session.id[:8]})",
            channel=_dashboard_channel, approve=(decision == "approve"),
            session_token=session_token, totp_code=totp_code or None,
        )
        if decision == "approve":
            from gateway import core_execute
            outcome = core_execute.execute_approved_action(session, approval_id)
            message = f"Approved and executed: {outcome.status} - {outcome.detail}"
        else:
            message = f"Rejected approval {approval_id}."
    except (approvals.ApprovalError, approvals.NotOwnerError) as exc:
        message = f"Refused: {exc}"

    pending = session.query(approvals.ApprovalRequest).filter_by(status=approvals.ApprovalStatus.PENDING.value).order_by(approvals.ApprovalRequest.requested_at.desc()).all()
    return templates.TemplateResponse(request, "approvals.html", {"pending": pending, "csrf_token": dash_session.csrf_secret, "message": message})


# --- alerts ---------------------------------------------------------------------------

@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "alerts.html", {"alerts": _recent_alerts(session, limit=100)})


# --- ledger ---------------------------------------------------------------------------

@app.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, q: str = "", session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    query = session.query(ledger.LedgerEntry).order_by(ledger.LedgerEntry.id.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(ledger.LedgerEntry.input_summary.ilike(like) | ledger.LedgerEntry.action.ilike(like) | ledger.LedgerEntry.result.ilike(like))
    rows = query.limit(200).all()
    return templates.TemplateResponse(request, "ledger.html", {"rows": rows, "q": q, "csrf_token": dash_session.csrf_secret, "verify_result": None})


@app.post("/ledger/verify", response_class=HTMLResponse)
def ledger_verify(request: Request, csrf_token: str = Form(...), session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    if not _require_csrf(dash_session, csrf_token):
        return HTMLResponse("CSRF check failed.", status_code=403)
    chain_result = ledger.verify_chain(session)
    anchor_result = ledger_anchor.verify_against_anchors(session, _anchor_sinks)
    rows = session.query(ledger.LedgerEntry).order_by(ledger.LedgerEntry.id.desc()).limit(200).all()
    return templates.TemplateResponse(request, "ledger.html", {"rows": rows, "q": "", "csrf_token": dash_session.csrf_secret, "verify_result": {"chain": chain_result, "anchor": anchor_result}})


# --- missions -------------------------------------------------------------------------

@app.get("/missions", response_class=HTMLResponse)
def missions_page(request: Request, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    rows = session.query(missions.Mission).order_by(missions.Mission.updated_at.desc()).all()
    return templates.TemplateResponse(request, "missions.html", {"missions": rows})


# --- budgets --------------------------------------------------------------------------

@app.get("/budgets", response_class=HTMLResponse)
def budgets_page(request: Request, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session), policy: policy_engine.PolicyDocument = Depends(get_policy)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    monthly = budget_guard.check_monthly_budget(session, policy.monthly_budget_usd, policy.budget_warn_at_pct, policy.budget_stop_at_pct)
    mission_ids = [m.id for m in session.query(missions.Mission).all()]
    mission_budgets = [
        (m_id, budget_guard.check_mission_budget(session, m_id, policy.default_mission_budget_usd, policy.budget_warn_at_pct, policy.budget_stop_at_pct))
        for m_id in mission_ids
    ]
    return templates.TemplateResponse(request, "budgets.html", {"monthly": monthly, "mission_budgets": mission_budgets})


# --- command box (does NOT execute - Milestone 2 not yet approved) --------------------

@app.get("/command", response_class=HTMLResponse)
def command_get(request: Request, session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    recent = session.query(CommandRequest).order_by(CommandRequest.created_at.desc()).limit(20).all()
    return templates.TemplateResponse(request, "command.html", {"csrf_token": dash_session.csrf_secret, "recent": recent, "message": None})


@app.post("/command", response_class=HTMLResponse)
def command_post(request: Request, text: str = Form(...), csrf_token: str = Form(...), session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME), session: Session = Depends(get_session)):
    dash_session = _require_session(session, session_token)
    if dash_session is None:
        return RedirectResponse("/login", status_code=303)
    if not _require_csrf(dash_session, csrf_token):
        recent = session.query(CommandRequest).order_by(CommandRequest.created_at.desc()).limit(20).all()
        return templates.TemplateResponse(request, "command.html", {"csrf_token": dash_session.csrf_secret, "recent": recent, "message": "CSRF check failed."}, status_code=403)

    now = datetime.now(timezone.utc)
    entry = ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="owner", action="dashboard_command", tier=1,
            input_summary=text[:200], tool="dashboard",
            result=f"LOGGED: not executed - Milestone 2 (Hermes) is not yet approved. Text: {text[:500]!r}",
        ),
        ts=now,
    )
    cmd = CommandRequest(id=str(uuid.uuid4()), text=text, created_at=now, ledger_entry_id=entry.id)
    session.add(cmd)
    session.commit()

    recent = session.query(CommandRequest).order_by(CommandRequest.created_at.desc()).limit(20).all()
    return templates.TemplateResponse(request, "command.html", {"csrf_token": dash_session.csrf_secret, "recent": recent, "message": "Logged (not executed) - see the Ledger."})
