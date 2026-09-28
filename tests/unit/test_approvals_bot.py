import pytest

from approvals_bot import bot, formatting
from core import approvals, ledger, policy_engine
from gateway import core_execute, plugins


OWNER_CHAT_ID = "999888777"
REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
    "TELEGRAM_OWNER_CHAT_ID": OWNER_CHAT_ID,
}


@pytest.fixture
def env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    yield


@pytest.fixture
def policy(env):
    return policy_engine.load_policy()


@pytest.fixture
def session():
    engine = ledger.get_engine("sqlite:///:memory:")
    ledger.init_db(engine)
    factory = ledger.get_session_factory(engine)
    s = factory()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_registry():
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()


class _FakeTelegramClient:
    def __init__(self):
        self.sent = []
        self.answered = []
        self.edited = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))

    def edit_message_text(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))


class _FakePlugin:
    def __init__(self, ok=True, detail="sent for real"):
        self.ok = ok
        self.detail = detail
        self.calls = []

    def execute(self, params):
        self.calls.append(params)
        return plugins.PluginResult(ok=self.ok, detail=self.detail)


def _pending_tier2_request(session, policy):
    plugins.register("send_message", _FakePlugin())
    outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hello owner"}, input_summary="notify owner of X",
    )
    return outcome.approval_id


# --- formatting.py -----------------------------------------------------------

def test_build_approval_card_text_includes_key_fields(session, policy):
    approval_id = _pending_tier2_request(session, policy)
    req = approvals.get_approval(session, approval_id)
    text = formatting.build_approval_card_text(req)
    assert req.agent_role in text
    assert req.action in text
    assert req.id in text


def test_tier3_card_mentions_cooling_period(session, policy):
    plugins.register("place_trade", _FakePlugin())
    outcome = core_execute.request_action(
        session, policy, agent_role="risk-manager", mission_id="m1",
        action="place_trade", params={"symbol": "EURUSD"}, input_summary="demo buy",
    )
    req = approvals.get_approval(session, outcome.approval_id)
    text = formatting.build_approval_card_text(req)
    assert "cooling period" in text.lower()


def test_parse_callback_data_roundtrip():
    decision, approval_id = formatting.parse_callback_data("approve:abc-123")
    assert decision == "approve"
    assert approval_id == "abc-123"


def test_parse_callback_data_rejects_malformed():
    with pytest.raises(formatting.CallbackParseError):
        formatting.parse_callback_data("not-valid-data")
    with pytest.raises(formatting.CallbackParseError):
        formatting.parse_callback_data("delete:abc-123")  # not an allowed decision


# --- bot.py: owner enforcement ------------------------------------------------

def test_non_owner_callback_is_rejected_and_does_not_execute(session, policy):
    approval_id = _pending_tier2_request(session, policy)
    client = _FakeTelegramClient()

    result = bot.handle_callback_query(
        session, client,
        callback_query_id="cb1", from_chat_id="111111",  # NOT the owner
        owner_chat_id=OWNER_CHAT_ID, message_id=42, original_text="card text",
        callback_data=f"approve:{approval_id}",
    )
    assert result.ok is False
    req = approvals.get_approval(session, approval_id)
    assert req.status == approvals.ApprovalStatus.PENDING.value  # untouched
    assert client.edited == []  # never touched the message


def test_owner_approve_executes_and_edits_message(session, policy):
    approval_id = _pending_tier2_request(session, policy)
    client = _FakeTelegramClient()

    result = bot.handle_callback_query(
        session, client,
        callback_query_id="cb1", from_chat_id=OWNER_CHAT_ID,
        owner_chat_id=OWNER_CHAT_ID, message_id=42, original_text="card text",
        callback_data=f"approve:{approval_id}",
    )
    assert result.ok is True
    req = approvals.get_approval(session, approval_id)
    # EXECUTED, not APPROVED - execute_approved_action's idempotency guard
    # (core/approvals.py::mark_executing) atomically advances a
    # successfully-run Tier 2/3 action past APPROVED so a second call for
    # the same approval_id can never run the plugin again.
    assert req.status == approvals.ApprovalStatus.EXECUTED.value
    assert len(client.edited) == 1
    assert "APPROVED" in client.edited[0][2]


def test_owner_reject_does_not_execute_plugin(session, policy):
    plugins._REGISTRY.clear()
    fake_plugin = _FakePlugin()
    plugins.register("send_message", fake_plugin)
    outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hello"}, input_summary="notify",
    )
    client = _FakeTelegramClient()

    result = bot.handle_callback_query(
        session, client,
        callback_query_id="cb1", from_chat_id=OWNER_CHAT_ID,
        owner_chat_id=OWNER_CHAT_ID, message_id=42, original_text="card text",
        callback_data=f"reject:{outcome.approval_id}",
    )
    assert result.ok is True  # the REJECTION itself was handled correctly
    assert fake_plugin.calls == []  # but the underlying action never ran
    req = approvals.get_approval(session, outcome.approval_id)
    assert req.status == approvals.ApprovalStatus.REJECTED.value


def test_malformed_callback_data_handled_gracefully(session, policy):
    client = _FakeTelegramClient()
    result = bot.handle_callback_query(
        session, client,
        callback_query_id="cb1", from_chat_id=OWNER_CHAT_ID,
        owner_chat_id=OWNER_CHAT_ID, message_id=42, original_text="card text",
        callback_data="garbage",
    )
    assert result.ok is False
    assert client.answered[-1][1] == "Malformed request."


def test_send_approval_card_calls_client_with_keyboard(session, policy):
    approval_id = _pending_tier2_request(session, policy)
    req = approvals.get_approval(session, approval_id)
    client = _FakeTelegramClient()

    bot.send_approval_card(client, OWNER_CHAT_ID, req)
    assert len(client.sent) == 1
    chat_id, text, reply_markup = client.sent[0]
    assert chat_id == OWNER_CHAT_ID
    assert reply_markup["inline_keyboard"][0][0]["callback_data"] == f"approve:{req.id}"
