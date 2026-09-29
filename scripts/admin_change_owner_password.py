"""
Audited admin script: rotates the Dashboard's single owner account
password (dashboard.auth.rotate_owner_password).

Requires the CURRENT password to proceed (this is a deliberate rotation
by someone who already knows it, not an account-recovery path - see
docs/DASHBOARD.md's "Resetting the owner account" section for the
no-password-known recovery procedure, which edits the database directly
instead). On success, every existing dashboard session is invalidated
(forces a fresh login everywhere) and the rotation is logged as a real
ledger entry - there is no separate audit log to keep in sync.

Does NOT touch TOTP enrollment - password and TOTP are independent
secrets, so rotating the password never requires re-enrolling 2FA.

Usage (inside the dashboard container, which has the dashboard/ package
and the real DB env - the gateway image does not include dashboard/):
    docker compose exec dashboard python -m scripts.admin_change_owner_password
Prompts interactively (getpass, never a CLI argument) for the current
password, then the new one twice.
"""

from __future__ import annotations

import getpass
import sys

from core import app_config, ledger
from dashboard import auth


def main() -> int:
    core_config = app_config.load_core_config()
    engine = ledger.get_engine(core_config.database_url)
    session = ledger.get_session_factory(engine)()
    try:
        owner = auth.get_owner_account(session)
        if owner is None:
            print("REFUSED: no owner account exists yet - use /setup/owner first.", file=sys.stderr)
            return 1

        current = getpass.getpass("Current password: ")
        if not auth.verify_password(current, owner.password_hash):
            print("REFUSED: current password is incorrect.", file=sys.stderr)
            return 1

        new_password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm new password: ")
        if new_password != confirm:
            print("REFUSED: new password and confirmation do not match.", file=sys.stderr)
            return 1
        if len(new_password) < 12:
            print("REFUSED: new password must be at least 12 characters.", file=sys.stderr)
            return 1

        invalidated = auth.rotate_owner_password(session, owner, new_password)
    finally:
        session.close()

    print("Password rotated successfully.")
    print(f"{invalidated} existing dashboard session(s) invalidated - log in again everywhere.")
    print("TOTP enrollment is unchanged - your existing authenticator app entry still works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
