from __future__ import annotations

from core import research_reports


def test_create_and_get_report_roundtrips(session):
    report = research_reports.create_report(
        session, query="XAUUSD news this week", report_text_untrusted="wrapped-content-here",
        source_urls=["https://reuters.com/a"], command_request_id="cmd-1",
    )
    fetched = research_reports.get_report(session, report.id)
    assert fetched is not None
    assert fetched.report_text_untrusted == "wrapped-content-here"
    assert fetched.source_urls == ["https://reuters.com/a"]
    assert fetched.command_request_id == "cmd-1"


def test_get_report_none_for_unknown_id(session):
    assert research_reports.get_report(session, "does-not-exist") is None


def test_get_latest_report_for_command_returns_most_recent(session):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    research_reports.create_report(
        session, query="q1", report_text_untrusted="old", source_urls=[],
        command_request_id="cmd-x", now=now - timedelta(minutes=5),
    )
    newest = research_reports.create_report(
        session, query="q2", report_text_untrusted="new", source_urls=[],
        command_request_id="cmd-x", now=now,
    )
    latest = research_reports.get_latest_report_for_command(session, "cmd-x")
    assert latest.id == newest.id
    assert latest.report_text_untrusted == "new"


def test_get_latest_report_for_command_none_when_no_match(session):
    assert research_reports.get_latest_report_for_command(session, "no-such-command") is None
