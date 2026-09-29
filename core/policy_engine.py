"""
Policy Engine - the single source of truth for "what tier is this action,
and is it allowed at all". Loaded from policies/policy.yaml (declarative,
human-editable) plus policies/authorised_scopes.yaml for security-scan
targets. This module makes decisions; it never executes anything itself -
the Gateway (gateway/app.py) is the only caller that acts on its verdicts.

Design rule this enforces (see BUILD PROMPT section 2, rule 2): permissions
are enforced in code, not only in prompts. An agent cannot talk its way past
this - classify() and is_hard_blocked() are pure functions over the action
name, independent of any LLM-generated justification text.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies" / "policy.yaml"
DEFAULT_SCOPES_PATH = Path(__file__).resolve().parent.parent / "policies" / "authorised_scopes.yaml"

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class PolicyError(Exception):
    """Raised when the policy file itself is invalid - never for a normal
    'this action isn't allowed' outcome, which is a Verdict, not an
    exception. A malformed policy file is a deploy-blocking configuration
    bug, not a runtime decision.
    """


class HardBlockedError(Exception):
    """Raised by classify() when the action is on the hard_block list. The
    caller (Gateway) must catch this, refuse the action, and alert the
    owner - it must never be silently downgraded to a normal tier."""

    def __init__(self, action: str):
        self.action = action
        super().__init__(f"Action '{action}' is hard-blocked and can never be executed.")


class RoleNotAllowedError(Exception):
    """Raised by check_role_limit() when a role listed in policy.yaml's
    `roles:` section requests an action its role forbids, or above its
    max_tier. This is the Milestone 2 lethal-trifecta enforcement
    (docs/MILESTONE_2_HERMES_DESIGN.md) - a researcher-role MCP token can
    never reach a Tier-2/3 action, and a chief_of_staff-role token can
    never call web_search/web_fetch directly, no matter what the caller's
    own request params claim. An agent_role NOT listed in `roles:` is
    unrestricted (backward compatible with every pre-Milestone-2 caller -
    Dashboard, scheduler, approvals_bot - which never had a role name tied
    to a Gateway-issued bearer token in the first place)."""

    def __init__(self, agent_role: str, action: str, reason: str):
        self.agent_role = agent_role
        self.action = action
        super().__init__(f"Role {agent_role!r} may not call '{action}': {reason}")


@dataclass(frozen=True)
class ActionRule:
    action: str
    tier: int
    allowed_domains: tuple[str, ...] = field(default_factory=tuple)
    allowed_agents: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


@dataclass(frozen=True)
class PluginRequirement:
    name: str
    enabled: bool
    required_env: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RoleLimit:
    role: str
    max_tier: int
    forbidden_actions: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class PolicyDocument:
    hard_block: frozenset[str]
    plugins: dict[str, PluginRequirement]
    actions: dict[str, ActionRule]
    default_tier_for_unknown_action: int
    max_risk_per_trade_pct: float
    max_daily_loss_pct: float
    monthly_budget_usd: float
    default_mission_budget_usd: float
    budget_warn_at_pct: float
    budget_stop_at_pct: float
    approval_expire_hours: int
    tier3_cooling_minutes: int
    suggest_auto_approve_after_streak: int
    scope_file: Path
    git_push_allowed_roots: tuple[str, ...]
    roles: dict[str, RoleLimit]


def _substitute_env(raw_text: str) -> str:
    """Replace every ${VAR} in the raw YAML text with the matching
    environment variable, raising a clear PolicyError (not a silent empty
    string) if a referenced variable isn't set - a budget/risk limit
    silently resolving to "" and then to 0 would be a dangerous surprise
    either direction (0% risk blocks all trading; a missing default could
    just as easily parse as unlimited).
    """

    missing: list[str] = []

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        val = os.environ.get(var_name)
        if val is None:
            missing.append(var_name)
            return ""
        return val

    substituted = _ENV_VAR_PATTERN.sub(_replace, raw_text)
    if missing:
        raise PolicyError(
            "policy.yaml references environment variable(s) that are not set: "
            f"{', '.join(sorted(set(missing)))}. Set them in .env before loading the policy."
        )
    return substituted


def load_policy(path: Path | None = None) -> PolicyDocument:
    path = path or DEFAULT_POLICY_PATH
    if not path.exists():
        raise PolicyError(f"Policy file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    substituted = _substitute_env(raw_text)
    try:
        doc = yaml.safe_load(substituted)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy.yaml is not valid YAML: {exc}") from exc

    if not isinstance(doc, dict):
        raise PolicyError("policy.yaml must parse to a mapping at the top level.")

    actions: dict[str, ActionRule] = {}
    for name, spec in (doc.get("actions") or {}).items():
        if "tier" not in spec:
            raise PolicyError(f"Action '{name}' in policy.yaml has no 'tier'.")
        tier = int(spec["tier"])
        if tier not in (0, 1, 2, 3):
            raise PolicyError(f"Action '{name}' has invalid tier {tier} (must be 0-3).")
        actions[name] = ActionRule(
            action=name,
            tier=tier,
            allowed_domains=tuple(spec.get("allowed_domains", []) or []),
            allowed_agents=tuple(spec.get("allowed_agents", []) or []),
            notes=str(spec.get("notes", "")),
        )

    plugins: dict[str, PluginRequirement] = {}
    for name, spec in (doc.get("plugins") or {}).items():
        plugins[name] = PluginRequirement(
            name=name,
            enabled=bool(spec.get("enabled", False)),
            required_env=tuple(spec.get("required_env", []) or []),
        )

    hard_block = frozenset(doc.get("hard_block") or [])
    default_tier = int(doc.get("default_tier_for_unknown_action", 3))
    if default_tier != 3:
        # Not a hard technical requirement, but silently allowing a lower
        # default here is exactly the kind of policy drift rule 2 (BUILD
        # PROMPT section 2) exists to prevent - fail loudly instead.
        raise PolicyError(
            "default_tier_for_unknown_action must be 3 (highest tier) - an "
            f"unrecognized action must never default to something more permissive. Got {default_tier}."
        )

    roles: dict[str, RoleLimit] = {}
    for name, spec in (doc.get("roles") or {}).items():
        if "max_tier" not in spec:
            raise PolicyError(f"Role '{name}' in policy.yaml's roles: section has no 'max_tier'.")
        roles[name] = RoleLimit(
            role=name,
            max_tier=int(spec["max_tier"]),
            forbidden_actions=frozenset(spec.get("forbidden_actions", []) or []),
        )

    trading = doc.get("trading") or {}
    budgets = doc.get("budgets") or {}
    approvals = doc.get("approvals") or {}
    security = doc.get("security") or {}

    return PolicyDocument(
        hard_block=hard_block,
        plugins=plugins,
        actions=actions,
        default_tier_for_unknown_action=default_tier,
        max_risk_per_trade_pct=float(trading.get("max_risk_per_trade_pct", 1.0)),
        max_daily_loss_pct=float(trading.get("max_daily_loss_pct", 3.0)),
        monthly_budget_usd=float(budgets.get("monthly_usd", 0.0)),
        default_mission_budget_usd=float(budgets.get("default_mission_usd", 0.0)),
        budget_warn_at_pct=float(budgets.get("warn_at_pct", 80)),
        budget_stop_at_pct=float(budgets.get("stop_at_pct", 100)),
        approval_expire_hours=int(approvals.get("expire_after_hours", 24)),
        tier3_cooling_minutes=int(approvals.get("tier3_cooling_period_minutes", 10)),
        suggest_auto_approve_after_streak=int(approvals.get("suggest_auto_approve_after_streak", 10)),
        scope_file=(path.parent / security.get("scope_file", "authorised_scopes.yaml")).resolve()
        if security.get("scope_file")
        else DEFAULT_SCOPES_PATH,
        git_push_allowed_roots=tuple(doc.get("git_push_allowed_roots") or []),
        roles=roles,
    )


@dataclass(frozen=True)
class Verdict:
    action: str
    tier: int
    rule: ActionRule | None
    is_unknown_action: bool


def classify(policy: PolicyDocument, action: str) -> Verdict:
    """The single function the Gateway calls before doing anything else.
    Raises HardBlockedError for a hard-blocked action. Returns a Verdict
    (tier 0-3) otherwise - an action with no matching rule is classified at
    policy.default_tier_for_unknown_action (always 3), never silently
    permitted.
    """
    if action in policy.hard_block:
        raise HardBlockedError(action)

    rule = policy.actions.get(action)
    if rule is None:
        return Verdict(action=action, tier=policy.default_tier_for_unknown_action, rule=None, is_unknown_action=True)
    return Verdict(action=action, tier=rule.tier, rule=rule, is_unknown_action=False)


def check_role_limit(policy: PolicyDocument, agent_role: str, verdict: Verdict) -> None:
    """Raises RoleNotAllowedError if `agent_role` is listed in policy.yaml's
    `roles:` section and this action violates its limit. Called by
    gateway.core_execute.request_action() right after classify() succeeds
    (hard-block already excluded) and before the budget check - a
    role-forbidden action is refused for the same reason a hard-block is:
    an authorization decision, not a spend decision.

    An agent_role with no entry in policy.roles is unrestricted - this
    keeps every pre-Milestone-2 caller (Dashboard, scheduler,
    approvals_bot, and every existing test's ad-hoc agent_role string)
    working exactly as before. Only the two Milestone 2 Hermes roles
    (researcher, chief_of_staff), which a Gateway MCP bearer token can
    actually resolve to, are ever listed here.
    """
    limit = policy.roles.get(agent_role)
    if limit is None:
        return
    if verdict.action in limit.forbidden_actions:
        raise RoleNotAllowedError(agent_role, verdict.action, f"'{verdict.action}' is on role {agent_role!r}'s forbidden_actions list.")
    if verdict.tier > limit.max_tier:
        raise RoleNotAllowedError(
            agent_role, verdict.action,
            f"Tier {verdict.tier} exceeds role {agent_role!r}'s max_tier ({limit.max_tier}).",
        )


def load_authorised_scopes(path: Path | None = None) -> list[dict]:
    path = path or DEFAULT_SCOPES_PATH
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return doc.get("scopes") or []


def is_target_authorised(target: str, path: Path | None = None) -> bool:
    """Security-department actions (scan/monitor) must check this in
    ADDITION to the normal tier/approval flow - a scan against a target
    that isn't explicitly listed is refused even at Tier 0/1, because
    'permission to run this kind of action' and 'permission against THIS
    target' are different questions (see policy.yaml's `security` section).
    """
    scopes = load_authorised_scopes(path)
    return any(s.get("target") == target and s.get("owner_confirmed") is True for s in scopes)
