"""
Single config schema (closes KNOWN_LIMITS gap #9). Two different failure
philosophies, on purpose:

- CoreConfig: vars the WHOLE Gateway cannot safely run without (DB
  connection, the ledger tail-truncation anchor path, the owner's Telegram
  chat id, budgets, trading risk limits) - missing any of these fails
  loudly at startup (load_core_config raises), listing every missing var
  at once, not just the first. There is no safe default for "what's the
  risk limit" or "who is the owner" - guessing would be actively dangerous.
- Plugin requirements (check_plugin_ready): a plugin missing ITS OWN
  required env vars, or explicitly disabled in policy.yaml, must refuse to
  execute (see gateway/registry_bootstrap.py) - but must NOT crash the
  whole Gateway. One unconfigured SMTP account shouldn't take down
  Telegram approvals.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ValidationError

from core import policy_engine


class ConfigError(Exception):
    pass


class CoreConfig(BaseModel):
    database_url: str
    anchor_path: Path
    owner_chat_id: str
    monthly_ai_budget_usd: float
    default_mission_budget_usd: float
    max_risk_per_trade_pct: float
    max_daily_loss_pct: float


# (env var name, CoreConfig field name) - BURNS_APP_DB_PASSWORD deliberately
# excluded: an empty password is a valid (if insecure) local-trust-auth
# config, not a "missing required value" the way a missing host/port/db/
# user would be.
#
# POSTGRES_USER/POSTGRES_PASSWORD (the admin/superuser role - see
# db/migrations/env.py) are deliberately NOT here and never feed into
# database_url below - this is the app's OWN connection, which must always
# be the restricted burns_app role (BURNS_APP_DB_USER/BURNS_APP_DB_PASSWORD,
# created by migration 0003) - closes KNOWN_LIMITS gap #10 (the app's own
# runtime credential must never be a Postgres superuser).
_REQUIRED_CORE_VARS = (
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "BURNS_APP_DB_USER",
    "LEDGER_ANCHOR_PATH", "TELEGRAM_OWNER_CHAT_ID",
    "MONTHLY_AI_BUDGET_USD", "DEFAULT_MISSION_BUDGET_USD",
    "MAX_RISK_PER_TRADE_PCT", "MAX_DAILY_LOSS_PCT",
)


def load_core_config(env: Mapping[str, str] | None = None) -> CoreConfig:
    env = env if env is not None else os.environ
    missing = [v for v in _REQUIRED_CORE_VARS if not env.get(v)]
    if missing:
        raise ConfigError(
            "Missing required core environment variable(s): " + ", ".join(missing) +
            ". These have no safe default and Burns OS will not start without them - see .env.example."
        )

    # Explicit +psycopg2 driver, not a bare "postgresql://" - SQLAlchemy's
    # default DBAPI choice for the bare scheme is version-dependent (2.1
    # prefers psycopg/v3 if it's importable, raising ModuleNotFoundError
    # here since only psycopg2-binary is installed - see requirements.txt).
    # Being explicit removes that ambiguity regardless of which SQLAlchemy
    # version ends up installed.
    database_url = (
        f"postgresql+psycopg2://{env['BURNS_APP_DB_USER']}:{env.get('BURNS_APP_DB_PASSWORD', '')}"
        f"@{env['POSTGRES_HOST']}:{env['POSTGRES_PORT']}/{env['POSTGRES_DB']}"
    )
    try:
        return CoreConfig(
            database_url=database_url,
            anchor_path=Path(env["LEDGER_ANCHOR_PATH"]),
            owner_chat_id=env["TELEGRAM_OWNER_CHAT_ID"],
            monthly_ai_budget_usd=float(env["MONTHLY_AI_BUDGET_USD"]),
            default_mission_budget_usd=float(env["DEFAULT_MISSION_BUDGET_USD"]),
            max_risk_per_trade_pct=float(env["MAX_RISK_PER_TRADE_PCT"]),
            max_daily_loss_pct=float(env["MAX_DAILY_LOSS_PCT"]),
        )
    except (ValueError, ValidationError) as exc:
        raise ConfigError(f"Core config values failed validation: {exc}") from exc


class PluginReadiness(BaseModel):
    name: str
    enabled: bool
    ready: bool
    missing_env: tuple[str, ...] = ()
    reason: str = ""


def check_plugin_ready(requirement: policy_engine.PluginRequirement, env: Mapping[str, str] | None = None) -> PluginReadiness:
    env = env if env is not None else os.environ
    if not requirement.enabled:
        return PluginReadiness(name=requirement.name, enabled=False, ready=False, reason="Disabled in policy.yaml.")

    missing = tuple(v for v in requirement.required_env if not env.get(v))
    if missing:
        return PluginReadiness(
            name=requirement.name, enabled=True, ready=False, missing_env=missing,
            reason=f"Enabled but missing required env var(s): {', '.join(missing)}.",
        )
    return PluginReadiness(name=requirement.name, enabled=True, ready=True, reason="ok")
