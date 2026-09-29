# Web Dashboard

The primary approval/control channel (Telegram postponed 2026-09-29 - see
`core/approval_channels.py`, `docs/KNOWN_LIMITS.md`). FastAPI + Jinja2/
HTMX, single owner account, argon2 password + TOTP 2FA.

## Access model - 127.0.0.1 only, never a public port

`docker-compose.yml`'s `dashboard` service publishes
`127.0.0.1:8090:8090` - bound to loopback only. There is no config flag
to change this to `0.0.0.0` short of editing the compose file directly,
which is deliberate. **Never** put this behind a public reverse proxy or
port-forward it on a router - the security model (session cookies,
CSRF, rate-limiting) assumes the network path itself is already trusted,
the same assumption `postgres`/`litellm` already make.

## Phone/remote access: Tailscale

Tailscale gives you a private, encrypted (WireGuard) network between your
own devices, with no public exposure - this is the only supported way to
reach the Dashboard from a phone or anywhere off this machine.

1. Install Tailscale on this machine and sign in
   (https://tailscale.com/download) - it joins your own private "tailnet."
2. Install the Tailscale app on your phone, same account.
3. **Recommended**: use `tailscale serve` to get a real HTTPS endpoint on
   your tailnet (keeps `DASHBOARD_COOKIE_SECURE=true` working correctly -
   Secure cookies require HTTPS):
   ```powershell
   tailscale serve https / http://127.0.0.1:8090
   ```
   This gives you a `https://<this-machine>.<your-tailnet>.ts.net` URL,
   reachable only from your own tailnet devices, with a real TLS
   certificate Tailscale manages for you.
4. Open that URL from your phone's browser once connected to the
   tailnet. Bookmark it / add to home screen for quick access.

**Do not use `tailscale funnel`** (Tailscale's public-internet-exposure
feature) for this - that defeats the entire point. `tailscale serve`
(tailnet-only) is correct; `tailscale funnel` is not.

If you genuinely only ever access the Dashboard from this same machine
(no phone access), Tailscale isn't needed at all - `http://127.0.0.1:8090`
directly works, but then `DASHBOARD_COOKIE_SECURE` should be set to
`false` in `.env` (Secure cookies aren't sent over plain HTTP) - the
tradeoff is documented in `.env`'s own comment on that variable.

## First-time setup

1. `make up` (or ensure the `dashboard` service is running).
2. Visit the Dashboard URL - `/setup/owner` if no account exists yet.
3. Choose a username and a strong password (12+ characters enforced).
4. You're redirected to `/setup/totp` - scan the QR code with an
   authenticator app (Google Authenticator, 1Password, Authy, etc.),
   enter the current 6-digit code to confirm enrollment.
5. Log in normally from then on: username, password, and the current
   TOTP code.

There is no "forgot password" flow (single-owner, deliberately no email/
SMS recovery path that would itself be an attack surface) - if you lose
access, an admin with direct database access can update
`owner_account.password_hash` (via `dashboard.auth.hash_password()`) and
clear `owner_account.totp_secret` to force re-enrollment.

## Security properties (live-tested 2026-09-29, see docs/KNOWN_LIMITS.md)

- Single owner account, argon2id password hashing.
- TOTP 2FA at login; **Tier-3 approvals require a second, FRESH TOTP
  code** at decision time, not just a valid session
  (`core/approval_channels.py::DashboardChannel`).
- CSRF: session-bound synchronizer token on every POST form.
- Session cookie: `HttpOnly`, `SameSite=Strict`, `Secure` by default (see
  `DASHBOARD_COOKIE_SECURE` above), 30-minute sliding idle timeout.
- Login rate-limit: 5 failed attempts locks the account for 15 minutes
  (`dashboard/auth.py::MAX_FAILED_LOGINS`/`LOCKOUT_MINUTES`).
- Every login attempt (success/fail), every approval decision, and every
  Command box submission is logged to the same hash-chained Ledger
  everything else in Burns OS uses - not a separate audit log to keep in
  sync.

## Pages

| Page | What it shows |
|---|---|
| Home | System health (ledger chain/anchor), pending-approval count, monthly spend, recent alerts |
| Approvals | Every PENDING approval, with Approve/Reject - Tier 3 additionally requires a fresh TOTP code inline |
| Alerts | Hard-block refusals, DLP refusals, `UNKNOWN_OUTCOME` reconciliations, budget stops, reconciled failures - all just Ledger rows matching known patterns, no separate alerts table |
| Ledger | Search + a "Verify chain + anchor" button that runs the real `core.ledger.verify_chain()`/`verify_against_anchors()` live |
| Missions | Every `core.missions.Mission` row and its status |
| Budgets | Global monthly + per-mission spend vs. cap |
| Command | Until Milestone 2 (Hermes) is approved, only creates a LOGGED request in the Ledger - never executes anything. See `docs/MILESTONE_2_HERMES_DESIGN.md` for what changes once it is. |

## Resetting the owner account (dev/test only)

To start over (e.g. a fresh e2e test run, or truly starting the whole
system over): delete the single row in `owner_account` and every row in
`dashboard_session`. There is no soft-delete - this is a deliberate,
manual, admin-only action, same reasoning as `core/ledger.py`'s
append-only design not applying to this table (unlike the Ledger, this
table is legitimately meant to be reset in a dev/test context).
