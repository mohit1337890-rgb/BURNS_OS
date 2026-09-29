"""
Playwright e2e fixtures - these run against a REAL LIVE dashboard/gateway
(docker-compose's `dashboard`/`gateway` services, `make up` first), not
TestClient. This is what actually proves the browser-facing security
properties (real cookies, real form submission, real TOTP timing) work,
and doubles as the live acceptance-test evidence
(docs/evidence/2026-09-29-dashboard-acceptance/ - screenshots saved here
too via the `screenshot` fixture).

OWNER_PASSWORD is read from .env (E2E_OWNER_PASSWORD) deliberately, not
hardcoded here - these tests bootstrap the REAL, one-time owner account
on first run (idempotent after that: if the account already exists, they
just log in). This is not test-only throwaway data - it becomes Mohit's
actual dashboard login. See docs/DASHBOARD.md for how to change it
afterward. (A hardcoded copy was caught by the gitleaks pre-commit hook
2026-09-29 and moved out of source into .env.)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pyotp
import pytest
import requests

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8090")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")
OWNER_USERNAME = "mohit"


def _env_value(key: str) -> str:
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{key} not found in .env")


OWNER_PASSWORD = _env_value("E2E_OWNER_PASSWORD")

EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

# The dashboard's owner account is the REAL one (not disposable per-run
# test data) - TOTP enrollment is one-time and the secret is never
# retrievable again afterward (dashboard/auth.py never stores it anywhere
# reversible-by-design). Caching it locally (gitignored - see .gitignore's
# tests/e2e/.totp_secret_cache entry) is what makes this suite genuinely
# RE-RUNNABLE against the same persistent live deployment, rather than
# only ever working once.
_TOTP_CACHE_FILE = Path(__file__).resolve().parent / ".totp_secret_cache"


def _gateway_token() -> str:
    return _env_value("GATEWAY_INTERNAL_TOKEN")


@pytest.fixture(scope="session")
def gateway_token() -> str:
    return _gateway_token()


def create_action(gateway_token: str, *, action: str, agent_role: str, params: dict, input_summary: str) -> dict:
    resp = requests.post(
        f"{GATEWAY_URL}/execute",
        headers={"X-API-Key": gateway_token},
        json={"agent_role": agent_role, "action": action, "params": params, "input_summary": input_summary},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="session", autouse=True)
def ensure_owner_account():
    """Idempotent: if /setup/owner redirects to /login, the account
    already exists from a previous run - nothing to do. TOTP enrollment
    (if not already done) still needs a browser step, handled by the
    `totp_secret` fixture below on first use."""
    resp = requests.get(f"{DASHBOARD_URL}/setup/owner", timeout=10, allow_redirects=False)
    if resp.status_code == 200:
        requests.post(
            f"{DASHBOARD_URL}/setup/owner",
            data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD, "confirm_password": OWNER_PASSWORD},
            timeout=10, allow_redirects=False,
        )
    yield


@pytest.fixture(scope="session")
def totp_secret(ensure_owner_account):
    """Enrolls TOTP via plain HTTP form submission (no screenshot needed
    for this bootstrap step - the login/approval tests below do the real
    Playwright browser work). Caches the secret locally on first success
    (_TOTP_CACHE_FILE) so re-running this suite against the SAME live
    deployment doesn't need to re-enroll (which would be impossible - see
    below) every time.
    """
    if _TOTP_CACHE_FILE.exists():
        return _TOTP_CACHE_FILE.read_text(encoding="utf-8").strip()

    resp = requests.get(f"{DASHBOARD_URL}/setup/totp", timeout=10, allow_redirects=False)
    if resp.status_code != 200:
        pytest.skip(
            "TOTP already enrolled from a previous run but no local cache file exists to recover the "
            f"secret from (by design, dashboard/auth.py never stores it anywhere retrievable) - delete "
            f"{_TOTP_CACHE_FILE} only if you also reset the dashboard's owner_account row."
        )
    # crude extraction of the secret from the rendered <code>...</code>
    text = resp.text
    marker = "<code>"
    start = text.index(marker) + len(marker)
    secret = text[start:text.index("</code>", start)].strip()
    code = pyotp.TOTP(secret).now()
    confirm = requests.post(f"{DASHBOARD_URL}/setup/totp", data={"secret": secret, "code": code}, timeout=10, allow_redirects=False)
    assert confirm.status_code == 303, confirm.text
    _TOTP_CACHE_FILE.write_text(secret, encoding="utf-8")
    return secret


def login(page, totp_secret: str, *, password: str = OWNER_PASSWORD) -> None:
    page.goto(f"{DASHBOARD_URL}/login")
    page.fill('input[name="username"]', OWNER_USERNAME)
    page.fill('input[name="password"]', password)
    page.fill('input[name="totp_code"]', pyotp.TOTP(totp_secret).now())
    page.click('button[type="submit"]')
    page.wait_for_url(f"{DASHBOARD_URL}/")


@pytest.fixture
def logged_in_page(page, totp_secret):
    login(page, totp_secret)
    return page


def shot(page, name: str) -> Path:
    path = EVIDENCE_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    return path
