import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from approvals_bot import poller
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
def session_factory():
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ledger.init_db(engine)
    return ledger.get_session_factory(engine)


@pytest.fixture(autouse=True)
def clean_registry():
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()


class _FakeTelegramClient:
    def __init__(self):
        self.sent, self.answered, self.edited = [], [], []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    def answer_callback_query(self, callback_query_id, text=None):
        self.answered.append((callback_query_id, text))

    def edit_message_text(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))


class _FakePlugin:
    def __init__(self):
        self.calls = []

    def execute(self, params):
        self.calls.append(params)
        return plugins.PluginResult(ok=True, detail="sent")


def _pending_approval_id(session_factory, policy) -> str:
    session = session_factory()
    plugins.register("send_message", _FakePlugin())
    outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify owner",
    )
    session.close()
    return outcome.approval_id


def _callback_update(update_id: int, approval_id: str, chat_id: str, decision: str = "approve") -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": chat_id},
            "message": {"message_id": 42, "text": "card text"},
            "data": f"{decision}:{approval_id}",
        },
    }


def _text_message_update(update_id: int) -> dict:
    return {"update_id": update_id, "message": {"message_id": 1, "text": "hello", "chat": {"id": OWNER_CHAT_ID}}}


def test_dispatch_update_ignores_plain_text_messages(session_factory):
    session = session_factory()
    client = _FakeTelegramClient()
    result = poller.dispatch_update(session, client, _text_message_update(1), OWNER_CHAT_ID)
    session.close()
    assert result is None
    assert client.answered == []


def test_dispatch_update_handles_owner_callback(session_factory, policy):
    approval_id = _pending_approval_id(session_factory, policy)
    session = session_factory()
    client = _FakeTelegramClient()

    result = poller.dispatch_update(session, client, _callback_update(1, approval_id, OWNER_CHAT_ID), OWNER_CHAT_ID)

    assert result.ok is True
    req = approvals.get_approval(session, approval_id)
    assert req.status == approvals.ApprovalStatus.EXECUTED.value
    session.close()


def test_dispatch_update_rejects_non_owner_callback(session_factory, policy):
    approval_id = _pending_approval_id(session_factory, policy)
    session = session_factory()
    client = _FakeTelegramClient()

    result = poller.dispatch_update(session, client, _callback_update(1, approval_id, "someone-else"), OWNER_CHAT_ID)

    assert result.ok is False
    req = approvals.get_approval(session, approval_id)
    assert req.status == approvals.ApprovalStatus.PENDING.value
    session.close()


def test_run_poll_loop_processes_a_scripted_batch_and_advances_offset(session_factory, policy):
    approval_id = _pending_approval_id(session_factory, policy)
    client = _FakeTelegramClient()

    batches = [[_callback_update(5, approval_id, OWNER_CHAT_ID)], []]

    def fetch_updates(offset):
        return batches.pop(0) if batches else []

    final_offset = poller.run_poll_loop(session_factory, client, fetch_updates, OWNER_CHAT_ID, max_iterations=2)

    assert final_offset == 6  # update_id 5 processed -> offset advances to 6
    session = session_factory()
    req = approvals.get_approval(session, approval_id)
    assert req.status == approvals.ApprovalStatus.EXECUTED.value
    session.close()


def test_run_poll_loop_passes_offset_to_fetch_updates(session_factory):
    client = _FakeTelegramClient()
    seen_offsets = []

    def fetch_updates(offset):
        seen_offsets.append(offset)
        if offset == 0:
            return [{"update_id": 10, "message": {}}]  # plain message, ignored, but advances offset
        return []

    poller.run_poll_loop(session_factory, client, fetch_updates, OWNER_CHAT_ID, max_iterations=3)
    assert seen_offsets == [0, 11, 11]


def test_run_poll_loop_stops_after_max_iterations(session_factory):
    client = _FakeTelegramClient()
    call_count = 0

    def fetch_updates(offset):
        nonlocal call_count
        call_count += 1
        return []

    poller.run_poll_loop(session_factory, client, fetch_updates, OWNER_CHAT_ID, max_iterations=4)
    assert call_count == 4
