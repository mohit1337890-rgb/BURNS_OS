# Milestone 2, Step 3 — hermes-researcher container (+ hermes-chief scaffolding)

Live evidence, 2026-09-29. Real `nousresearch/hermes-agent:latest` image
(the actual third-party open-source project, not a mock), real
docker-compose services, real network.

## Setup approach

`hermes setup`'s interactive wizard was bypassed entirely for a
reproducible, headless deployment - `deploy/hermes/{researcher,chief}/
config.yaml.template` (committed, no secrets) is rendered to `config.yaml`
(gitignored, has the real MCP bearer token) by
`scripts/generate_hermes_config.py`. Each agent's `.env` (also gitignored)
holds its own LiteLLM virtual key and a generated `API_SERVER_KEY`.

## Condition 1(a)+(b): defense in depth, both layers proven live

**Network isolation** (from inside the real running `hermes-researcher`
and `hermes-chief` containers, `docker exec`):

```
=== internet (must FAIL) ===
OK: internet unreachable (URLError)
=== postgres (must FAIL) ===
OK: postgres unreachable (gaierror: Temporary failure in name resolution - not even DNS-resolvable)
=== litellm (must SUCCEED) ===
OK: litellm reachable
=== gateway-mcp (must SUCCEED) ===
OK: gateway-mcp reachable
```

Identical result from both containers. SMTP is an external host (not a
container in this system) - covered by the same "internet unreachable"
result, since the `hermes_internal` network (`internal: true`) has no
route to any external host at all, not just a specific denylist.

**`disabled_toolsets`**: confirmed via `hermes config` inside the running
container that the full list from `docs/MILESTONE_2_HERMES_DESIGN.md`
section 1 (terminal, file, code_execution, browser, web, image_gen,
video_gen, video, tts, vision, discord, feishu_doc, feishu_drive, spotify,
hermes-yuanbao, x_search, homeassistant, desktop_ui, project, connections,
setup, cronjob, delegation, kanban, skills) loaded correctly from the
rendered config.yaml, unchanged by Hermes's own config-schema migration
(schema 0 -> 46) that runs on first boot. **Not yet confirmed by asking a
live model** whether it can actually still invoke one of these (that
needs a real LLM call - see "Still pending" below).

## MCP connectivity, both agents, real protocol (`hermes mcp test`)

```
hermes-researcher:
  Testing 'burns_gateway'...
  Transport: HTTP -> http://gateway-mcp:8091/mcp
  v Connected (16688ms)
  v Tools discovered: 3
    execute_action / get_approval_status / verify_ledger

hermes-chief:
  Testing 'burns_gateway'...
  v Connected (8349ms)
  v Tools discovered: 3 (same three)
```

Confirms the `tools.include` filter (only these 3 MCP tools, nothing else
from the Gateway's server) works as configured, from Hermes's own side
this time (Steps 1-2 proved the Gateway side).

## api-server requires auth

`POST http://localhost:8642/v1/responses` with no `Authorization` header,
from inside the container itself (loopback) - `401`, confirmed the
`API_SERVER_KEY` gate is live before any request reaches the agent.

## Per-agent LiteLLM virtual keys (condition 6.2)

Real keys issued via LiteLLM's own `/key/generate` API (not the master
key) - `max_budget: 50.0`, `budget_duration: 30d` per agent, matching the
approved $50/month cap. **The separate $5/day sub-limit is not yet
independently enforced** - LiteLLM's key-level budget here is a single
30-day rolling cap, not a dual daily+monthly limit; a true daily cap would
need either a scheduled key-rotation job or a newer LiteLLM feature not
yet evaluated. Flagged honestly rather than claimed as done - `core/
budget_guard.py`'s own Ledger-based caps remain the independent second
layer regardless.

## Still pending (needs a real LLM provider key)

**No LLM provider API key was configured anywhere in this system**
(`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`GOOGLE_API_KEY` all empty) when
this step began - a genuine blocker, not something fixable in code.
Resolved mid-step: Mohit has OpenRouter and NVIDIA NIM keys instead -
`configs/litellm_config.yaml`'s `premium`/`cheap` routes were repointed
to `openrouter/anthropic/claude-sonnet-4.5` and
`nvidia_nim/meta/llama-3.1-8b-instruct` respectively. **Waiting on
`OPENROUTER_API_KEY`/`NVIDIA_NIM_API_KEY` to be added to `.env`** before
any of the following can be live-tested:
- LiteLLM itself successfully routing a real completion.
- A live model call actually confirming it cannot invoke a disabled
  toolset (currently only confirmed at the config level, not behaviorally).
- Any acceptance test that needs the agent to actually think (e, i, and
  the full form of f/g/h).

## Regression check

No `tests/unit/` changes in this step (infrastructure/config only) -
last confirmed suite state remains 383 passed, 1 skipped (Step 2).
