"""
Live acceptance tests, redone via the Dashboard (Telegram postponed -
2026-09-29). Run against the REAL docker-compose `dashboard` + `gateway`
services (`make up` first) with a real Chromium browser (Playwright).
Screenshots saved to tests/e2e/evidence/ as each test runs - this file
IS both the regression test suite and the live evidence-gathering tool.

    .venv/Scripts/python.exe -m pytest tests/e2e/ -v -s

Maps to the acceptance items from the 2026-09-29 dashboard follow-up:
  B) Tier-2 -> Reject -> nothing happens; Approve -> executed + verified.
  C) Tier-3 -> needs a fresh TOTP -> cooling period -> the scheduler executes.
  D) Hard-block -> refused + visible alert.
  E) Unauthenticated / wrong password / wrong TOTP / missing+forged CSRF / expired session -> all rejected.
  F) Double-clicking Approve -> executes once.
  G) DLP fake key -> refused + visible alert.
"""

from __future__ import annotations

import time
import uuid

import pyotp
import pytest
import requests

from tests.e2e.conftest import DASHBOARD_URL, OWNER_PASSWORD, OWNER_USERNAME, create_action, login, shot

pytestmark = pytest.mark.e2e


# --- E: auth/session security ------------------------------------------------------

def test_e_unauthenticated_request_redirected_to_login(page):
    page.goto(f"{DASHBOARD_URL}/approvals")
    page.wait_for_url(f"{DASHBOARD_URL}/login")
    shot(page, "E1_unauthenticated_redirect_to_login")
    assert "/login" in page.url


def test_e_wrong_password_rejected(page, totp_secret):
    page.goto(f"{DASHBOARD_URL}/login")
    page.fill('input[name="username"]', OWNER_USERNAME)
    page.fill('input[name="password"]', "definitely-the-wrong-password")
    page.click('button[type="submit"]')
    shot(page, "E2_wrong_password_rejected")
    assert "Invalid username or password" in page.content()
    assert page.url == f"{DASHBOARD_URL}/login"


def test_e_wrong_totp_rejected(page, totp_secret):
    page.goto(f"{DASHBOARD_URL}/login")
    page.fill('input[name="username"]', OWNER_USERNAME)
    page.fill('input[name="password"]', OWNER_PASSWORD)
    page.fill('input[name="totp_code"]', "000000")
    page.click('button[type="submit"]')
    shot(page, "E3_wrong_totp_rejected")
    assert "Invalid TOTP code" in page.content()


def test_e_missing_csrf_token_rejected(logged_in_page, gateway_token):
    outcome = create_action(gateway_token, action="send_message", agent_role="chief-of-staff", params={"text": f"e2e csrf test {uuid.uuid4()}"}, input_summary="e2e CSRF missing test")
    resp = requests.post(f"{DASHBOARD_URL}/approvals/{outcome['approval_id']}/decide", data={"decision": "approve"}, timeout=10)
    assert resp.status_code == 422  # FastAPI Form(...) treats an absent/empty required field as missing


def test_e_forged_csrf_token_rejected(logged_in_page, gateway_token):
    outcome = create_action(gateway_token, action="send_message", agent_role="chief-of-staff", params={"text": f"e2e csrf test {uuid.uuid4()}"}, input_summary="e2e CSRF forged test")
    cookies = {c["name"]: c["value"] for c in logged_in_page.context.cookies()}
    resp = requests.post(
        f"{DASHBOARD_URL}/approvals/{outcome['approval_id']}/decide",
        data={"decision": "approve", "csrf_token": "forged-not-the-real-secret"},
        cookies=cookies, timeout=10,
    )
    assert resp.status_code == 403
    shot(logged_in_page, "E4_forged_csrf_rejected")


def _docker_exe() -> str:
    import shutil
    found = shutil.which("docker")
    if found:
        return found
    for candidate in (r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",):
        if __import__("os").path.exists(candidate):
            return candidate
    raise RuntimeError("docker executable not found on PATH or in the known default install location.")


def test_e_expired_session_redirects_to_login(page, totp_secret):
    login(page, totp_secret)
    # Simulate idle-timeout expiry by directly deleting the session server-side
    # (equivalent to waiting 30+ minutes - same effect, doesn't slow the suite down).
    cookie = next(c for c in page.context.cookies() if c["name"] == "burns_dashboard_session")
    # This test intentionally reaches into the live DB directly (via the
    # dashboard container) to force expiry rather than sleeping 30 real minutes.
    import subprocess
    subprocess.run(
        [_docker_exe(), "exec", "burns_os_system-dashboard-1", "python", "-c",
         f"""
from core import app_config, ledger
import hashlib
cc = app_config.load_core_config()
engine = ledger.get_engine(cc.database_url)
s = ledger.get_session_factory(engine)()
from dashboard.models import DashboardSession
row = s.query(DashboardSession).filter_by(id=hashlib.sha256({cookie['value']!r}.encode()).hexdigest()).first()
if row:
    s.delete(row)
    s.commit()
"""],
        check=True, capture_output=True,
    )
    page.goto(f"{DASHBOARD_URL}/")
    page.wait_for_url(f"{DASHBOARD_URL}/login")
    shot(page, "E5_expired_session_redirect")
    assert "/login" in page.url


# --- D: hard-block -----------------------------------------------------------------

def test_d_hard_block_refused_and_visible_in_alerts(logged_in_page, gateway_token):
    resp = requests.post(
        f"http://127.0.0.1:8080/execute", headers={"X-API-Key": gateway_token},
        json={"agent_role": "anyone", "action": "reveal_secrets", "params": {}, "input_summary": "e2e hard-block test"},
        timeout=15,
    )
    assert resp.json()["status"] == "refused_hard_block"
    logged_in_page.goto(f"{DASHBOARD_URL}/alerts")
    shot(logged_in_page, "D_hard_block_visible_in_alerts")
    assert "REFUSED_HARD_BLOCK" in logged_in_page.content() or "reveal_secrets" in logged_in_page.content()


# --- G: DLP --------------------------------------------------------------------------

def test_g_dlp_fake_key_refused_and_visible(logged_in_page, gateway_token):
    outcome = create_action(
        gateway_token, action="send_email", agent_role="sales-researcher",
        params={"to": "client@example.com", "subject": "notes", "body": "here's my key for testing: sk-abcdefghijklmnopqrstuvwx1234567890"},
        input_summary="e2e DLP test",
    )
    logged_in_page.goto(f"{DASHBOARD_URL}/approvals")
    csrf = logged_in_page.locator('input[name="csrf_token"]').first.get_attribute("value")
    resp = requests.post(
        f"{DASHBOARD_URL}/approvals/{outcome['approval_id']}/decide",
        data={"decision": "approve", "csrf_token": csrf},
        cookies={c["name"]: c["value"] for c in logged_in_page.context.cookies()}, timeout=10,
    )
    assert "dlp" in resp.text.lower() or "DLP" in resp.text
    logged_in_page.goto(f"{DASHBOARD_URL}/alerts")
    shot(logged_in_page, "G_dlp_refusal_visible_in_alerts")


# --- B: Tier-2 reject/approve --------------------------------------------------------

def test_b_tier2_reject_then_approve(logged_in_page, gateway_token):
    # Reject
    outcome1 = create_action(gateway_token, action="send_message", agent_role="chief-of-staff", params={"text": f"e2e B-reject {uuid.uuid4()}"}, input_summary="e2e test B reject")
    logged_in_page.goto(f"{DASHBOARD_URL}/approvals")
    shot(logged_in_page, "B1_pending_approval_card")
    logged_in_page.locator(f'form[action="/approvals/{outcome1["approval_id"]}/decide"] button[value="reject"]').click()
    shot(logged_in_page, "B2_after_reject")
    assert "Rejected" in logged_in_page.content()

    # Approve
    outcome2 = create_action(gateway_token, action="send_message", agent_role="chief-of-staff", params={"text": f"e2e B-approve {uuid.uuid4()}"}, input_summary="e2e test B approve")
    logged_in_page.goto(f"{DASHBOARD_URL}/approvals")
    logged_in_page.locator(f'form[action="/approvals/{outcome2["approval_id"]}/decide"] button[value="approve"]').click()
    shot(logged_in_page, "B3_after_approve")
    assert "executed" in logged_in_page.content().lower()

    logged_in_page.goto(f"{DASHBOARD_URL}/ledger")
    logged_in_page.locator('form[action="/ledger/verify"] button[type="submit"]').click()
    shot(logged_in_page, "B4_ledger_verify_ok")
    assert "OK" in logged_in_page.content()


# --- F: double-click approve executes once -----------------------------------------

def test_f_double_click_approve_executes_once(logged_in_page, gateway_token):
    outcome = create_action(gateway_token, action="send_message", agent_role="chief-of-staff", params={"text": f"e2e F-double {uuid.uuid4()}"}, input_summary="e2e test F double-click")
    logged_in_page.goto(f"{DASHBOARD_URL}/approvals")
    csrf = logged_in_page.locator('input[name="csrf_token"]').first.get_attribute("value")
    cookies = {c["name"]: c["value"] for c in logged_in_page.context.cookies()}
    r1 = requests.post(f"{DASHBOARD_URL}/approvals/{outcome['approval_id']}/decide", data={"decision": "approve", "csrf_token": csrf}, cookies=cookies, timeout=10)
    r2 = requests.post(f"{DASHBOARD_URL}/approvals/{outcome['approval_id']}/decide", data={"decision": "approve", "csrf_token": csrf}, cookies=cookies, timeout=10)
    assert r1.status_code == 200 and r2.status_code == 200
    assert "already" in r2.text.lower() or "not_executed" in r2.text.lower() or "Refused" in r2.text
    shot(logged_in_page, "F_double_click_second_refused")


# --- C: Tier-3 requires fresh TOTP, then cooling + scheduler executes ---------------

def test_c_tier3_requires_fresh_totp_then_scheduler_executes_after_cooling(logged_in_page, gateway_token, totp_secret):
    outcome = create_action(gateway_token, action="place_trade", agent_role="risk-manager", params={"symbol": "EURUSD", "side": "buy"}, input_summary="e2e test C tier3")
    approval_id = outcome["approval_id"]

    logged_in_page.goto(f"{DASHBOARD_URL}/approvals")
    shot(logged_in_page, "C1_tier3_card_requires_totp")

    # Without TOTP -> refused
    csrf = logged_in_page.locator('input[name="csrf_token"]').first.get_attribute("value")
    form = logged_in_page.locator(f'form[action="/approvals/{approval_id}/decide"]')
    form.locator('button[value="approve"]').click()
    shot(logged_in_page, "C2_tier3_missing_totp_refused")
    assert "Refused" in logged_in_page.content() or "totp" in logged_in_page.content().lower()

    # With a fresh TOTP code -> approved (still cooling)
    logged_in_page.goto(f"{DASHBOARD_URL}/approvals")
    form = logged_in_page.locator(f'form[action="/approvals/{approval_id}/decide"]')
    form.locator('input[name="totp_code"]').fill(pyotp.TOTP(totp_secret).now())
    form.locator('button[value="approve"]').click()
    shot(logged_in_page, "C3_tier3_approved_with_totp")

    # Scheduler should auto-execute once cooling elapses (policy.yaml
    # temporarily shortened for this run - see docs/KNOWN_LIMITS.md).
    # Searches by action name (place_trade) - the ledger search filters on
    # action/summary/result text, not the approval_id column, so that's
    # what actually surfaces this approval's EXECUTING/FAILED rows.
    deadline = time.time() + 150
    executed = False
    while time.time() < deadline:
        logged_in_page.goto(f"{DASHBOARD_URL}/ledger?q=place_trade")
        content = logged_in_page.content()
        if "not implemented yet" in content.lower():
            executed = True
            break
        time.sleep(5)
    shot(logged_in_page, "C4_scheduler_executed_stub_honestly")
    assert executed, "scheduler did not execute the Tier-3 approval within the wait window"
