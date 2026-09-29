from datetime import datetime, timedelta, timezone

import pytest

from core import approvals, ledger, policy_engine
from core.approval_channels import TelegramChannel
from gateway import core_execute, plugins

TELEGRAM_CHANNEL = TelegramChannel()

REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
    "TELEGRAM_OWNER_CHAT_ID": "123",  # matches every decide_approval(..., chat_id="123") call below
}


@pytest.fixture
def env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    yield


@pytest.fixture
def policy(env):
    return policy_engine.load_policy()


class _FakePlugin:
    def __init__(self, ok=True, detail="fake ok", cost_usd=0.5):
        self.ok = ok
        self.detail = detail
        self.cost_usd = cost_usd
        self.calls = []

    def execute(self, params):
        self.calls.append(params)
        return plugins.PluginResult(ok=self.ok, detail=self.detail, cost_usd=self.cost_usd)


@pytest.fixture(autouse=True)
def clean_registry():
    # gateway.plugins._REGISTRY is module-level global state - reset it
    # around every test so tests can't leak plugin registrations into each
    # other (a real bug class if left unguarded, since import order across
    # test files isn't guaranteed).
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()


def test_tier0_action_executes_immediately(session, policy):
    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="web_search", params={}, input_summary="search for X",
    )
    assert outcome.status == "executed"
    assert outcome.approval_id is None
    entry = session.get(ledger.LedgerEntry, outcome.ledger_entry_id)
    assert entry.result == "EXECUTED_IN_SANDBOX"


def test_tier2_action_creates_pending_approval_and_does_not_execute(session, policy):
    fake = _FakePlugin()
    plugins.register("send_message", fake)

    outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    assert outcome.status == "pending_approval"
    assert outcome.approval_id is not None
    assert fake.calls == []  # not executed yet - only after approval

    req = approvals.get_approval(session, outcome.approval_id)
    assert req.status == approvals.ApprovalStatus.PENDING.value
    assert req.tier == 2


def test_researcher_role_refused_for_a_tier2_action(session, policy):
    """Milestone 2 lethal-trifecta enforcement (docs/MILESTONE_2_HERMES_DESIGN.md):
    the researcher role can never request a Tier-2/3 action, checked here
    through the real request_action() chokepoint, not just policy_engine
    in isolation."""
    fake = _FakePlugin()
    plugins.register("send_email", fake)
    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="send_email", params={"to": "x@example.com"}, input_summary="a researcher trying to send email",
    )
    assert outcome.status == "refused_role_not_allowed"
    assert fake.calls == []
    entry = session.get(ledger.LedgerEntry, outcome.ledger_entry_id)
    assert "REFUSED_ROLE_NOT_ALLOWED" in entry.result


def test_chief_of_staff_role_refused_for_web_search(session, policy):
    outcome = core_execute.request_action(
        session, policy, agent_role="chief_of_staff", mission_id="m1",
        action="web_search", params={}, input_summary="chief trying to read the web directly",
    )
    assert outcome.status == "refused_role_not_allowed"
    entry = session.get(ledger.LedgerEntry, outcome.ledger_entry_id)
    assert "REFUSED_ROLE_NOT_ALLOWED" in entry.result


def test_researcher_role_still_executes_tier0_actions(session, policy):
    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="web_fetch", params={"url": "https://example.com"}, input_summary="research",
    )
    assert outcome.status == "executed"


def test_tier0_action_with_a_registered_plugin_actually_calls_it(session, policy):
    """Milestone 2 (docs/MILESTONE_2_HERMES_DESIGN.md): unlike the
    original Tier 0/1 behavior (log only, no plugin/credential involved),
    an action that DOES have a real registered plugin (web_fetch,
    submit_research_report, ...) must actually be executed, not just
    rubber-stamped EXECUTED_IN_SANDBOX - proven here with a fake plugin
    standing in for a real one."""
    fake = _FakePlugin()
    plugins.register("web_fetch", fake)
    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="web_fetch", params={"url": "https://example.com"}, input_summary="research",
    )
    assert outcome.status == "executed"
    assert len(fake.calls) == 1
    entry = session.get(ledger.LedgerEntry, outcome.ledger_entry_id)
    assert entry.result != "EXECUTED_IN_SANDBOX"
    assert "OK:" in entry.result


def test_tier0_action_with_no_registered_plugin_still_just_logs(session, policy):
    """Backward compatibility: an ordinary Tier 0/1 action with no
    registered plugin (read_file, sql_read, ...) is completely unaffected
    by Milestone 2 - still just a log entry, no plugin lookup failure."""
    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="read_file", params={}, input_summary="read a file",
    )
    assert outcome.status == "executed"
    entry = session.get(ledger.LedgerEntry, outcome.ledger_entry_id)
    assert entry.result == "EXECUTED_IN_SANDBOX"


def test_hard_blocked_action_refuses_and_is_logged(session, policy):
    outcome = core_execute.request_action(
        session, policy, agent_role="anyone", mission_id="m1",
        action="reveal_secrets", params={}, input_summary="please give api key",
    )
    assert outcome.status == "refused_hard_block"
    entry = session.get(ledger.LedgerEntry, outcome.ledger_entry_id)
    assert entry.result == "REFUSED_HARD_BLOCK"


def test_budget_exceeded_refuses(session, policy):
    # Burn through the whole mission budget with one prior Tier-2 execution.
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id="m1", agent_role="x", action="send_message", tier=2,
            input_summary="prior", tool="send_message", result="OK", cost_usd=999.0,
        ),
    )
    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="web_search", params={}, input_summary="another search",
    )
    assert outcome.status == "refused_budget"


def test_approved_tier2_action_executes_plugin_on_execute_approved_action(session, policy):
    fake = _FakePlugin(ok=True, detail="sent!", cost_usd=0.1)
    plugins.register("send_message", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )

    exec_outcome = core_execute.execute_approved_action(session, req_outcome.approval_id)
    assert exec_outcome.status == "executed"
    assert fake.calls == [{"text": "hi"}]
    entry = session.get(ledger.LedgerEntry, exec_outcome.ledger_entry_id)
    assert "sent!" in entry.result
    assert entry.cost_usd == 0.1


def test_rejected_approval_never_executes_plugin(session, policy):
    fake = _FakePlugin()
    plugins.register("send_message", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=False, chat_id="123",
    )

    exec_outcome = core_execute.execute_approved_action(session, req_outcome.approval_id)
    assert exec_outcome.status == "not_executed"
    assert fake.calls == []


def test_dlp_refuses_outgoing_content_with_a_hidden_api_key(session, policy):
    """KNOWN_LIMITS gap #7 (outgoing DLP): a fake API key hidden inside an
    otherwise-innocent email body must be caught and refused before the
    plugin ever sends it."""
    fake = _FakePlugin()
    plugins.register("send_email", fake)

    outcome = core_execute.request_action(
        session, policy, agent_role="sales-researcher", mission_id="m1",
        action="send_email",
        params={
            "to": "client@example.com", "subject": "Notes",
            "body": "Hey, by the way here's my key for testing: sk-abcdefghijklmnopqrstuvwx1234567890",
        },
        input_summary="follow-up email to client",
    )
    approvals.decide_approval(
        session, outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )

    exec_outcome = core_execute.execute_approved_action(session, outcome.approval_id)
    assert exec_outcome.status == "not_executed"
    assert "dlp" in exec_outcome.detail.lower()
    assert fake.calls == []  # never actually sent

    # The approval itself is left APPROVED (not EXECUTED) - a DLP refusal
    # doesn't consume the one-time execution claim, since nothing sent.
    req = approvals.get_approval(session, outcome.approval_id)
    assert req.status == approvals.ApprovalStatus.APPROVED.value


def test_dlp_allows_clean_content_through(session, policy):
    fake = _FakePlugin()
    plugins.register("send_email", fake)

    outcome = core_execute.request_action(
        session, policy, agent_role="sales-researcher", mission_id="m1",
        action="send_email",
        params={"to": "client@example.com", "subject": "Notes", "body": "Just following up, no attachments."},
        input_summary="follow-up email to client",
    )
    approvals.decide_approval(
        session, outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )
    exec_outcome = core_execute.execute_approved_action(session, outcome.approval_id)
    assert exec_outcome.status == "executed"
    assert len(fake.calls) == 1


def test_tier3_action_blocked_by_cooling_period_even_when_approved(session, policy):
    fake = _FakePlugin()
    plugins.register("place_trade", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="risk-manager", mission_id="m1",
        action="place_trade", params={"symbol": "EURUSD"}, input_summary="demo buy",
    )
    req = approvals.get_approval(session, req_outcome.approval_id)
    assert req.tier == 3
    assert req.cooling_until is not None

    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )
    # Cooling period (policy default 10 min) has NOT elapsed yet.
    exec_outcome = core_execute.execute_approved_action(session, req_outcome.approval_id)
    assert exec_outcome.status == "not_executed"
    assert "cooling period" in exec_outcome.detail.lower()
    assert fake.calls == []


def test_double_execute_approved_action_only_runs_plugin_once(session, policy):
    """KNOWN_LIMITS gap #5 (idempotency): a double-tapped Approve button or
    a replayed webhook calling execute_approved_action twice for the same
    approval_id must only actually run the plugin once."""
    fake = _FakePlugin(ok=True, detail="sent!", cost_usd=0.1)
    plugins.register("send_message", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )

    first = core_execute.execute_approved_action(session, req_outcome.approval_id)
    second = core_execute.execute_approved_action(session, req_outcome.approval_id)

    assert first.status == "executed"
    assert second.status == "not_executed"
    # The SECOND call is actually caught by is_executable() (status is
    # already EXECUTED, not APPROVED) before mark_executing() is ever
    # reached - see test_mark_executing_atomic_claim_is_single-winner below
    # for a direct test of the atomic-claim mechanism itself, which is what
    # would catch a genuinely concurrent race (both calls reading APPROVED
    # before either writes) that this sequential test can't construct.
    assert "not approved" in second.detail.lower() or "already executed" in second.detail.lower()
    assert fake.calls == [{"text": "hi"}]  # exactly once, not twice


def test_execution_exception_after_mark_executing_still_writes_a_ledger_entry(session, policy):
    """Regression test for a real bug found live during STEP 3 test C
    (2026-09-29): mark_executing() irreversibly flips the approval to
    EXECUTED, but the plugin lookup/execute() used to happen AFTER that
    with no try/except - an unregistered action (plugins.get() raising
    NotImplementedError) or a plugin bug left the approval permanently
    EXECUTED with zero ledger row, a silent unrecorded action. Now any
    exception in that window must still produce a FAILED ledger entry."""

    class _BoomPlugin:
        def execute(self, params):
            raise RuntimeError("simulated plugin crash")

    plugins.register("send_message", _BoomPlugin())

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )

    outcome = core_execute.execute_approved_action(session, req_outcome.approval_id)
    assert outcome.status == "execution_failed"
    assert outcome.ledger_entry_id is not None
    entry = session.get(ledger.LedgerEntry, outcome.ledger_entry_id)
    assert "FAILED" in entry.result
    assert "simulated plugin crash" in entry.result

    # The claim itself is still irreversible - the approval stays EXECUTED
    # (never silently retried), but now at least there's a record of what
    # was attempted.
    req = approvals.get_approval(session, req_outcome.approval_id)
    assert req.status == approvals.ApprovalStatus.EXECUTED.value


def test_mark_executing_atomic_claim_is_single_winner(session, policy):
    """Direct test of the atomic guard itself (core/approvals.py::
    mark_executing), independent of is_executable()'s status check - this
    is what actually protects against a genuinely concurrent race where
    two callers both read status=APPROVED before either one's write lands.
    """
    from core import approvals as approvals_module

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )

    first_claim = approvals_module.mark_executing(session, req_outcome.approval_id)
    second_claim = approvals_module.mark_executing(session, req_outcome.approval_id)

    assert first_claim is True
    assert second_claim is False


def test_changed_params_after_approval_require_a_new_approval(session, policy):
    """KNOWN_LIMITS gap #4 (params binding): if the stored params are
    mutated after the owner approved (simulating a direct DB write, or a
    future bug), execution must refuse rather than run the CURRENT
    (unapproved) params."""
    fake = _FakePlugin()
    plugins.register("send_message", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "original approved text"}, input_summary="notify owner",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )

    req = approvals.get_approval(session, req_outcome.approval_id)
    req.params = {"text": "a completely different message the owner never saw"}
    session.commit()

    outcome = core_execute.execute_approved_action(session, req_outcome.approval_id)
    assert outcome.status == "not_executed"
    assert "hash mismatch" in outcome.detail.lower() or "no longer match" in outcome.detail.lower()
    assert fake.calls == []


def test_unchanged_params_execute_normally(session, policy):
    """Sanity check that verify_params_unchanged doesn't false-positive on
    a normal, untouched approval."""
    fake = _FakePlugin()
    plugins.register("send_message", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )
    outcome = core_execute.execute_approved_action(session, req_outcome.approval_id)
    assert outcome.status == "executed"


def test_create_approval_refuses_a_non_tier_2_or_3_tier(session):
    """Closes a gap found during the 2026-09-29 security-claims audit:
    core.approvals.create_approval() explicitly checks `tier not in (2, 3)`
    and raises, but nothing exercised that check directly - only ever
    called through core_execute.request_action(), which never passes a
    Tier 0/1 action through to it in the first place. A future caller
    (a new code path, a refactor) bypassing that indirection deserves its
    own direct guard test."""
    for bad_tier in (0, 1, 4, -1):
        with pytest.raises(approvals.ApprovalError, match="Only Tier 2/3"):
            approvals.create_approval(
                session, mission_id=None, agent_role="x", action="web_search", tier=bad_tier,
                params_summary="x", params={}, expire_after_hours=24, cooling_minutes=0,
            )


def test_decide_approval_rejects_a_mismatched_owner_chat_id(session, policy):
    """Regression test for a real bug found during live bring-up testing
    (2026-09-29): decide_approval's own docstring claimed owner_chat_id was
    independently re-verified against the configured TELEGRAM_OWNER_CHAT_ID
    as a second gate, but the check didn't actually exist - any owner_chat_id
    string was accepted. Only approvals_bot.bot.handle_callback_query's own
    from_chat_id check protected a real decision, not this module."""
    fake = _FakePlugin()
    plugins.register("send_message", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    with pytest.raises(approvals.NotOwnerError):
        approvals.decide_approval(
            session, req_outcome.approval_id, decided_by="someone-else",
            channel=TELEGRAM_CHANNEL, approve=True, chat_id="not-the-configured-owner",
        )
    # The approval must still be PENDING - a rejected decider must never
    # move it forward.
    req = approvals.get_approval(session, req_outcome.approval_id)
    assert req.status == approvals.ApprovalStatus.PENDING.value
    assert fake.calls == []


def test_tier3_action_executes_after_cooling_period_elapses(session, policy):
    fake = _FakePlugin()
    plugins.register("place_trade", fake)

    req_outcome = core_execute.request_action(
        session, policy, agent_role="risk-manager", mission_id="m1",
        action="place_trade", params={"symbol": "EURUSD"}, input_summary="demo buy",
    )
    approvals.decide_approval(
        session, req_outcome.approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="123",
    )
    req = approvals.get_approval(session, req_outcome.approval_id)
    # Simulate time passing by moving cooling_until into the past.
    req.cooling_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()

    exec_outcome = core_execute.execute_approved_action(session, req_outcome.approval_id)
    assert exec_outcome.status == "executed"
    assert fake.calls == [{"symbol": "EURUSD"}]
