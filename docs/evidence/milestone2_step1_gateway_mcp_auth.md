# Milestone 2, Step 1 — Gateway MCP streamable-http + auth + per-agent roles

Live evidence, 2026-09-29. Real docker-compose `gateway-mcp` service, real
MCP protocol client (`mcp.client.streamable_http` + `mcp.ClientSession`),
run from inside another container on the same internal docker network
(the service publishes no host port at all).

## Network reachability

- From the Windows host: `curl http://127.0.0.1:8091/mcp` → **connection
  refused** (curl exit code 7) - confirmed not published to the host.
- From inside `gateway-1` (another container, same compose network):
  `http://gateway-mcp:8091/mcp` reachable - confirmed internal-only.

## Auth: missing/wrong token → 401 (acceptance test c, first half)

```
missing-token status: 401
wrong-token status: 401
```

## Real MCP protocol round-trip with correct per-agent tokens

```
=== researcher token: web_search (tier 0) -> should EXECUTE ===
{
  "status": "executed",
  "detail": "Tier 0/1 action executed in-sandbox.",
  "ledger_entry_id": 106,
  "approval_id": null
}
=== researcher token: send_email (tier 2) -> should be REFUSED (role_not_allowed) ===
{
  "status": "refused_role_not_allowed",
  "detail": "Role 'researcher' may not call 'send_email': Tier 2 exceeds role 'researcher''s max_tier (0).",
  "ledger_entry_id": 107,
  "approval_id": null
}
=== chief token: web_search (tier 0, forbidden for chief) -> should be REFUSED (role_not_allowed) ===
{
  "status": "refused_role_not_allowed",
  "detail": "Role 'chief_of_staff' may not call 'web_search': 'web_search' is on role 'chief_of_staff''s forbidden_actions list.",
  "ledger_entry_id": 108,
  "approval_id": null
}
=== chief token: place_trade (tier 3) -> should be pending_approval (chief CAN request tier 3) ===
{
  "status": "pending_approval",
  "detail": "Tier 3 action requires approval (id=593e1460-c968-41e2-af56-95d0ef3c466f).",
  "ledger_entry_id": 109,
  "approval_id": "593e1460-c968-41e2-af56-95d0ef3c466f"
}
=== SECURITY TEST: chief token but params claim agent_role=researcher -> must still enforce chief_of_staff (refused for web_search) ===
{
  "status": "refused_role_not_allowed",
  "detail": "Role 'chief_of_staff' may not call 'web_search': 'web_search' is on role 'chief_of_staff''s forbidden_actions list.",
  "ledger_entry_id": 110,
  "approval_id": null
}
```

## What this proves (mapping to the approved conditions/acceptance tests)

- **Condition 1 (defense in depth) - the auth half**: a request with no
  token, or the wrong token, never reaches a tool at all (401 from the
  middleware, before the MCP session even initializes for the wrong-token
  case, and before ANY tool call for the missing-token case).
- **Design doc acceptance test (c)**: MCP endpoint rejects missing/wrong
  token (401) - confirmed. "A token from agent A cannot act as agent B" -
  confirmed by the final test above: the chief token's own tool call
  explicitly set `"agent_role": "researcher"` in its JSON params, and the
  Gateway still enforced `chief_of_staff` (refused for `web_search`,
  which only `chief_of_staff`'s forbidden_actions list blocks) - proving
  `agent_role` is derived from WHICH token authenticated the request,
  never from the request's own claimed value.
- **Design doc acceptance test (d)**: the researcher role is refused for
  `send_email` (a Tier-2 action) - confirmed, `REFUSED_ROLE_NOT_ALLOWED`,
  logged to the Ledger (`ledger_entry_id: 107`).
- **The reverse split** (not originally a separate acceptance test, but
  part of Mohit's condition 4's intent): chief_of_staff is refused for
  `web_search`/`web_fetch` - it can request up to Tier 3 (proven via
  `place_trade` creating a real pending approval) but can never read raw
  web content directly.

## Not yet covered by this evidence (later steps)

- Condition 1(b) - "even from a shell inside the container, the
  internet/Postgres/SMTP are unreachable" - this is about the Hermes
  containers themselves (Steps 3-4), not `gateway-mcp` (which legitimately
  needs Postgres access for the Ledger). Deferred to the Step 3/4 evidence
  file.
- Acceptance tests a, b, e-i - later steps.

## Regression check

Full unit suite after this step's code changes: **352 passed, 1 skipped**
(up from 328 before this round - +24 new tests: `tests/unit/test_mcp_auth.py`
(10), plus new role-limit/contextvar-precedence tests in
`test_policy_engine.py`, `test_core_execute.py`, `test_mcp_server.py`).
