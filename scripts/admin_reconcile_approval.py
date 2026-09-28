"""
Audited admin script: reconciles a single approval stuck EXECUTED with zero
ledger record (core.reconciliation.reconcile_stuck_approval - see that
module's docstring for why this is a distinct, manual-only path, separate
from the automatic run_reconciliation_pass()).

This is a deliberate one-at-a-time, human-operated tool - it takes an
explicit approval_id and reason on the command line (never scans/guesses),
prints exactly what it found and what it did, and the action itself is
"audited" by virtue of being logged as a real ledger entry (RECONCILED_FAILED,
referencing the approval, with the operator's name and reason baked into
the entry text) - there is no separate audit log to keep in sync.

Usage (inside the gateway container, which has core/ and the real DB env):
    python -m scripts.admin_reconcile_approval <approval_id> \\
        --reason "<why this is being reconciled>" --by "<your name>"
"""

from __future__ import annotations

import argparse
import sys

from core import app_config, ledger, reconciliation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("approval_id", help="The approval to reconcile (must currently be EXECUTED with zero ledger rows).")
    parser.add_argument("--reason", required=True, help="Why this approval is being reconciled - goes verbatim into the ledger entry.")
    parser.add_argument("--by", required=True, dest="reconciled_by", help="Who is running this (goes verbatim into the ledger entry).")
    args = parser.parse_args()

    core_config = app_config.load_core_config()
    engine = ledger.get_engine(core_config.database_url)
    session = ledger.get_session_factory(engine)()
    try:
        entry = reconciliation.reconcile_stuck_approval(
            session, args.approval_id, reason=args.reason, reconciled_by=args.reconciled_by,
        )
        # Read these while the session is still open - reconcile_stuck_approval's
        # own second commit (the approval status update) expires this
        # entry's attributes again, and accessing them after session.close()
        # below would raise DetachedInstanceError (found live, 2026-09-29).
        entry_id, entry_result = entry.id, entry.result
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    print(f"Reconciled approval {args.approval_id} -> FAILED_RECONCILED.")
    print(f"Ledger entry {entry_id}: {entry_result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
