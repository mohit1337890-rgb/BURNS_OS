"""
Pure formatting functions for the Approvals Bot (BUILD PROMPT section 4.4):
turning an ApprovalRequest into the Telegram message text + inline-keyboard
JSON, and decoding a button press back into (decision, approval_id). Kept
free of any network/Telegram-API-client code so it's directly unit-testable
without a real bot token.
"""

from __future__ import annotations

from core.approvals import ApprovalRequest

# callback_data format: "<decision>:<approval_id>" - Telegram caps
# callback_data at 64 bytes, so this must stay short; the approval_id
# (a uuid4, 36 chars) plus a one-word decision prefix comfortably fits.
_DECISIONS = ("approve", "reject")


def build_approval_card_text(req: ApprovalRequest) -> str:
    lines = [
        f"\U0001F514 Approval needed - Tier {req.tier}",
        f"Agent: {req.agent_role}",
        f"Action: {req.action}",
        f"Mission: {req.mission_id or '(none)'}",
        "",
        req.params_summary,
    ]
    if req.tier == 3:
        lines += [
            "",
            "⚠️ Tier 3: money / irreversible / high-risk.",
            f"Mandatory cooling period after approval before this executes.",
        ]
    lines += ["", f"Expires: {req.expires_at.isoformat()}", f"Approval id: {req.id}"]
    return "\n".join(lines)


def build_approval_keyboard(req: ApprovalRequest) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{req.id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{req.id}"},
        ]]
    }


class CallbackParseError(Exception):
    pass


def parse_callback_data(data: str) -> tuple[str, str]:
    parts = data.split(":", 1)
    if len(parts) != 2 or parts[0] not in _DECISIONS:
        raise CallbackParseError(f"Malformed or unrecognized callback_data: {data!r}")
    decision, approval_id = parts
    if not approval_id:
        raise CallbackParseError(f"callback_data missing approval_id: {data!r}")
    return decision, approval_id


def build_outcome_text(original_text: str, *, decided_by_label: str, decision: str, result_detail: str) -> str:
    verb = "APPROVED" if decision == "approve" else "REJECTED"
    return f"{original_text}\n\n---\n{verb} by {decided_by_label}.\n{result_detail}"
