"""
Channel-agnostic owner authorization for approval decisions.

2026-09-29: Telegram is postponed - the Web Dashboard (dashboard/) is now
the primary approval channel, with Telegram to be re-added later as a
second one. core.approvals.decide_approval() always calls a channel's
own authorize_decision() itself and never trusts a pre-computed "is
this the owner" boolean from the caller - the same "core does its own
independent check" principle that closed a real bug found live
2026-09-29 (decide_approval used to accept owner_chat_id without ever
comparing it to anything - see docs/KNOWN_LIMITS.md).

Each channel is responsible for proving identity in whatever way makes
sense for it (a Telegram chat_id match; a dashboard session lookup plus,
for Tier 3, a freshly-verified TOTP code) - decide_approval() itself
stays channel-agnostic, only ever calling this one interface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from core.approvals import ApprovalRequest


@dataclass(frozen=True)
class AuthorizationResult:
    ok: bool
    reason: str


class ApprovalChannel(Protocol):
    name: str

    def authorize_decision(self, session: Session, req: "ApprovalRequest", **credentials: object) -> AuthorizationResult: ...


class TelegramChannel:
    """Unchanged logic from before the channel split. DISABLED in
    practice, not by a flag here but because nothing currently constructs
    or calls it with a real chat_id - approvals_bot isn't wired into any
    running process while Telegram is postponed (see KNOWN_LIMITS). Kept
    real and testable so re-enabling Telegram later is a wiring change,
    not a rewrite.
    """

    name = "telegram"

    def authorize_decision(self, session: Session, req: "ApprovalRequest", *, chat_id: str | None = None, **_: object) -> AuthorizationResult:
        configured = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")
        if not configured or not chat_id or str(chat_id) != configured:
            return AuthorizationResult(False, "chat_id does not match the configured TELEGRAM_OWNER_CHAT_ID.")
        return AuthorizationResult(True, "ok")


class DashboardChannel:
    """The primary channel now. Two independent checks, both required:

    1. session_token resolves to a live, non-expired dashboard session -
       a real DB lookup (dashboard.auth.get_valid_session), not a claim
       the caller can fake by passing a truthy value.
    2. Tier 3 additionally requires a FRESH TOTP code, verified right now
       against the owner account's TOTP secret (dashboard.auth.verify_totp_now)
       - a valid session alone is not enough for money/irreversible
       actions. This mirrors the existing cooling-period philosophy: the
       highest-stakes tier gets an extra deliberate step a stolen session
       cookie alone can't satisfy.

    Imports dashboard.auth lazily (inside the method, not at module load)
    so core/ never has a hard import-time dependency on dashboard/ - only
    code paths that actually go through the dashboard channel need it
    importable.
    """

    name = "dashboard"

    def authorize_decision(
        self, session: Session, req: "ApprovalRequest", *,
        session_token: str | None = None, totp_code: str | None = None, **_: object,
    ) -> AuthorizationResult:
        from dashboard import auth as dashboard_auth

        if not session_token:
            return AuthorizationResult(False, "No dashboard session token provided.")
        dash_session = dashboard_auth.get_valid_session(session, session_token)
        if dash_session is None:
            return AuthorizationResult(False, "No valid (non-expired) dashboard session for this token.")

        if req.tier == 3:
            if not totp_code:
                return AuthorizationResult(False, "Tier 3 approvals require a fresh TOTP code.")
            if not dashboard_auth.verify_totp_now(session, totp_code):
                return AuthorizationResult(False, "TOTP code invalid.")

        return AuthorizationResult(True, "ok")
