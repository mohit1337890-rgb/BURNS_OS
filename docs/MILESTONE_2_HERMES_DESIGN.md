# Milestone 2: Hermes Agent Container - Design (2026-09-29)

**Status: design only. No Hermes code exists yet. Milestone 2 implementation
still requires Mohit's explicit approval, per his own standing instruction -
nothing in this document authorizes writing Hermes itself.**

This supersedes the informal "Hermes will use the Gateway MCP" note in the
original build prompt with a concrete design, updated for the 2026-09-29
decision that the **Dashboard is the interface**, not Telegram: the
Command box (`dashboard/templates/command.html`, currently log-only) is
what will actually dispatch to Hermes once this milestone is approved.

## Goals

1. The Dashboard's Command box becomes real: text the owner types there
   can actually cause Hermes to do something, not just log a request.
2. Every external/Tier-2/3 effect Hermes causes still goes through the
   exact same chokepoint (`gateway.core_execute.request_action`) every
   other caller does - Hermes gets no special path, no bypass, no new
   privilege.
3. Approvals for anything Hermes triggers happen via the Dashboard
   (`core/approval_channels.py::DashboardChannel`), already built and
   live-tested (2026-09-29) - Tier-3 still requires a fresh TOTP code at
   decision time, exactly as it does for a human-initiated action.
4. Hermes holds no real external-system credentials - see "The credential
   model" below for the precise, honest claim (not a hand-wave of
   "zero" that turns out to hide something).

## Architecture

```
   Owner's phone/laptop (Tailscale)
            |
            v  HTTPS (session cookie, CSRF, TOTP)
      +-----------+
      | Dashboard |  (127.0.0.1-bound, dashboard/app.py - already live)
      +-----+-----+
            |
            | Command box POST -> internal Docker network only,
            | never the host, never the internet
            v
      +-----------+        MCP over streamable-http           +-----------+
      |  Hermes   |  <-------------------------------------->  |  Gateway  |
      | container |     (HERMES_MCP_TOKEN - the ONE thing      | (existing)|
      +-----+-----+      Hermes needs to prove it's Hermes)    +-----+-----+
            |                                                        |
            | LLM calls (LiteLLM virtual key,                        | already
            | budget-capped, not the master key)                    | does this
            v                                                        v
      +-----------+                                         Postgres / Telegram /
      |  LiteLLM  |                                         SMTP / git / MT5 /
      +-----------+                                         everything else -
                                                              all still ONLY
                                                              reachable from
                                                              inside the Gateway
```

Hermes has **no direct network path** to Postgres, Telegram, SMTP, git,
or MT5 - none of those hostnames/ports are reachable from the Hermes
container at all (enforced the same way `litellm_app` having zero grants
on `burns_os` is enforced - not just "Hermes doesn't have the password",
but the connection itself has nowhere to go). The Gateway remains the
literal, structural chokepoint, not just a policy convention Hermes is
trusted to respect.

## The credential model - precise, not "zero" as a slogan

Mohit's instruction was "Hermes container still has ZERO secrets." The
honest version of that claim, after actually designing the thing:

**Hermes needs exactly two credentials to function at all**, both
deliberately narrow, revocable, and unable to reach anything beyond their
one purpose:

1. **`HERMES_MCP_TOKEN`** - authenticates Hermes to the Gateway's MCP
   endpoint (see "MCP transport" below). This is NOT `GATEWAY_INTERNAL_TOKEN`
   (the Dashboard/other internal callers' own token) - a separate,
   independently rotatable/revocable credential, specifically so revoking
   Hermes's access is a one-line change that doesn't also break the
   Dashboard or anything else. Knowing this token lets Hermes call
   `execute_action`/`get_approval_status`/`verify_ledger` - the exact
   same three tools every other MCP caller has always had (see
   `gateway/mcp_server.py`) - nothing more. It does not grant database
   access, does not grant any plugin credential, does not bypass policy/
   tier/budget/DLP/hard-block checks (those live in `gateway.core_execute`,
   which every MCP tool call still goes through).
2. **A LiteLLM virtual key**, not `LITELLM_MASTER_KEY`. Issued via
   LiteLLM's own key-management API with its own spend cap (on top of,
   not instead of, `core/budget_guard.py`'s existing Ledger-based caps -
   two independent limits, same "a single mechanism is a single point of
   failure" reasoning as the ledger anchor). This key can only call the
   `premium`/`cheap`/`local` model routes already defined in
   `configs/litellm_config.yaml`; it cannot mint new keys, change
   LiteLLM's own config, or see other keys' spend.

**What Hermes explicitly never gets**: `BURNS_APP_DB_PASSWORD`,
`TELEGRAM_BOT_TOKEN`, `SMTP_PASSWORD`, `GATEWAY_INTERNAL_TOKEN` (the
Dashboard's own token), `GATEWAY_INTERNAL_TOKEN`'s HTTP `/execute` path
at all (Hermes only ever speaks MCP, never the REST API), `MT5_*`,
`COOLIFY_API_TOKEN`, `POSTGRES_USER`/`POSTGRES_PASSWORD` (admin), or any
`BACKUP_*` value. If a future reviewer greps Hermes's own container
environment and finds anything beyond the two credentials above, that's
a bug against this design, not an acceptable variance.

## MCP transport: streamable-http, not stdio

`gateway/mcp_server.py` currently defaults to stdio transport (see
`MCPServer.run()`, confirmed live 2026-09-29 by spawning it as a real
subprocess) - correct for a caller that can spawn the Gateway process
directly, which was the only scenario that existed before Hermes. Hermes
is a **separate container** with no ability to spawn anything inside the
Gateway's container, so stdio doesn't apply here.

For Milestone 2, the Gateway needs a second entry point -
`gateway/mcp_server.py`'s `build_server()` already takes the transport as
a parameter (`MCPServer.run(transport=...)`, confirmed supports
`'stdio' | 'sse' | 'streamable-http'`) - running the SAME `build_server()`
output over `streamable-http` on an internal-only port (not published to
the host, same pattern as `postgres`/`litellm` today) is a small, additive
change: no new tools, no new logic, just a second way to reach the
existing three tools, authenticated by `HERMES_MCP_TOKEN` instead of
process-spawn trust. This keeps `gateway/app.py`'s existing HTTP REST API
(`/execute` etc., `GATEWAY_INTERNAL_TOKEN`-authenticated) completely
separate and unchanged - Hermes never touches it.

**Hard requirement, checked live 2026-09-29 against the installed `mcp`
SDK, not assumed**: `MCPServer.run_streamable_http_async()` has **no
built-in caller authentication at all** - it just serves a plain Starlette
app over uvicorn (`transport_security` only covers Host-header/DNS-
rebinding protection, not "who is calling"). Today, nothing invokes this
code path anywhere - `gateway/mcp_server.py::main()` always runs `stdio`
(hardcoded, no transport argument even exists yet), and no
docker-compose service exposes an MCP HTTP port, so there is no live gap
right now. But this means **the `HERMES_MCP_TOKEN` check cannot be an
afterthought** - whoever implements this must wrap the ASGI app
`streamable_http_app()` returns with an authentication middleware (reject
any request missing/mismatching `HERMES_MCP_TOKEN` before it reaches any
tool) as part of the SAME change that first turns this transport on, not
a follow-up. A first version of this endpoint that omits that check would
be a real, exploitable hole (any container reachable on that internal
Docker network could call `execute_action` with no credential at all),
not a theoretical one - this document treats it as a blocking
requirement for Milestone 2's implementation, not one of the open
questions below.

## Command box -> Hermes flow

1. Owner types a command in the Dashboard (`POST /command`, already
   CSRF-protected, session-authenticated).
2. Today (live now): this only calls `core.ledger.append_entry()` (action
   `dashboard_command`, tier 1) and stores a `CommandRequest` row - "LOGGED:
   not executed - Milestone 2 (Hermes) is not yet approved."
3. **Once Milestone 2 is approved**, that same route additionally makes
   an internal HTTP call to Hermes (`http://hermes:8100/command` on the
   Docker-internal network only) with the command text and the
   `CommandRequest.id`. Hermes is never reachable from outside the Docker
   network - no host port mapping, matching the Dashboard's own
   127.0.0.1-only posture.
4. Hermes runs its own reasoning loop (LiteLLM calls via its virtual key)
   and, for anything that needs a real effect, calls
   `execute_action` over the Gateway's MCP endpoint - which runs the
   *exact* same `gateway.core_execute.request_action` chokepoint as
   today: hard-block check, budget check, Tier 0/1 auto-executes, Tier
   2/3 creates a real `ApprovalRequest` no differently than an HTTP
   `/execute` call would.
5. A Tier-2/3 approval Hermes triggers shows up on the Dashboard's
   `/approvals` page exactly like any other - `decided_by` records that
   it originated from a Hermes-initiated command (via `CommandRequest.id`
   in the params/summary), but the approval/authorization mechanics
   (`DashboardChannel`, fresh TOTP for Tier 3, CSRF) are completely
   unchanged. Hermes cannot self-approve anything - it has no dashboard
   session and no TOTP secret; `DashboardChannel.authorize_decision()`
   would refuse it exactly as it refuses any other unauthenticated caller.
6. Hermes reports progress/completion the same way it does everything
   else that touches persistent state - by calling Gateway MCP tools
   (a natural extension: a `log_progress`-style tool, or reusing
   `execute_action` with a Tier-0 logging action) - never by writing to
   Postgres directly (it has no credential that could).
7. The Command page gains a "status" column once this lands, driven by
   ledger rows linked back to `CommandRequest.ledger_entry_id` and any
   follow-up entries Hermes's progress calls produce - no new UI paradigm,
   the Ledger is already the single source of truth for "what happened."

## Safety argument: why a compromised or hallucinating Hermes is contained

This is the same argument that's applied to every caller since Milestone
1, restated for Hermes specifically:

- **Hard-block actions** (`reveal_secrets`, `modify_policies`, etc.) are
  refused by `core.policy_engine.classify()` before any tier logic runs,
  regardless of who's asking - a jailbroken/hallucinating Hermes asking
  for one is refused identically to a malicious human doing the same over
  the HTTP API.
- **Tier 2/3 actions require a human decision via the Dashboard** - Hermes
  requesting a Tier-3 `place_trade` creates a pending approval and stops
  there; nothing executes until Mohit approves it with a fresh TOTP code,
  same as today.
- **DLP** scans outgoing content regardless of caller - a Hermes-drafted
  email containing a leaked secret is refused the same way a
  human-triggered one is (`core/dlp.py`, unchanged).
- **Budget caps** (`core/budget_guard.py` + Hermes's own separate LiteLLM
  spend cap) bound the financial blast radius of a runaway reasoning loop
  independently of whether any individual action looks "safe."
- **The Ledger** records every MCP call Hermes makes, hash-chained and
  anchored exactly like every other action - a compromised Hermes leaves
  the same unavoidable audit trail anything else does.

None of this is new design work - it's the existing Milestone 1
chokepoint correctly having no special case for "the caller is an LLM."

## Open questions for Mohit before implementation

1. **`HERMES_MCP_TOKEN` rotation policy** - manual, or should the Gateway
   support rotating it without a restart?
2. **LiteLLM virtual key budget** for Hermes specifically - a number, and
   whether it should differ from `DEFAULT_MISSION_BUDGET_USD`.
3. **Command box UX once Hermes is live** - synchronous (the page waits
   for an initial Hermes response) or fire-and-forget with a status poll/
   refresh? (Recommendation: fire-and-forget - Hermes's own reasoning
   loop could take much longer than an HTTP request should reasonably
   block for.)
4. **Does Hermes get its own systemd/Docker healthcheck and restart
   policy**, and should a Hermes crash mid-command leave the
   `CommandRequest` in an explicit "unknown" state (mirroring
   `core/reconciliation.py`'s `UNKNOWN_OUTCOME` pattern for exactly this
   class of problem)? (Recommendation: yes - same reasoning as the
   `EXECUTING` marker: a process can always die mid-task, and pretending
   otherwise is how KNOWN_LIMITS bug #11 happened in the first place.)
5. Should Hermes be a single long-running container, or spawned per-command
   (a fresh container per Command box submission, torn down after)? The
   latter is more expensive but gives a stronger "no accumulated state,
   no session persistence, nothing to compromise between commands"
   guarantee - worth deciding deliberately rather than defaulting to
   "however Milestone 1's containers happen to work."

## Non-goals for this document

No Hermes code, no new Gateway MCP tools beyond exposing the existing
three over a second transport, no changes to `core/approval_channels.py`
or `core/approvals.py` (Hermes is just another caller of what's already
there), no decision on which agent framework/loop Hermes runs internally
(LangGraph, a custom loop, the Claude Agent SDK, etc.) - that's an
implementation detail for whenever Milestone 2 is actually approved.
