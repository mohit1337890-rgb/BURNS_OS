"""
Renders deploy/hermes/{researcher,chief}/config.yaml from their .template
files, substituting the real HERMES_MCP_TOKEN_* value from .env.
config.yaml.template is committed (no secret in it); the rendered
config.yaml is gitignored (has the real bearer token) - same pattern as
.env.example -> .env.

Run this once after any change to a template, or after rotating a
HERMES_MCP_TOKEN_* value (scripts/admin_rotate_hermes_token.py, once
that exists - manual for now, see docs/MILESTONE_2_HERMES_DESIGN.md
decision 1).

Usage: .venv/Scripts/python.exe -m scripts.generate_hermes_config
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_value(key: str) -> str:
    env_path = REPO_ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{key} not found in .env")


def main() -> int:
    substitutions = {
        "researcher": {"__HERMES_MCP_TOKEN_RESEARCHER__": _env_value("HERMES_MCP_TOKEN_RESEARCHER")},
        "chief": {"__HERMES_MCP_TOKEN_CHIEF__": _env_value("HERMES_MCP_TOKEN_CHIEF")},
    }
    for profile, subs in substitutions.items():
        template_path = REPO_ROOT / "deploy" / "hermes" / profile / "config.yaml.template"
        out_path = REPO_ROOT / "deploy" / "hermes" / profile / "config.yaml"
        text = template_path.read_text(encoding="utf-8")
        for placeholder, value in subs.items():
            text = text.replace(placeholder, value)
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path}")

    # Each profile's own .env (LITELLM_VIRTUAL_KEY_* referenced by
    # config.yaml's key_env:, API_SERVER_* for the Dashboard -> hermes-chief
    # Command box call) - the main .env is the single source of truth for
    # all of these; this only ever copies, never generates independently
    # (a prior version of this script generated API_SERVER_KEY fresh, in
    # isolation, which the Dashboard could then never have authenticated
    # with - fixed 2026-09-29).
    env_vars = {
        "researcher": {
            "LITELLM_VIRTUAL_KEY_RESEARCHER": _env_value("LITELLM_VIRTUAL_KEY_RESEARCHER"),
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": "0.0.0.0",
            "API_SERVER_KEY": _env_value("HERMES_RESEARCHER_API_SERVER_KEY"),
        },
        "chief": {
            "LITELLM_VIRTUAL_KEY_CHIEF": _env_value("LITELLM_VIRTUAL_KEY_CHIEF"),
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": "0.0.0.0",
            "API_SERVER_KEY": _env_value("HERMES_CHIEF_API_SERVER_KEY"),
        },
    }
    for profile, values in env_vars.items():
        out_path = REPO_ROOT / "deploy" / "hermes" / profile / ".env"
        out_path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
