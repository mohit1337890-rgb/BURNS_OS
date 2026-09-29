"""
Persistence for the researcher -> chief-of-staff handoff (Milestone 2,
Mohit's approval condition 4): submit_research_report/get_research_report
are ordinary Tier-0 actions routed through the SAME
gateway.core_execute.request_action chokepoint as every other action - no
new MCP tool, no direct container-to-container channel between the two
Hermes agents. The stored report text is ALREADY wrapped in the UNTRUSTED
marker (gateway/plugins/web_research.py::wrap_untrusted) before it ever
reaches this table - a compromised/hallucinating researcher cannot
un-wrap its own report on the way in, and the chief-of-staff's own system
prompt is expected to treat whatever get_research_report returns as data,
never instructions (see docs/MILESTONE_2_HERMES_DESIGN.md and the
handoff-injection acceptance test).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, String, Text
from sqlalchemy.orm import Session

from core.ledger import Base


class ResearchReport(Base):
    __tablename__ = "research_report"

    id = Column(String, primary_key=True)
    command_request_id = Column(String, nullable=True)
    query = Column(String, nullable=False)
    report_text_untrusted = Column(Text, nullable=False)
    source_urls = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


def create_report(
    session: Session, *, query: str, report_text_untrusted: str, source_urls: list[str],
    command_request_id: str | None = None, now: datetime | None = None,
) -> ResearchReport:
    now = now or datetime.now(timezone.utc)
    report = ResearchReport(
        id=str(uuid.uuid4()), command_request_id=command_request_id, query=query,
        report_text_untrusted=report_text_untrusted, source_urls=source_urls, created_at=now,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def get_report(session: Session, report_id: str) -> ResearchReport | None:
    return session.get(ResearchReport, report_id)


def get_latest_report_for_command(session: Session, command_request_id: str) -> ResearchReport | None:
    return (
        session.query(ResearchReport)
        .filter_by(command_request_id=command_request_id)
        .order_by(ResearchReport.created_at.desc())
        .first()
    )
