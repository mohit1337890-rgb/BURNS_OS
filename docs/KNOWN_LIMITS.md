# Known Limits (Milestone 1, live-tested 2026-09-28/29)

Updated honestly as the build progresses - nothing here gets claimed "done"
until it has a passing test proving it, and every real bug found during
live testing is recorded here, not quietly fixed and forgotten.

## Current status

- **320 passing pytest tests, 1 intentionally skipped** (SQLite-only)
  against SQLite (`tests/unit/`, no Docker required), with the
  DB-touching majority of them now ALSO running against a real Postgres
  instance via a parametrized `session` fixture (`tests/unit/conftest.py`)
  - see "Item 6" below. Confirmed live 2026-09-29 (`.venv/Scripts/python.exe
  -m pytest tests/unit/ -q` -> `320 passed, 1 skipped in 273.35s`), after
  the full Dashboard/channel-agnostic round (+89 tests: `core/
  approval_channels.py`, `dashboard/auth.py`, `dashboard/app.py`).
- **12 dedicated live-Postgres-only tests** (`tests/postgres/`, `make
  test-pg`) proving things SQLite structurally can't: the append-only
  trigger, a genuine two-thread concurrency race, the burns_app role's
  restrictions, TRUNCATE being blocked, litellm_app's isolation,
  burns_app's lack of DDL rights, and (added 2026-09-29) burns_app being
  refused a connection to the archived incident database. 3 of the 12
  need admin (`burns_admin`)/`litellm_app` creds injected via `-e`
  (skip cleanly otherwise) - see each test's own docstring for the exact
  command.
- **Docker Compose stack runs for real**: postgres, langfuse(+its own db),
  litellm(+its own separate db), gateway, approvals_bot, and a new
  `scheduler` service - all healthy, all live-tested (see below).
- **9 of the original 9 identified security gaps are closed**, plus a
  10th found and closed during Postgres bring-up, plus several more real
  bugs found only by testing live (see "Bugs found while going live"
  below) - every one has its own regression test.

## Blocking / pending inputs

- **`TELEGRAM_BOT_TOKEN`/`TELEGRAM_OWNER_CHAT_ID` are still placeholder
  values**, not real ones. Everything that doesn't need a real Telegram
  round-trip has been live-tested (Tier-0/1 auto-exec, hard-block refusal,
  DLP refusal, Tier-3 cooling+scheduler auto-exec, the concurrency/
  idempotency guard, the append-only trigger, the ledger anchor). Blocked
  until a real token exists: live acceptance tests B (Approve/Reject card)
  and E (wrong-account rejection) in full, the Telegram-alert half of D
  and G, and `TelegramAnchorSink`.
- **Milestone 2 (Hermes Agent container) remains explicitly not approved** -
  Mohit's own instruction.

## STEP 1-3 live acceptance test results (2026-09-28/29, real Docker + Postgres)

| # | Test | Result |
|---|------|--------|
| A | Tier-1 auto-exec, no approval/card | **PASS** (live HTTP) |
| B | Tier-2 card -> Reject -> Approve -> send | **BLOCKED** on real Telegram |
| C | Tier-3 stub -> cooling -> scheduler auto-exec | **PASS** (live; found+fixed 2 real bugs along the way - see below) |
| D | Hard-block refuse + alert | **PASS** (refusal) / alert **BLOCKED** on real Telegram |
| E | Wrong-account button press -> rejected | **BLOCKED** - needs a second real Telegram account |
| F | Double-tap Approve -> executes once | **PASS** - real two-thread Postgres concurrency test (the mechanism Telegram itself would hit) |
| G | DLP fake key -> refuse + alert | **PASS** (refusal) / alert **BLOCKED** on real Telegram |

## Bugs found while going live (all fixed, each with a regression test)

Nothing here was found by code review - every one of these only surfaced
by actually running the thing against real Docker/Postgres/processes, per
Mohit's own instruction ("run live, show evidence").

**Infra/build bugs** (the stack couldn't come up at all without these):
1. `requirements.txt` had no Postgres driver at all.
2. `deploy/Dockerfile.gateway` never copied `alembic.ini`/`db/`/`tests/`/
   `pytest.ini`/`scripts/` - migrations and tests couldn't run in the
   container.
3. `gateway/app.py`'s startup called `ledger.init_db()`/`create_all()`
   against real Postgres, racing Alembic's own `CREATE TABLE` - removed;
   schema ownership belongs to Alembic only against real Postgres.
4. SQLAlchemy's bare `postgresql://` scheme resolved to `psycopg` (v3, not
   installed) instead of `psycopg2` - now explicit `postgresql+psycopg2://`.
5. `docker-compose.yml`'s gateway/litellm healthchecks used `wget`, absent
   from both images - always "unhealthy" despite the app being fine.
6. langfuse: Docker's auto-set `HOSTNAME` env var made the Next.js
   standalone server bind to the container's own IP, not `0.0.0.0` -
   unreachable via localhost/127.0.0.1 (its own healthcheck included).
7. langfuse's healthcheck used bare `localhost`, which resolved IPv6 first
   with nothing listening there - explicit `127.0.0.1` fixed it.
8. `Makefile`'s `test-pg` ran `pytest tests/ -m postgres`, which tried to
   collect `tests/unit/test_approvals_bot*.py` inside the gateway
   container (no `approvals_bot/` package there) - scoped to
   `tests/postgres/` instead.

**Security bugs:**
9. `core.approvals.decide_approval()`'s docstring claimed `owner_chat_id`
   was independently re-verified as "a second gate" against the
   configured `TELEGRAM_OWNER_CHAT_ID` - the check never actually existed
   in the code (`NotOwnerError` was defined but never raised anywhere).
   Only `approvals_bot.bot.handle_callback_query`'s own `from_chat_id`
   check protected a real decision - one layer, not the two documented.
   Fixed: the real check now lives in `decide_approval()` itself.
10. **Postgres app role was a superuser.** `POSTGRES_USER` both bootstraps
    the Postgres container (making it the superuser, unavoidably - that's
    how the official image works) AND was the SAME role the app itself
    connected as. Migration 0002's `REVOKE UPDATE/DELETE` was therefore a
    no-op (superusers bypass ACL checks entirely), and even the append-only
    TRIGGER could be bypassed via `SET session_replication_role = replica`
    (superuser-only, but that's exactly the point). Proved live in a
    disposable test DB before the fix. **Closed**: migration 0003 creates
    `burns_app` (NOSUPERUSER, only SELECT+INSERT on `ledger`, full CRUD on
    `approvals`/`missions`) - gateway/approvals_bot/scheduler all connect
    as `burns_app` now; migrations run as the admin role
    (`burns_admin` - the renamed original superuser), injected via `-e`
    only for that one transient command, never baked into the long-running
    containers' own environment.
11. **`execute_approved_action()` had a crash-unsafe window.**
    `mark_executing()` irreversibly flips an approval to EXECUTED (by
    design - never auto-retried, since a Tier-3 action might have already
    reached a real external system) - but the plugin call and the ledger
    write recording what happened came AFTER that, with nothing in
    between. Found live during test C: my own new
    `scripts/scheduler_loop.py` forgot to call `registry_bootstrap.bootstrap()`,
    so `plugins.get()` raised `NotImplementedError` - and the approval was
    left permanently EXECUTED with **zero ledger record**, a silent,
    unrecorded action. Closed in two layers: (a) an `EXECUTING` ledger
    marker, written and committed BEFORE the plugin is even called, so a
    genuine process kill (proved with a real `os._exit()` in a real
    subprocess, `tests/unit/test_reconciliation.py`) still leaves a
    recoverable trace; (b) a try/except around the plugin call itself for
    ordinary Python exceptions, logging a `FAILED` entry immediately
    rather than waiting for the reconciliation job. A new
    `core/reconciliation.py` module's `run_reconciliation_pass()` (wired
    into `scripts/scheduler_loop.py`, runs every `SCHEDULER_INTERVAL_SECONDS`)
    finds any `EXECUTING` marker with no later result after
    `RECONCILIATION_STALE_MINUTES` (default 15) and flips that approval to
    `UNKNOWN_OUTCOME`, alerting (stdout today - Telegram once a real token
    exists) that a human must verify what actually happened.
12. **The real approval this exact bug produced live**
    (`d0521604-3749-4cfc-b3f5-0dafcc3681bf`, a Tier-3 `place_trade` stub -
    no real money/action was ever involved) was reconciled via a new
    audited admin script, `scripts/admin_reconcile_approval.py`
    (`core.reconciliation.reconcile_stuck_approval()`): does NOT edit or
    delete any existing ledger row (append-only, untouched) - appends a
    new `RECONCILIED_FAILED` entry referencing the approval and moves the
    approval to a new `FAILED_RECONCILED` status. This was verified live
    (ledger chain still `ok=True` afterward) - **the actual evidence of
    that reconciliation was subsequently destroyed by bug #13 below**,
    an unrelated incident that happened minutes later while wiring up
    role separation; the mechanism itself remains fully tested (9 tests,
    `tests/unit/test_reconciliation.py`) and was proven live once.
13. **litellm shared the same Postgres database as Burns OS's own tables**
    (`docker-compose.yml`'s `DATABASE_URL` pointed litellm at `${POSTGRES_DB}`,
    i.e. `burns_os`, same as `ledger`/`approvals`/`missions`). litellm's
    own Prisma schema-sync ("db push") treats the whole database's public
    schema as its own and silently **drops any table it doesn't
    recognize** - it dropped `ledger` (26 rows, including bug #12's
    reconciliation evidence), `approvals`, and `alembic_version` outright
    the moment litellm restarted. **Closed**: litellm now has its own
    dedicated `litellm_db` database on the same Postgres server -
    Postgres databases (not just schemas) are fully isolated from each
    other, so this specific failure mode is now structurally impossible,
    not just avoided by convention.
14. **`core.ledger.append_entry()`'s hash-chain write wasn't atomic.**
    Found immediately after fixing #13, via the SAME two-thread
    concurrency test that proves gap #5/#10: "read the latest hash, then
    insert" is two separate statements - two genuinely concurrent callers
    (exactly what that test constructs, and exactly what a real
    double-tap or overlapping scheduler tick can produce) could both read
    the same "latest" row before either committed, producing two ledger
    rows with the SAME `prev_hash` - a forked chain `verify_chain()`
    correctly detects, but only after the fact. **Closed**: a DB-enforced
    `UNIQUE` constraint on `prev_hash` (migration 0004, and on the
    SQLAlchemy model itself so SQLite tests get it via `create_all()` too)
    plus a retry loop in `append_entry()` - a collision now means "retry
    with the fresh latest hash," not "silently fork."
15. **The append-only trigger never covered `TRUNCATE`.** Found while
    using `TRUNCATE` (as `burns_admin`) to clear the corrupted synthetic
    test data that bug #14 left behind - a `BEFORE ROW` trigger (migration
    0002) never fires for `TRUNCATE`, which is statement-level.
    `burns_app` was never granted `TRUNCATE` (migration 0003), so this was
    never exploitable by the app's own role - but the append-only CLAIM
    itself was incomplete without this, and any future grant change could
    have silently reopened it. **Closed**: migration 0005 adds a
    `BEFORE TRUNCATE ... FOR EACH STATEMENT` trigger, tested live even
    against an admin/superuser connection (`tests/postgres/test_postgres_live.py::test_truncate_on_ledger_is_blocked_even_for_admin`).
16. **`gateway/plugins/email_smtp.py`'s exception handler could leak
    `SMTP_PASSWORD`.** `except (smtplib.SMTPException, OSError) as exc:`
    interpolated `str(exc)` directly into the `PluginResult.detail` that
    becomes a Ledger row - found during the security-claims audit
    (item 5) by writing a direct test for `TelegramPlugin`/`SmtpEmailPlugin`
    (neither had ever been tested directly, only via fakes elsewhere).
    Some SMTP server error responses echo client request data back
    verbatim; nothing prevented a real password reaching an internal audit
    log this way. **Closed**: the password is explicitly redacted from
    the detail string before it's ever returned, matching
    `TelegramPlugin`'s already-more-careful pattern (which never echoes
    `resp.text` back either).

## Item 5 - Security-claims audit (2026-09-29)

Grepped every docstring/comment/doc for a claimed security property (gate,
check, verify, owner, append-only, refuse, block, enforce) and confirmed
each one has a real test. Gaps found and closed:

| Claim | File | Test |
|---|---|---|
| `owner_chat_id` independently re-verified (not just the caller's own check) | `core/approvals.py::decide_approval` | `test_core_execute.py::test_decide_approval_rejects_a_mismatched_owner_chat_id` |
| All 7 `hard_block` actions actually raise (only 2 had a named test before) | `core/policy_engine.py::classify` | `test_policy_engine.py::test_every_documented_hard_block_action_actually_raises` (parametrized x7) |
| DLP's *configured-secret-VALUE* substring layer (distinct from the regex-shape layer) | `core/dlp.py::scan_text` | `test_dlp.py` (new file, 4 tests) |
| `create_approval` refuses a non-Tier-2/3 tier | `core/approvals.py::create_approval` | `test_core_execute.py::test_create_approval_refuses_a_non_tier_2_or_3_tier` |
| `verify_api_key` returns 503 (not 401) when the server itself has no token configured | `gateway/app.py::verify_api_key` | `test_gateway_app.py::test_execute_returns_503_when_server_has_no_token_configured` |
| Budget guard's own claims (mission vs. monthly cap, warn/stop thresholds) - previously only indirectly exercised | `core/budget_guard.py` | `test_budget_guard.py` (new file, 7 tests) |
| Append-only also covers `TRUNCATE` | `db/migrations/versions/0005_*` | `test_postgres_live.py::test_truncate_on_ledger_is_blocked_even_for_admin` |
| `TelegramPlugin`/`SmtpEmailPlugin` never leak the token/password into a Ledger-bound detail string | `gateway/plugins/telegram.py`, `email_smtp.py` | `test_telegram_plugin.py`, `test_email_smtp_plugin.py` (new files; the SMTP one caught bug #16 above) |
| `database_url` never uses the admin role | `core/app_config.py::load_core_config` | `test_app_config.py::test_load_core_config_database_url_never_uses_the_admin_role` |
| `core.ledger.append_entry()` is the only write path, hash-chain safe under concurrency | `core/ledger.py` | `test_ledger.py` + `test_postgres_live.py`'s two-thread test (found bug #14) |
| burns_app can't bypass via grant OR `session_replication_role` | `db/migrations/versions/0003_*` | `test_postgres_live.py::test_direct_update/delete_on_ledger_is_blocked_by_grant`, `test_app_connection_is_the_restricted_burns_app_role_not_a_superuser`, `test_burns_app_cannot_bypass_the_trigger_via_session_replication_role`, `test_burns_app_update_is_blocked_by_the_trigger_even_if_granted` |

Claims that were ALREADY adequately tested (a non-exhaustive sample, to
show the audit wasn't only gap-finding): hard-block-before-tier-logic
ordering, Tier-3 cooling period, params-hash rebinding after approval,
mark_executing's atomic single-winner claim, `is_target_authorised`'s
scope-file check, `/health` needing no auth, the bot's owner-only
`from_chat_id` check, `expire_pending_approvals`, the git_push
path-traversal guard, disabled-plugin refusal without crashing the
Gateway.

## Item 6 - Full suite parametrized sqlite/postgres (2026-09-29)

`tests/unit/conftest.py` adds a `session` fixture parametrized over
`["sqlite", "postgres"]` - most DB-touching test files
(`test_ledger.py`, `test_core_execute.py`, `test_scheduler.py`,
`test_reconciliation.py`, `test_missions.py`, `test_budget_guard.py`,
`test_llm_spend.py`, `test_ledger_anchor.py`) now run against BOTH
automatically. The Postgres half:
- Connects as `burns_app` (same role the real Gateway uses), against a
  SEPARATE, dedicated `burns_os_test` database (not the "live" `burns_os`
  this session's manual bring-up testing has been writing real demo/
  evidence rows into - found live: SAVEPOINT-based per-test rollback only
  isolates a test's own NEW writes, it does not hide rows some earlier,
  already-committed transaction left behind).
- Uses the standard SQLAlchemy "join a Session into an external
  transaction" SAVEPOINT recipe for per-test isolation - every test's
  writes (including `core.ledger.append_entry()`'s own internal
  rollback-and-retry on a hash collision) are discarded at teardown.
- Skips (not fails) automatically when Postgres isn't reachable, so CI or
  a dev machine without `make up` running still gets the SQLite half.
- One test (`test_ledger.py::test_append_entry_retries_on_a_prev_hash_collision`)
  is SQLite-only by design - its own single-thread SAVEPOINT-within-a-
  SAVEPOINT simulation doesn't play well with the outer per-test SAVEPOINT,
  and the thing it proves is already proven far more convincingly by
  `test_postgres_live.py`'s genuine two-thread test.

`tests/unit/test_gateway_app.py` and `test_mcp_server.py` were
deliberately left SQLite-only - both build their own
session-factory/multi-connection wiring (StaticPool for a shared
in-memory DB across TestClient's requests) that doesn't map cleanly onto
the parametrized fixture, and their real HTTP/MCP-transport behavior is
already covered live (see README).

## Incident follow-up (2026-09-29) - see `docs/incidents/2026-09-litellm-table-drop.md`

Full postmortem in that file. Summary of what changed:

- **`litellm_app`**: a new, dedicated, `NOSUPERUSER` Postgres role that owns
  `litellm_db` and nothing else. `burns_os`'s default `PUBLIC CONNECT`
  grant is revoked (migration `0006`) - `litellm_app` cannot even open a
  connection to `burns_os` now, proven live
  (`test_litellm_app_cannot_connect_to_burns_os`).
- **`burns_app` has no DDL rights** on `burns_os` - confirmed live
  (`CREATE TABLE` -> `permission denied for schema public`), not just
  assumed from Postgres 16's default -
  `test_burns_app_has_no_ddl_rights_on_burns_os`.
- **langfuse** was already isolated by construction (its own separate
  `langfuse-db` *container/server*, not just a database) - no code change
  needed, confirmed by re-reading `docker-compose.yml`.
- `core.ledger.verify_chain()` / `core.ledger_anchor.verify_against_anchors()`
  now fail cleanly (`ok: False`, clear reason) instead of raising an
  unhandled `DBAPIError` when the `ledger` table is missing entirely (an
  empty-but-existing table already worked correctly before this) - proved
  live against the freshly-recreated `burns_os`, before migrations were
  reapplied:
  ```
  ChainVerification(ok=False, total_entries=0, first_broken_id=None,
    reason='Could not query the ledger table - it may be missing entirely: ...')
  AnchorVerification(ok=False, checked_anchors=1, failed_anchor=...,
    reason='Could not query the ledger table while checking anchor id=8 ...')
  ```
- **The live DB fork was not truncated.** Per instruction: `pg_dump`'d the
  full corrupted `burns_os` to `backups/burns_os_incident_20260929.dump`
  (+ `.sha256`), copied the pre-incident anchor file to
  `backups/ledger_anchor_incident_20260929.jsonl` (+ `.sha256`), renamed
  the live corrupted database aside to `burns_os_incident_20260929`
  (still on the same Postgres server, not dropped), then created a fresh
  `burns_os` and ran all 6 migrations cleanly. `LEDGER_ANCHOR_PATH` now
  points at a new file (`ledger_anchor_v2_20260929.jsonl`) rather than
  editing/deleting the old one, which is kept as historical record too.
  Post-recreation: `chain_ok: true, anchor_ok: true` on the live
  `/ledger/verify` endpoint.

## Backups (`make backup` / `make restore`, 2026-09-29)

`scripts/backup.py`: nightly `pg_dump` of `burns_os` (custom format) +
the ledger anchor file, both GPG-encrypted (AES256 symmetric,
`BACKUP_ENCRYPTION_PASSPHRASE` in `.env`), each with a `.sha256` sidecar.
Local retention is `BACKUP_RETENTION_DAYS` (default 14, auto-pruned every
run). Also copied to `BACKUP_OFFSITE_DIR` - **not actually offsite yet**,
see that var's TODO in `.env`/`.env.example`: it's a second local folder
until Mohit picks a real destination (S3, another host, etc.).

`scripts/restore.py`: verifies the archive's checksum first (refuses a
tampered/corrupted archive), decrypts, restores into a **fresh, separate**
database (never touches the live `burns_os`), then runs
`core.ledger.verify_chain()` against the restored data and fails loudly
if it doesn't check out.

**Tested live end-to-end** (2026-09-28/29): `make backup` produced
`backups/burns_os_20260928_212901.dump.gpg` (4526 bytes) +
`..._anchor.jsonl.gpg` (248 bytes); `make restore
ARCHIVE=backups/burns_os_20260928_212901.dump.gpg` restored into
`burns_os_restore_test_20260928_212910` and reported
`RESTORE_VERIFY_CHAIN_OK=True entries=14`. Test database dropped after
confirming (a throwaway restore target, not the live DB).

Neither script is wired to a real nightly cron/scheduler process yet
(same status `core/scheduler.py` was in before `scripts/scheduler_loop.py`
existed) - that's the next step once Mohit confirms this approach.

## Git

`scripts/git-hooks/pre-commit` (tracked in the repo; install via
`make install-hooks`, since `.git/hooks/` itself is never tracked) runs
`gitleaks protect --staged` via Docker before every commit, refusing to
commit if it finds a likely secret. Installed and active for this repo
now.

## Dashboard: Telegram postponed, Web Dashboard is now primary (2026-09-29)

Mohit's decision: Telegram is postponed; the Web Dashboard becomes the
primary approval/control channel, Telegram to be re-added later as a
second one. Real, live-tested work, not a plan:

- **`core/approval_channels.py`**: a new `ApprovalChannel` interface -
  `TelegramChannel` (unchanged logic, just not currently wired into any
  running process) and `DashboardChannel`. `core.approvals.decide_approval()`
  was refactored to take a `channel` + `**credentials` instead of a bare
  `owner_chat_id` string - it always calls `channel.authorize_decision()`
  itself and never trusts a pre-computed "is this the owner" boolean from
  the caller (the same principle that closed the owner-check bug from the
  previous round). `DashboardChannel` requires a valid, non-expired
  dashboard session for Tier 2, and ADDITIONALLY a fresh TOTP code
  (verified right then, not from login time) for Tier 3.
- **`dashboard/`** (new package): `auth.py` (argon2 password hashing,
  TOTP 2FA enrollment/verification via `pyotp`+QR code, server-side
  sessions with SHA-256-hashed-at-rest tokens, session-bound CSRF tokens,
  login rate-limit/lockout, every login/approval/command logged to the
  Ledger), `models.py` (`owner_account`/`dashboard_session`/
  `command_request` - mutable app state, unlike the append-only Ledger -
  migration `0007`), `app.py` (FastAPI + Jinja2/HTMX: Home, Approvals,
  Alerts, Ledger [search + live verify button], Missions, Budgets,
  Command box).
- **Security posture, all live-tested**: binds `127.0.0.1` only
  (`docker-compose.yml`'s `dashboard` service); Tailscale documented for
  phone access (`docs/DASHBOARD.md`) - never a public port; CSRF on every
  POST (missing -> FastAPI's own 422 for the empty-field case, forged ->
  this module's own 403); `HttpOnly`/`SameSite=Strict`/`Secure`-by-default
  cookies (`DASHBOARD_COOKIE_SECURE`); 30-minute sliding idle timeout;
  5-failed-attempt lockout.
- **STEP 3 acceptance tests B/C/D/E/F/G, redone via the Dashboard with a
  real Chromium browser** (Playwright, `tests/e2e/test_acceptance.py`,
  `make test-e2e`) - **11/11 passing**, screenshots in
  `tests/e2e/evidence/` for every step (pending-approval card, reject,
  approve, Tier-3-without-TOTP refusal, Tier-3-with-fresh-TOTP approval,
  scheduler auto-executing the honest stub after cooling, hard-block/DLP
  alerts visible on the Alerts page, double-click-approve executing
  once, and all five of item E's rejection cases: unauthenticated,
  wrong password, wrong TOTP, forged/missing CSRF, expired session).
  This is a genuine, re-runnable regression suite against the live
  system, not a one-time script - it caches the TOTP secret locally
  (`tests/e2e/.totp_secret_cache`, gitignored - the real owner account's
  secret is never retrievable again after enrollment, by design) so it
  can be re-run against the same persistent deployment.
- **229 -> 231+ unit tests, +14 for `core/approval_channels.py`, +21 for
  `dashboard/auth.py`, +17 for `dashboard/app.py`** (TestClient-based,
  same pattern as `test_gateway_app.py`) - see the final count at the top
  of this file.

## Item D follow-up: backups upgraded to asymmetric GPG (2026-09-29)

The first backup implementation used a shared symmetric passphrase - a
`.env` leak alone would have been enough to decrypt every past backup.
**Closed**: `scripts/backup.py`/`scripts/restore.py` now use a real GPG
keypair (`docs/BACKUP_RECOVERY.md` has the full procedure and live-test
evidence) - this machine only ever holds the PUBLIC key
(`deploy/keys/burns_os_backups_public.asc`, safe to commit); the PRIVATE
key lives offline, handed to Mohit directly, imported only transiently
during an actual recovery. Proved live: a decrypt attempt without the
private key genuinely fails (`decryption failed: No secret key`);
importing it from its offline export and re-running `scripts/restore.py`
succeeds end to end (`RESTORE_VERIFY_CHAIN_OK=True`).

Also closed: `burns_app`/`litellm_app` had residual `CONNECT`+table
grants on the archived incident database (`burns_os_incident_20260929` -
a renamed COPY of the pre-fix `burns_os`, carrying its old grants) -
revoked; a live test now guards against this regression
(`test_burns_app_cannot_connect_to_the_archived_incident_database`).

**Still open**: `BACKUP_OFFSITE_DIR` is a second local folder, not
actually offsite - real rclone-based offsite sync is pending Mohit
choosing and configuring a remote (`docs/BACKUP_RECOVERY.md`'s own TODO
section has the exact next steps). Neither `make backup` nor a nightly
schedule is wired into `scripts/scheduler_loop.py` yet - a manual `make
backup` today, a real cron once the offsite piece is decided.

## Milestone 2 (Hermes): design updated, still explicitly not approved

`docs/MILESTONE_2_HERMES_DESIGN.md` - design only, no code. Interface is
now the Dashboard's Command box -> Hermes (not Telegram). Hermes's
credential model is precisely two narrow, revocable tokens (an
MCP-endpoint auth token distinct from `GATEWAY_INTERNAL_TOKEN`, and a
budget-capped LiteLLM virtual key, not the master key) - explicitly not
"zero" as a hand-wave, but zero REAL external-system credentials
(no Telegram/SMTP/DB/trading/git access, direct or otherwise). The
Gateway's MCP server needs a second transport (`streamable-http`, not
just `stdio`) for a separate container to reach it - `gateway/mcp_server.py`'s
`build_server()` already supports this, unused so far. Milestone 2 code
still requires Mohit's explicit approval before any of this is built.

## Not yet started

Squad Builder, Evals, Monitor & Self-Heal, Daily/Weekly reports, Web
Dashboard, all 10 departments' actual tools/roles beyond the generic
Gateway plugins, backup/restore scripts, Milestone 2 (Hermes Agent
container) - explicitly not approved yet.
