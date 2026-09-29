from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core import ledger, research_reports
from gateway.plugins.web_research import (
    GetResearchReportPlugin,
    SubmitResearchReportPlugin,
    WebFetchPlugin,
    wrap_untrusted,
)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ledger.init_db(engine)
    return ledger.get_session_factory(engine)


def test_wrap_untrusted_contains_markers_and_content():
    wrapped = wrap_untrusted("https://example.com/a", "hello world")
    assert "UNTRUSTED" in wrapped
    assert "hello world" in wrapped
    assert "https://example.com/a" in wrapped
    assert "BEGIN UNTRUSTED CONTENT" in wrapped
    assert "END UNTRUSTED CONTENT" in wrapped


# --- WebFetchPlugin ---------------------------------------------------------------

def _mock_transport(status_code=200, text="page body", headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=text, headers=headers or {})
    return httpx.MockTransport(handler)


def test_web_fetch_refuses_when_allowed_domains_is_empty():
    plugin = WebFetchPlugin(allowed_domains=())
    result = plugin.execute({"url": "https://wikipedia.org/wiki/Gold"})
    assert result.ok is False
    assert "no domains are allow-listed" in result.detail


def test_web_fetch_refuses_a_domain_not_on_the_allowlist():
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=_mock_transport())
    result = plugin.execute({"url": "https://evil-attacker-site.example.com/steal"})
    assert result.ok is False
    assert "not on the allow-listed domain list" in result.detail


def test_web_fetch_allows_an_exact_domain_match():
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=_mock_transport(text="Gold is a chemical element."))
    result = plugin.execute({"url": "https://wikipedia.org/wiki/Gold"})
    assert result.ok is True
    assert "Gold is a chemical element." in result.detail
    assert "UNTRUSTED" in result.detail  # every result is wrapped
    assert result.evidence_links == ["https://wikipedia.org/wiki/Gold"]


def test_web_fetch_sends_an_identifiable_user_agent():
    """Found live 2026-09-29: Wikipedia (an allow-listed domain) returns a
    flat 403 for a bare/default User-Agent, per its own published policy -
    a regression here would silently break every fetch against it."""
    from gateway.plugins import web_research

    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["user-agent"] = request.headers.get("user-agent")
        return httpx.Response(200, text="ok")

    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=httpx.MockTransport(handler))
    plugin.execute({"url": "https://wikipedia.org/wiki/Gold"})
    assert seen_headers["user-agent"] == web_research.USER_AGENT
    assert seen_headers["user-agent"] != ""


def test_web_fetch_allows_a_subdomain_of_an_allowlisted_domain():
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=_mock_transport(text="subdomain content"))
    result = plugin.execute({"url": "https://en.wikipedia.org/wiki/Gold"})
    assert result.ok is True


def test_web_fetch_does_not_match_a_lookalike_domain():
    """wikipedia.org.evil.com must NOT match the wikipedia.org allow-list
    entry - a naive suffix/substring check would wrongly allow this."""
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=_mock_transport())
    result = plugin.execute({"url": "https://wikipedia.org.evil.com/phish"})
    assert result.ok is False
    assert "not on the allow-listed domain list" in result.detail


def test_web_fetch_refuses_missing_url():
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",))
    result = plugin.execute({})
    assert result.ok is False
    assert "requires a 'url'" in result.detail


def test_web_fetch_does_not_follow_redirects():
    transport = _mock_transport(status_code=302, headers={"Location": "https://evil.example.com"})
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=transport)
    result = plugin.execute({"url": "https://wikipedia.org/wiki/Gold"})
    assert result.ok is False
    assert "redirect" in result.detail


def test_web_fetch_refuses_on_http_error_status():
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=_mock_transport(status_code=404))
    result = plugin.execute({"url": "https://wikipedia.org/wiki/DoesNotExist"})
    assert result.ok is False
    assert "404" in result.detail


def test_web_fetch_truncates_oversized_response():
    from gateway.plugins import web_research

    long_text = "x" * (web_research.MAX_RESPONSE_BYTES + 100)
    plugin = WebFetchPlugin(allowed_domains=("wikipedia.org",), transport=_mock_transport(text=long_text))
    result = plugin.execute({"url": "https://wikipedia.org/wiki/Gold"})
    assert result.ok is True
    assert "[TRUNCATED]" in result.detail


# --- SubmitResearchReportPlugin / GetResearchReportPlugin --------------------------

def test_submit_research_report_persists_and_wraps(session_factory):
    plugin = SubmitResearchReportPlugin(session_factory)
    result = plugin.execute({
        "query": "XAUUSD news", "report_text": "gold rallied this week",
        "source_urls": ["https://reuters.com/a"], "command_request_id": "cmd-1",
    })
    assert result.ok is True
    assert result.detail.startswith("report_id=")
    report_id = result.detail.split("=", 1)[1]

    s = session_factory()
    stored = research_reports.get_report(s, report_id)
    s.close()
    assert stored is not None
    assert "gold rallied this week" in stored.report_text_untrusted
    assert "UNTRUSTED" in stored.report_text_untrusted


def test_submit_research_report_refuses_without_report_text(session_factory):
    plugin = SubmitResearchReportPlugin(session_factory)
    result = plugin.execute({"query": "x"})
    assert result.ok is False


def test_get_research_report_by_id(session_factory):
    submit = SubmitResearchReportPlugin(session_factory)
    submitted = submit.execute({"query": "q", "report_text": "the actual findings", "source_urls": []})
    report_id = submitted.detail.split("=", 1)[1]

    fetch = GetResearchReportPlugin(session_factory)
    result = fetch.execute({"report_id": report_id})
    assert result.ok is True
    assert "the actual findings" in result.detail


def test_get_research_report_by_command_request_id(session_factory):
    submit = SubmitResearchReportPlugin(session_factory)
    submit.execute({"query": "q", "report_text": "findings", "source_urls": [], "command_request_id": "cmd-42"})

    fetch = GetResearchReportPlugin(session_factory)
    result = fetch.execute({"command_request_id": "cmd-42"})
    assert result.ok is True
    assert "findings" in result.detail


def test_get_research_report_refuses_with_no_identifiers(session_factory):
    result = GetResearchReportPlugin(session_factory).execute({})
    assert result.ok is False


def test_get_research_report_not_found(session_factory):
    result = GetResearchReportPlugin(session_factory).execute({"report_id": "nonexistent"})
    assert result.ok is False
    assert "No matching" in result.detail
