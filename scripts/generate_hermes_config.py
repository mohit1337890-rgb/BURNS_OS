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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
