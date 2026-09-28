"""
Outgoing-content DLP (Data Loss Prevention) - closes KNOWN_LIMITS gap #7.

Scans a Tier 2/3 action's params for secrets RIGHT BEFORE they'd leave the
system via a plugin (gateway/core_execute.py::execute_approved_action) -
gitleaks-style regex patterns for common secret SHAPES, plus a direct
substring check against every currently-configured secret VALUE (so a
literal leak of, say, this deployment's own SMTP_PASSWORD is caught even if
it doesn't happen to match any generic pattern). A match refuses the send
and is logged/alertable - see gateway/core_execute.py.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# (pattern, human label) - deliberately conservative/well-known shapes to
# keep the false-positive rate low; gitleaks' own default ruleset covers
# far more, but these are the common ones worth catching without a full
# external dependency.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI/Anthropic-style API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "Slack token"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub personal access token"),
    (re.compile(r"-----BEGIN (RSA|EC|OPENSSH|PGP|DSA) PRIVATE KEY-----"), "PEM private key"),
    (re.compile(r"nvapi-[A-Za-z0-9_-]{20,}"), "NVIDIA API key"),
]

# Env vars whose CURRENT VALUE is treated as a secret to catch verbatim in
# outgoing content, regardless of shape. Kept as a fixed list (not "every
# env var") so an accidental substring match against something mundane
# (e.g. a short, common OWNER_NAME) can't cause false positives.
_SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN", "SMTP_PASSWORD", "GATEWAY_INTERNAL_TOKEN",
    "LITELLM_MASTER_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
    "MT5_PASSWORD", "POSTGRES_PASSWORD", "BURNS_APP_DB_PASSWORD",
)

_MIN_CONFIGURED_SECRET_LEN = 8  # never substring-match a trivially short/empty value


@dataclass(frozen=True)
class DlpFinding:
    label: str
    field: str


def _configured_secret_values() -> dict[str, str]:
    values = {}
    for var in _SECRET_ENV_VARS:
        val = os.environ.get(var, "")
        if len(val) >= _MIN_CONFIGURED_SECRET_LEN:
            values[var] = val
    return values


def scan_text(text: str, field: str = "") -> list[DlpFinding]:
    findings = []
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(DlpFinding(label=label, field=field))
    for var, val in _configured_secret_values().items():
        if val in text:
            findings.append(DlpFinding(label=f"configured {var} value found verbatim", field=field))
    return findings


def scan_params(params: dict) -> list[DlpFinding]:
    """Scans every string value in a plugin params dict (the "outgoing
    content" for send_message/send_email/publish_post/etc.) - non-string
    values are skipped (a symbol name or numeric amount isn't outgoing
    free-text content).
    """
    findings: list[DlpFinding] = []
    for key, value in (params or {}).items():
        if isinstance(value, str):
            findings.extend(scan_text(value, field=key))
    return findings
