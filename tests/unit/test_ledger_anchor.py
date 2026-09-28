import pytest

from core import ledger, ledger_anchor




def _entry(action="a") -> ledger.LedgerEntryInput:
    return ledger.LedgerEntryInput(
        mission_id="m1", agent_role="r", action=action, tier=1, input_summary="x", tool="t", result="ok",
    )


def test_write_anchor_returns_none_on_empty_ledger(session, tmp_path):
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")
    record = ledger_anchor.write_anchor(session, [sink])
    assert record is None


def test_write_anchor_records_the_latest_row(session, tmp_path):
    ledger.append_entry(session, _entry("first"))
    row2 = ledger.append_entry(session, _entry("second"))
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")

    record = ledger_anchor.write_anchor(session, [sink])
    assert record.last_id == row2.id
    assert record.last_hash == row2.hash


def test_file_anchor_sink_persists_across_instances(session, tmp_path):
    ledger.append_entry(session, _entry("first"))
    path = tmp_path / "anchor.jsonl"
    sink1 = ledger_anchor.FileAnchorSink(path)
    ledger_anchor.write_anchor(session, [sink1])

    sink2 = ledger_anchor.FileAnchorSink(path)  # fresh instance, same file
    records = sink2.read_all()
    assert len(records) == 1


def test_verify_against_anchors_ok_on_untampered_ledger(session, tmp_path):
    for i in range(5):
        ledger.append_entry(session, _entry(f"action-{i}"))
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")
    ledger_anchor.write_anchor(session, [sink])

    result = ledger_anchor.verify_against_anchors(session, [sink])
    assert result.ok is True
    assert result.checked_anchors == 1


def test_verify_against_anchors_ok_with_no_anchors_written_yet(session, tmp_path):
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")
    result = ledger_anchor.verify_against_anchors(session, [sink])
    assert result.ok is True
    assert result.checked_anchors == 0


def test_deleting_last_n_rows_is_detected_via_file_anchor_alone(session, tmp_path):
    """The exact scenario asked for: verify_chain() alone would NOT catch
    this (the remaining chain is still internally perfectly consistent) -
    only the external anchor can."""
    for i in range(5):
        ledger.append_entry(session, _entry(f"action-{i}"))
    latest_id_before_deletion = session.query(ledger.LedgerEntry).order_by(ledger.LedgerEntry.id.desc()).first().id
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")
    ledger_anchor.write_anchor(session, [sink])  # anchors the latest row

    # Now simulate an attacker deleting the last 2 rows.
    rows = session.query(ledger.LedgerEntry).order_by(ledger.LedgerEntry.id.desc()).limit(2).all()
    for row in rows:
        session.delete(row)
    session.commit()

    # verify_chain() alone: still reports OK, because what's left is still
    # a perfectly valid, unbroken chain on its own.
    chain_result = ledger.verify_chain(session)
    assert chain_result.ok is True  # proves this check ALONE is insufficient

    # verify_against_anchors(): catches it, because the anchored row no
    # longer exists. Not asserting a specific absolute id here - the
    # Postgres-parametrized run shares a persistent table across the whole
    # test session (tests/unit/conftest.py), so ids keep climbing across
    # runs; only the RELATIVE fact (the anchored row is gone) is a stable
    # thing to assert.
    anchor_result = ledger_anchor.verify_against_anchors(session, [sink])
    assert anchor_result.ok is False
    assert "no longer exists" in anchor_result.reason
    assert anchor_result.failed_anchor.last_id == latest_id_before_deletion


def test_tampering_an_anchored_row_and_its_hash_is_detected(session, tmp_path):
    """verify_against_anchors() checks the STORED hash against what was
    anchored - it does not itself recompute from fields (that's
    ledger.verify_chain()'s job). Its actual threat model is a MORE
    sophisticated attacker who tampers a row's content AND recomputes a
    self-consistent replacement hash (which would defeat verify_chain()
    alone) - they still can't retroactively fix the anchor already written
    to a file/Telegram OUTSIDE the database at the original hash value.
    """
    ledger.append_entry(session, _entry("first"))
    row2 = ledger.append_entry(session, _entry("second"))
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")
    ledger_anchor.write_anchor(session, [sink])

    row2.cost_usd = 999999.0
    row2.hash = "0" * 64  # attacker's faked replacement hash
    session.commit()

    result = ledger_anchor.verify_against_anchors(session, [sink])
    assert result.ok is False
    assert "no longer matches" in result.reason


def test_field_tamper_without_hash_change_is_ledger_verify_chains_job_not_the_anchors(session, tmp_path):
    """Documents the layering: a field tampered WITHOUT updating the
    stored hash is NOT something verify_against_anchors() catches on its
    own (its check is deliberately lightweight: id+hash still present) -
    ledger.verify_chain()'s full recomputation is what catches that half
    of the threat model. The two checks are complementary, not redundant.
    """
    ledger.append_entry(session, _entry("first"))
    row2 = ledger.append_entry(session, _entry("second"))
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")
    ledger_anchor.write_anchor(session, [sink])

    row2.cost_usd = 999999.0  # hash left untouched
    session.commit()

    assert ledger_anchor.verify_against_anchors(session, [sink]).ok is True  # anchor alone: blind to this
    assert ledger.verify_chain(session).ok is False  # verify_chain: catches it via recomputation


def test_multiple_anchors_over_time_all_checked(session, tmp_path):
    ledger.append_entry(session, _entry("first"))
    sink = ledger_anchor.FileAnchorSink(tmp_path / "anchor.jsonl")
    ledger_anchor.write_anchor(session, [sink])  # anchors row 1

    ledger.append_entry(session, _entry("second"))
    ledger_anchor.write_anchor(session, [sink])  # anchors row 2

    result = ledger_anchor.verify_against_anchors(session, [sink])
    assert result.ok is True
    assert result.checked_anchors == 2


def test_telegram_sink_silently_no_ops_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    sink = ledger_anchor.TelegramAnchorSink()
    # Must not raise even though nothing is configured.
    sink.write(ledger_anchor.AnchorRecord(last_id=1, last_hash="abc", timestamp="2026-01-01T00:00:00+00:00"))
    assert sink.read_all() == []
