"""
Real periodic process (STEP 2/3 of the live bring-up): calls
core.scheduler.run_due_tier3_executions() and core.ledger_anchor.write_anchor()
on a fixed interval, driven by real wall-clock time - this is the "real
cron/loop" that core/scheduler.py's and core/ledger_anchor.py's own
docstrings describe as deliberately out of scope for those pure-logic
modules (each is unit-tested with a fake "now"/fake sinks; this script is
what actually calls them for real).

Two independently-configurable intervals rather than one shared tick:
Tier-3 due-executions and stale-PENDING expiry should be checked
frequently (a cooling period can elapse at any moment; an approval's
24h expiry is time-sensitive), while the ledger anchor is meant to be
hourly in production - ANCHOR_INTERVAL_SECONDS can be temporarily
shortened (e.g. for a live demo) without that meaning "check every N
seconds" for the scheduler half too.
"""

from __future__ import annotations

import os
import time

from core import app_config, ledger, ledger_anchor, policy_engine, reconciliation, scheduler
from dashboard import reconciliation as command_reconciliation
from gateway import registry_bootstrap


def main() -> None:
    core_config = app_config.load_core_config()
    engine = ledger.get_engine(core_config.database_url)
    session_factory = ledger.get_session_factory(engine)

    policy = policy_engine.load_policy()
    # Without this, gateway.plugins._REGISTRY is empty in this process (it's
    # only populated by gateway/app.py's own lifespan, which this separate
    # process never runs) - core.scheduler.run_due_tier3_executions() would
    # crash with NotImplementedError on its first real due Tier-3 approval
    # instead of running the (possibly stub) plugin. Caught live during
    # STEP 3 test C (2026-09-29): the scheduler crashed on exactly the
    # place_trade stub case it was meant to prove works.
    registry_bootstrap.bootstrap(policy)

    scheduler_interval = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))
    anchor_interval = int(os.environ.get("ANCHOR_INTERVAL_SECONDS", "3600"))
    reconciliation_stale_minutes = int(os.environ.get("RECONCILIATION_STALE_MINUTES", "15"))

    sinks = [ledger_anchor.FileAnchorSink(core_config.anchor_path)]
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        sinks.append(ledger_anchor.TelegramAnchorSink())

    print(
        f"[SCHEDULER_LOOP] starting - tier3/expiry every {scheduler_interval}s, "
        f"anchor every {anchor_interval}s, reconciliation every {scheduler_interval}s "
        f"(stale after {reconciliation_stale_minutes}m), anchor_path={core_config.anchor_path}",
        flush=True,
    )

    last_anchor_at = 0.0
    while True:
        now = time.time()

        session = session_factory()
        try:
            outcomes = scheduler.run_due_tier3_executions(session)
            if outcomes:
                print(f"[SCHEDULER_LOOP] executed {len(outcomes)} due Tier-3 approval(s): {[o.status for o in outcomes]}", flush=True)
        finally:
            session.close()

        session = session_factory()
        try:
            reconciled = reconciliation.run_reconciliation_pass(session, older_than_minutes=reconciliation_stale_minutes)
            if reconciled:
                print(f"[SCHEDULER_LOOP] reconciled {len(reconciled)} stale EXECUTING marker(s) -> UNKNOWN_OUTCOME: {[e.approval_id for e in reconciled]}", flush=True)
        finally:
            session.close()

        # Milestone 2: same reasoning, for CommandRequest rows stuck
        # "dispatched" (dashboard/reconciliation.py) - hermes-chief or the
        # dispatching process can die mid-task same as any plugin call can.
        session = session_factory()
        try:
            reconciled_commands = command_reconciliation.run_command_reconciliation_pass(session, older_than_minutes=reconciliation_stale_minutes)
            if reconciled_commands:
                print(f"[SCHEDULER_LOOP] reconciled {len(reconciled_commands)} stale dispatched command(s) -> UNKNOWN_OUTCOME: {[c.id for c in reconciled_commands]}", flush=True)
        finally:
            session.close()

        if now - last_anchor_at >= anchor_interval:
            session = session_factory()
            try:
                record = ledger_anchor.write_anchor(session, sinks)
                if record is not None:
                    print(f"[SCHEDULER_LOOP] wrote anchor: last_id={record.last_id} hash={record.last_hash[:12]}... at {record.timestamp}", flush=True)
            finally:
                session.close()
            last_anchor_at = now

        time.sleep(scheduler_interval)


if __name__ == "__main__":
    main()
