"""
Records real LLM token spend into the Ledger so core.budget_guard's
SUM(ledger.cost_usd) - already computed fresh from the Ledger, never a
separately-tracked counter - automatically includes it. Closes
KNOWN_LIMITS gap #3 (LLM spend not in budget): previously nothing wrote LLM
spend into the ledger at all, so an agent's own token usage was invisible
to the budget guard even though the guard's math was already correct once
given real numbers.

TODO: the actual per-call amount today must be passed in by the caller
(e.g. from LiteLLM's response `usage`/cost fields once the LiteLLM proxy is
actually running - see docs/KNOWN_LIMITS.md, Docker/WSL2 not installed on
this dev machine yet) - this module doesn't call LiteLLM itself, it only
provides the one correct place to RECORD a cost once known.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from core import ledger


def record_llm_spend(
    session: Session, *, mission_id: str | None, agent_role: str, model: str, cost_usd: float,
) -> ledger.LedgerEntry:
    if cost_usd < 0:
        raise ValueError(f"cost_usd must be >= 0, got {cost_usd}.")
    return ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=mission_id, agent_role=agent_role, action="llm_call", tier=0,
            input_summary=f"LLM call via {model}", tool=model, result="LLM_SPEND_RECORDED",
            cost_usd=cost_usd,
        ),
    )
