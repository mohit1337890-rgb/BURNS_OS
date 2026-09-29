"""
Direct unit tests for dashboard/auth.py - password hashing, TOTP
enrollment/verification, login rate-limit/lockout, session lifecycle
(idle timeout, hashed-at-rest tokens), and CSRF token verification.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pyotp
import pytest

from dashboard import auth


# --- password -------------------------------------------------------------------

def test_password_hash_is_not_the_plaintext():
    h = auth.hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert auth.verify_password("correct horse battery staple", h) is True


def test_wrong_password_does_not_verify():
    h = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("wrong password", h) is False


# --- owner account bootstrap ------------------------------------------------------

def test_bootstrap_owner_account_creates_exactly_one(session):
    owner = auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    assert owner.id is not None
    assert auth.get_owner_account(session).username == "mohit"


def test_bootstrap_owner_account_refuses_a_second_time(session):
    auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    with pytest.raises(ValueError, match="already exists"):
        auth.bootstrap_owner_account(session, username="someone-else", password="x")


# --- TOTP ---------------------------------------------------------------------------

def test_totp_enrollment_requires_a_valid_code_to_confirm(session):
    owner = auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    secret, qr_png = auth.start_totp_enrollment(owner)
    assert qr_png[:8] == b"\x89PNG\r\n\x1a\n"  # a real PNG, not a placeholder
    assert owner.totp_secret is None  # not stored until confirmed

    ok = auth.confirm_totp_enrollment(session, owner, secret, "000000")  # near-certainly wrong
    assert ok is False
    assert owner.totp_secret is None

    real_code = pyotp.TOTP(secret).now()
    ok = auth.confirm_totp_enrollment(session, owner, secret, real_code)
    assert ok is True
    assert owner.totp_secret == secret
    assert owner.totp_enrolled_at is not None


def test_verify_totp_now_accepts_the_current_code_and_rejects_a_wrong_one(session):
    owner = auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    secret, _ = auth.start_totp_enrollment(owner)
    auth.confirm_totp_enrollment(session, owner, secret, pyotp.TOTP(secret).now())

    assert auth.verify_totp_now(session, pyotp.TOTP(secret).now()) is True
    assert auth.verify_totp_now(session, "000000") is False


def test_verify_totp_now_false_when_not_enrolled(session):
    auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    assert auth.verify_totp_now(session, "123456") is False


# --- login / lockout ------------------------------------------------------------

def test_attempt_login_succeeds_with_correct_password_no_totp(session):
    auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    result = auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code=None)
    assert result.ok is True


def test_attempt_login_fails_with_wrong_password(session):
    auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    result = auth.attempt_login(session, username="mohit", password="wrong", totp_code=None)
    assert result.ok is False


def test_attempt_login_requires_totp_once_enrolled(session):
    owner = auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    secret, _ = auth.start_totp_enrollment(owner)
    auth.confirm_totp_enrollment(session, owner, secret, pyotp.TOTP(secret).now())

    no_totp = auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code=None)
    assert no_totp.ok is False

    wrong_totp = auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code="000000")
    assert wrong_totp.ok is False

    right_totp = auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code=pyotp.TOTP(secret).now())
    assert right_totp.ok is True


def test_repeated_failed_logins_lock_the_account(session):
    owner = auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    for _ in range(auth.MAX_FAILED_LOGINS):
        auth.attempt_login(session, username="mohit", password="wrong", totp_code=None)

    assert auth.is_locked_out(owner) is True
    # Even the CORRECT password is refused while locked out.
    result = auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code=None)
    assert result.ok is False
    assert "locked" in result.reason.lower()


def test_lockout_clears_after_the_lockout_window(session):
    owner = auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    for _ in range(auth.MAX_FAILED_LOGINS):
        auth.attempt_login(session, username="mohit", password="wrong", totp_code=None)
    assert auth.is_locked_out(owner) is True

    future = datetime.now(timezone.utc) + timedelta(minutes=auth.LOCKOUT_MINUTES + 1)
    assert auth.is_locked_out(owner, now=future) is False
    result = auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code=None, now=future)
    assert result.ok is True


def test_successful_login_resets_failed_count(session):
    owner = auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    auth.attempt_login(session, username="mohit", password="wrong", totp_code=None)
    auth.attempt_login(session, username="mohit", password="wrong", totp_code=None)
    assert owner.failed_login_count == 2
    auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code=None)
    assert owner.failed_login_count == 0


def test_login_attempts_are_logged_to_the_ledger(session):
    from core import ledger as ledger_module

    auth.bootstrap_owner_account(session, username="mohit", password="hunter2-very-secret")
    auth.attempt_login(session, username="mohit", password="wrong", totp_code=None)
    auth.attempt_login(session, username="mohit", password="hunter2-very-secret", totp_code=None)

    rows = session.query(ledger_module.LedgerEntry).filter_by(action="dashboard_login").order_by(ledger_module.LedgerEntry.id.asc()).all()
    assert len(rows) == 2
    assert "FAILED" in rows[0].result
    assert "OK" in rows[1].result


# --- sessions ---------------------------------------------------------------------

def test_create_session_returns_a_raw_token_not_stored_verbatim(session):
    raw_token, row = auth.create_session(session)
    assert row.id != raw_token  # only the hash is stored
    assert len(raw_token) > 20


def test_get_valid_session_finds_a_fresh_session(session):
    raw_token, _ = auth.create_session(session)
    found = auth.get_valid_session(session, raw_token)
    assert found is not None


def test_get_valid_session_none_for_a_bogus_token(session):
    assert auth.get_valid_session(session, "not-a-real-token") is None


def test_get_valid_session_expires_after_idle_timeout(session):
    now = datetime.now(timezone.utc)
    raw_token, _ = auth.create_session(session, now=now)
    just_past_timeout = now + timedelta(minutes=auth.SESSION_IDLE_TIMEOUT_MINUTES + 1)
    assert auth.get_valid_session(session, raw_token, now=just_past_timeout) is None


def test_get_valid_session_slides_the_idle_window_forward(session):
    now = datetime.now(timezone.utc)
    raw_token, _ = auth.create_session(session, now=now)
    almost_timeout = now + timedelta(minutes=auth.SESSION_IDLE_TIMEOUT_MINUTES - 1)
    assert auth.get_valid_session(session, raw_token, now=almost_timeout) is not None
    # Activity just before the deadline should push the deadline forward -
    # another (SESSION_IDLE_TIMEOUT_MINUTES - 1) later should still be valid.
    still_valid = almost_timeout + timedelta(minutes=auth.SESSION_IDLE_TIMEOUT_MINUTES - 1)
    assert auth.get_valid_session(session, raw_token, now=still_valid) is not None


def test_invalidate_session_logs_out(session):
    raw_token, _ = auth.create_session(session)
    auth.invalidate_session(session, raw_token)
    assert auth.get_valid_session(session, raw_token) is None


# --- CSRF -------------------------------------------------------------------------

def test_csrf_token_matching_the_session_secret_verifies(session):
    _, row = auth.create_session(session)
    assert auth.verify_csrf(row, row.csrf_secret) is True


def test_csrf_token_not_matching_is_refused(session):
    _, row = auth.create_session(session)
    assert auth.verify_csrf(row, "forged-token") is False


def test_csrf_missing_token_is_refused(session):
    _, row = auth.create_session(session)
    assert auth.verify_csrf(row, None) is False
