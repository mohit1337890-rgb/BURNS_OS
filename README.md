# Burns OS

Internal AI operating system for Burns Worldwide (Mohit, Founder & CEO). Full
design/spec lives in the original build prompt this repo was started from -
see `docs/ARCHITECTURE.md` for the summarized version. **This README states
only what has actually been built and tested, with evidence.** Nothing here
is marked "done" without a passing test - see `docs/KNOWN_LIMITS.md` for the
living, honest, currently-open-vs-closed list (security gaps, blocked items,
real bugs found and fixed - including several found only by running this
live against real Docker/Postgres on 2026-09-28/29, not by code review).
See `docs/incidents/2026-09-litellm-table-drop.md` for the one real
incident this live testing caused (litellm briefly shared, and its
schema-sync dropped, Burns OS's own tables) and how it was closed.

## What's real right now (Milestone 1, in progress)

**Telegram is postponed (2026-09-29); the Web Dashboard is now the primary
approval/control channel** - see `docs/DASHBOARD.md`. Approvals are
channel-agnostic (`core/approval_channels.py`'s `ApprovalChannel`
interface): `DashboardChannel` is live and wired in, `TelegramChannel`
still exists and is fully tested but nothing currently calls it. Every
Tier-2/3 acceptance test that used to go through the Telegram bot has been
re-run live through the Dashboard with a real Chromium browser
(Playwright, `make test-e2e`, screenshots in `tests/e2e/evidence/`).

**328 passing pytest tests, 1 intentionally skipped** plus **11
Playwright end-to-end tests** against the live Dashboard+Gateway, the
DB-touching majority of the unit tests running against BOTH SQLite
(`python -m pytest tests/unit/ -v`, no Docker required) AND a real
Postgres instance in the same run (`tests/unit/conftest.py`'s
parametrized `session` fixture - see `docs/KNOWN_LIMITS.md` item 6), plus
**12 dedicated live-Postgres-only tests** (`make test-pg`) for things SQLite
structurally can't prove, including that `burns_app` has zero DDL rights,
that `litellm_app`/`burns_app` can't reach each other's or the archived
incident database, and that ledger verification genuinely fails (not just
"looks ok") when the ledger table itself is missing. **All originally-
identified security gaps are closed**, each with its own test against a
real Postgres instance - not just SQLite, and not just written-but-
unverified. The full Docker Compose stack (`postgres`, `langfuse`,
`litellm`, `gateway`, `dashboard`, `approvals_bot`, and a `scheduler`
service) has been run live, with real evidence, per
`docs/KNOWN_LIMITS.md`'s STEP 1-3 results table.

| Layer | Modules | What it does |
|---|---|---|
| **Policy** | `policies/policy.yaml`, `core/policy_engine.py` | Declarative action->tier rules (0=read, 1=sandbox, 2=external+approval, 3=money/irreversible+board review+cooling period), hard-block list, per-plugin enable/required-env, git_push allow-list. |
| **Ledger** | `core/ledger.py`, `core/ledger_anchor.py` | Append-only, SHA-256 hash-chained audit log + an external anchor (file today, Telegram once a token exists) that catches tail-truncation `verify_chain()` alone can't. |
| **Budget** | `core/budget_guard.py`, `core/llm_spend.py` | Global monthly + per-mission caps, computed fresh from the Ledger; real LLM token spend counts toward it. |
| **Approvals** | `core/approvals.py`, `core/approval_channels.py` | Tier 2/3 state machine, SHA-256 params binding (a changed param after approval requires a new one), atomic single-execution claim (a double-tap/replay can't run a plugin twice), 24h expiry, Tier-3 cooling period. `decide_approval()` always independently re-verifies the caller via a channel-agnostic `ApprovalChannel` (`DashboardChannel` live; `TelegramChannel` tested, not wired in) - never trusts a pre-computed "is this the owner" flag from the caller. |
| **Dashboard** | `dashboard/` (`app.py`, `auth.py`, `models.py`) | The primary approval/control UI (FastAPI + Jinja2/HTMX), 127.0.0.1-only by default. Single owner account: argon2 password + TOTP 2FA, with a second FRESH TOTP code required at decision time for Tier-3 approvals. CSRF on every POST, HttpOnly/SameSite=Strict/Secure cookies, 30-min idle timeout, login lockout. Pages: Home, Approvals, Alerts, Ledger (search+verify), Missions, Budgets, Command (logs only until Milestone 2). See `docs/DASHBOARD.md`. |
| **Scheduler** | `core/scheduler.py` | Executes due Tier-3 approvals post-cooling; expires stale PENDING approvals. |
| **DLP** | `core/dlp.py` | Refuses to send outgoing content (email/message params) containing a likely secret. |
| **Missions** | `core/missions.py` | Spec-Kit state machine with a REAL owner-approval binding (`Mission.spec_approval_id` -> an actual `ApprovalRequest`, not just a status string). |
| **Config** | `core/app_config.py` | Core vars fail loudly at startup if missing; plugin vars are required only when that plugin is enabled in `policy.yaml` - a disabled/misconfigured plugin refuses to execute instead of crashing the Gateway. |
| **Gateway** | `gateway/core_execute.py`, `gateway/registry_bootstrap.py`, `gateway/plugins/` | The chokepoint itself, wired to real plugins (Telegram, SMTP email, git_push) and honest stubs (Coolify deploy, MT5 trade). Crash-safe: an `EXECUTING` ledger marker is written before a plugin ever runs (see Reconciliation below). |
| **Gateway HTTP** | `gateway/app.py` | FastAPI server, local API-key auth, `/execute` `/approvals/{id}` `/ledger/verify` `/health`. Tested with `TestClient` AND live over real HTTP (`curl`) against the running Docker container. |
| **Gateway MCP** | `gateway/mcp_server.py` | The same actions as MCP tools, for a future Hermes container. Has a real `python -m gateway.mcp_server` entry point now, tested by spawning it as a real subprocess and talking to it over real stdio (not just `list_tools()`/`call_tool()` direct calls). |
| **Approvals Bot** | `approvals_bot/` | Approval Card formatting, owner-only decision handling, real polling-loop logic (`poller.py`) and a real `python -m approvals_bot` entry point - runs live in Docker against a placeholder token (polls, fails auth gracefully, retries); **still hasn't sent/received a real Telegram message** (real bot token still pending). |
| **Reconciliation** | `core/reconciliation.py`, `scripts/admin_reconcile_approval.py` | Recovers from an approval left inconsistent by a process death: an automatic scheduler job finds a stale `EXECUTING` marker with no result and flips it to `UNKNOWN_OUTCOME` (alerting); a separate, manual, audited admin script handles the one pre-existing case that predates the marker. |
| **Scheduled process** | `scripts/scheduler_loop.py` (docker-compose `scheduler` service) | The real, running loop for `core/scheduler.py` (Tier-3 post-cooling execution), `core/ledger_anchor.py` (hourly anchor write) and `core/reconciliation.py` - all three were previously "pure logic, no loop of its own." Runs live in Docker now. |
| **Backups** | `scripts/backup.py`, `scripts/restore.py` | `make backup`: asymmetric-GPG-encrypted `pg_dump` + anchor file, 14-day retention - this machine only ever holds the public key, the private key lives offline. `make restore`: checksummed, decrypts, restores into a fresh test DB, verifies the chain - never touches the live DB. Tested live end-to-end including a genuine "cannot decrypt without the offline private key" proof (`docs/BACKUP_RECOVERY.md`). Offsite copy still pending Mohit's rclone remote choice; not yet wired to a real nightly scheduler. |

Run the tests yourself:

```bash
cd burns_os_system
.venv\Scripts\python.exe -m pytest tests/unit/ -v      # SQLite + Postgres (if reachable) in one run
make test-pg                                             # live-Postgres-only tests (needs `make up` first)
```

## How it actually works

1. A caller (an agent, eventually via Hermes MCP; today: a direct Python call, an HTTP `POST /execute`, or a real MCP `execute_action` tool call over stdio - all three go through the exact same `gateway.core_execute.request_action`) asks for an action.
2. **Hard-block check first** - refused + logged immediately, no tier logic can override it.
3. **Budget check** - refused if the mission or global monthly cap is already over its stop threshold (real LLM spend counts here too).
4. **Tier 0/1** - executes in-sandbox immediately, logged only.
5. **Tier 2/3** - creates an `ApprovalRequest` (with a SHA-256 hash of its own params) and a `PENDING_APPROVAL` ledger entry. **Nothing external has happened yet.**
6. A human decides via the Dashboard (`dashboard/app.py`, session + CSRF + fresh-TOTP-for-Tier-3, independently re-verified by `core.approval_channels.DashboardChannel` - not just trusted from the caller) or the Gateway API directly. If approved, and - for Tier 3 - once the cooling period has elapsed, `execute_approved_action` re-checks the params hash, scans the outgoing content for secrets (DLP), atomically claims the one-time execution right, writes an `EXECUTING` marker, THEN runs the real plugin and logs the final result + cost.
7. `core/scheduler.py::run_due_tier3_executions` is what actually re-tries a Tier-3 action once its cooling period elapses, if nothing else already did - and it's a real running process now (`scripts/scheduler_loop.py`, the docker-compose `scheduler` service), not just tested logic.
8. If the process dies between claiming and logging a result, `core/reconciliation.py`'s scheduled pass finds the orphaned `EXECUTING` marker and flips that approval to `UNKNOWN_OUTCOME` for a human to check - proved with a real `os._exit()` mid-plugin-call.
9. Every step lands in the same hash-chained, concurrency-safe (DB-enforced `UNIQUE` on the hash chain) Ledger; `core.ledger.verify_chain()` + `core.ledger_anchor.verify_against_anchors()` together detect both in-place tampering and tail truncation - the anchor half runs on a real hourly schedule now too.
10. The Gateway's own Postgres connection (`burns_app`) is a deliberately powerless role - no superuser, no `UPDATE`/`DELETE`/`TRUNCATE` on the Ledger, can't even attempt the `session_replication_role` trigger-bypass trick. Migrations run separately, as a genuinely different admin role, never baked into the Gateway's own environment.

## What's NOT built yet

See `docs/KNOWN_LIMITS.md` for the full, current list. Headline items:

- **Milestone 2 (Hermes Agent container) is explicitly NOT approved yet** - Mohit's own instruction. See `docs/MILESTONE_2_HERMES_DESIGN.md` for the design (no code) - Command box -> Hermes -> Gateway MCP, Hermes gets exactly two narrow credentials, zero direct access to Postgres/Telegram/SMTP/etc.
- **Real Telegram is postponed** (Mohit's own 2026-09-29 decision, not a blocker) - `TelegramChannel` and the Approvals Bot are fully built and tested but not currently wired into anything running; the Dashboard is the live approval channel instead.
- Offsite backup sync (rclone) - pending Mohit choosing a remote, see `docs/BACKUP_RECOVERY.md`.
- Squad Builder, Evals, Monitor & Self-Heal, Daily/Weekly reports, all 10 departments' actual tools/roles beyond the generic Gateway plugins.

## Repo layout

See `docs/ARCHITECTURE.md` (to be written) for the full intended layout from the original build prompt. Currently populated: `core/`, `gateway/`, `dashboard/`, `approvals_bot/`, `policies/`, `configs/`, `deploy/`, `db/migrations/`, `scripts/`, `tests/unit/`, `tests/postgres/`, `tests/e2e/`. Everything else under `agents/`, `roles/`, `skills/`, `evals/`, `hermes/`, `sandbox/` is an empty directory reserved for later milestones (`hermes/` specifically waits on Milestone 2 approval - see `docs/MILESTONE_2_HERMES_DESIGN.md`).
