"""
SQLAlchemy models for dashboard/auth.py - mutable application state
(current password/TOTP secret, active sessions), not audit history.
See db/migrations/versions/0007_dashboard_tables.py for the schema and
why this is a separate concern from core/ledger.py's append-only rows.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String

from core.ledger import Base


class OwnerAccount(Base):
    __tablename__ = "owner_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    totp_secret = Column(String, nullable=True)
    totp_enrolled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)


class DashboardSession(Base):
    __tablename__ = "dashboard_session"

    id = Column(String, primary_key=True)  # SHA-256 hex of the raw token
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_active_at = Column(DateTime(timezone=True), nullable=False)
    csrf_secret = Column(String, nullable=False)
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)


class CommandRequest(Base):
    __tablename__ = "command_request"

    id = Column(String, primary_key=True)
    text = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    ledger_entry_id = Column(Integer, nullable=True)
