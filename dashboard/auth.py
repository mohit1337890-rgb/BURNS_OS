"""
Dashboard authentication/authorization primitives: password hashing
(argon2), TOTP 2FA (pyotp), server-side sessions, CSRF tokens, and
login rate-limiting/lockout. Single-owner system - there is exactly one
OwnerAccount row, created once via bootstrap_owner_account().

Every login attempt (success or failure), and every use of a fresh TOTP
code, lands in the same hash-chained Ledger as everything else in this
system (core.ledger.append_entry) - "log every login" isn't a separate,
parallel audit mechanism, it's the same one.

Session tokens: the raw token is a high-entropy random string
(secrets.token_urlsafe), set as an HttpOnly cookie - only its SHA-256
hash is ever stored in the database (dashboard.models.DashboardSession.id),
so a database dump/leak alone is not enough to hijack a live session, the
same reasoning as never storing a plaintext password.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy.orm import Session

from core import ledger
from dashboard.models import DashboardSession, OwnerAccount

_PASSWORD_HASHER = PasswordHasher()

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
SESSION_IDLE_TIMEOUT_MINUTES = 30
TOTP_ISSUER = "Burns OS"


# --- password -----------------------------------------------------------------

def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False


# --- owner account (single row) ------------------------------------------------

def get_owner_account(session: Session) -> OwnerAccount | None:
    return session.query(OwnerAccount).order_by(OwnerAccount.id.asc()).first()


def bootstrap_owner_account(session: Session, *, username: str, password: str, now: datetime | None = None) -> OwnerAccount:
    """Creates the single owner account - refuses if one already exists
    (this is a one-time setup step, not a general "create user" API -
    there is deliberately no multi-user support)."""
    if get_owner_account(session) is not None:
        raise ValueError("An owner account already exists - bootstrap_owner_account() is one-time only.")
    now = now or datetime.now(timezone.utc)
    owner = OwnerAccount(
        username=username, password_hash=hash_password(password),
        created_at=now, failed_login_count=0,
    )
    session.add(owner)
    session.commit()
    session.refresh(owner)
    return owner


def change_password(session: Session, owner: OwnerAccount, new_password: str) -> None:
    owner.password_hash = hash_password(new_password)
    session.commit()


def rotate_owner_password(session: Session, owner: OwnerAccount, new_password: str, now: datetime | None = None) -> int:
    """Used by scripts/admin_change_owner_password.py. Unlike bare
    change_password(), this also invalidates every existing dashboard
    session (a stale session cookie from before the rotation should not
    keep working - the same reasoning a "log out everywhere" button
    would have) and logs the rotation to the Ledger. Does NOT touch
    totp_secret - password and TOTP are independent, so rotating one
    never requires re-enrolling the other."""
    now = now or datetime.now(timezone.utc)
    change_password(session, owner, new_password)
    invalidated = session.query(DashboardSession).delete()
    session.commit()
    _log_ledger(session, action="dashboard_owner_password_rotated", result=f"OK: password rotated, {invalidated} session(s) invalidated", now=now)
    return invalidated


# --- TOTP (2FA) -----------------------------------------------------------------

def start_totp_enrollment(owner: OwnerAccount) -> tuple[str, bytes]:
    """Generates a NEW TOTP secret and a PNG QR code for it - does NOT
    store the secret yet (confirm_totp_enrollment() does, only after the
    owner proves they scanned it correctly by submitting one valid code -
    otherwise a failed/abandoned enrollment could silently lock the owner
    out with a secret they never actually saved in their authenticator app).
    """
    secret = pyotp.random_base32()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=owner.username, issuer_name=TOTP_ISSUER)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return secret, buf.getvalue()


def confirm_totp_enrollment(session: Session, owner: OwnerAccount, pending_secret: str, code: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    totp = pyotp.TOTP(pending_secret)
    if not totp.verify(code, valid_window=1):
        return False
    owner.totp_secret = pending_secret
    owner.totp_enrolled_at = now
    session.commit()
    _log_ledger(session, action="dashboard_totp_enroll", result="OK: TOTP enrolled", now=now)
    return True


def verify_totp_now(session: Session, code: str) -> bool:
    """Used both at login (second factor, if enrolled) and by
    core.approval_channels.DashboardChannel for a Tier-3 approval's
    fresh-code requirement. valid_window=1 allows the immediately
    adjacent 30s window either side (clock skew tolerance) - still a
    "right now" check, not a stale/reused code from an hour ago."""
    owner = get_owner_account(session)
    if owner is None or not owner.totp_secret:
        return False
    return pyotp.TOTP(owner.totp_secret).verify(code, valid_window=1)


# --- login / lockout ------------------------------------------------------------

def _log_ledger(session: Session, *, action: str, result: str, now: datetime | None = None) -> None:
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="owner", action=action, tier=0,
            input_summary=action, tool="dashboard", result=result,
        ),
        ts=now,
    )


def is_locked_out(owner: OwnerAccount, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if owner.locked_until is None:
        return False
    locked_until = owner.locked_until if owner.locked_until.tzinfo else owner.locked_until.replace(tzinfo=timezone.utc)
    return now < locked_until


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    reason: str


def attempt_login(session: Session, *, username: str, password: str, totp_code: str | None, now: datetime | None = None) -> LoginResult:
    now = now or datetime.now(timezone.utc)
    owner = get_owner_account(session)

    if owner is None or owner.username != username:
        _log_ledger(session, action="dashboard_login", result=f"FAILED: unknown username {username!r}", now=now)
        return LoginResult(False, "Invalid username or password.")

    if is_locked_out(owner, now):
        _log_ledger(session, action="dashboard_login", result="FAILED: account locked out", now=now)
        return LoginResult(False, "Account temporarily locked due to repeated failed logins - try again later.")

    if not verify_password(password, owner.password_hash):
        owner.failed_login_count += 1
        if owner.failed_login_count >= MAX_FAILED_LOGINS:
            owner.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
        session.commit()
        _log_ledger(session, action="dashboard_login", result=f"FAILED: wrong password (attempt {owner.failed_login_count})", now=now)
        return LoginResult(False, "Invalid username or password.")

    if owner.totp_secret:
        if not totp_code:
            _log_ledger(session, action="dashboard_login", result="FAILED: TOTP code required but not provided", now=now)
            return LoginResult(False, "TOTP code required.")
        if not pyotp.TOTP(owner.totp_secret).verify(totp_code, valid_window=1):
            owner.failed_login_count += 1
            if owner.failed_login_count >= MAX_FAILED_LOGINS:
                owner.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            session.commit()
            _log_ledger(session, action="dashboard_login", result=f"FAILED: wrong TOTP code (attempt {owner.failed_login_count})", now=now)
            return LoginResult(False, "Invalid TOTP code.")

    owner.failed_login_count = 0
    owner.locked_until = None
    session.commit()
    _log_ledger(session, action="dashboard_login", result="OK: login success", now=now)
    return LoginResult(True, "ok")


# --- sessions -------------------------------------------------------------------

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(session: Session, *, ip_address: str | None = None, user_agent: str | None = None, now: datetime | None = None) -> tuple[str, DashboardSession]:
    now = now or datetime.now(timezone.utc)
    raw_token = secrets.token_urlsafe(32)
    row = DashboardSession(
        id=_hash_token(raw_token),
        created_at=now, last_active_at=now,
        csrf_secret=secrets.token_urlsafe(32),
        ip_address=ip_address, user_agent=user_agent,
    )
    session.add(row)
    session.commit()
    return raw_token, row


def get_valid_session(session: Session, raw_token: str, now: datetime | None = None) -> DashboardSession | None:
    now = now or datetime.now(timezone.utc)
    row = session.query(DashboardSession).filter_by(id=_hash_token(raw_token)).first()
    if row is None:
        return None
    last_active = row.last_active_at if row.last_active_at.tzinfo else row.last_active_at.replace(tzinfo=timezone.utc)
    if now - last_active > timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES):
        session.delete(row)
        session.commit()
        return None
    row.last_active_at = now  # sliding idle-timeout window
    session.commit()
    return row


def invalidate_session(session: Session, raw_token: str) -> None:
    row = session.query(DashboardSession).filter_by(id=_hash_token(raw_token)).first()
    if row is not None:
        session.delete(row)
        session.commit()


def cleanup_expired_sessions(session: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=SESSION_IDLE_TIMEOUT_MINUTES)
    stale = session.query(DashboardSession).filter(DashboardSession.last_active_at < cutoff).all()
    for row in stale:
        session.delete(row)
    if stale:
        session.commit()
    return len(stale)


# --- CSRF -------------------------------------------------------------------------

def verify_csrf(dash_session: DashboardSession, submitted_token: str | None) -> bool:
    if not submitted_token:
        return False
    return hmac.compare_digest(dash_session.csrf_secret, submitted_token)
