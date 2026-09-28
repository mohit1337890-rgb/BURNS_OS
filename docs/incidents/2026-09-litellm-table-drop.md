# Incident: litellm's schema-sync dropped Burns OS's own tables

**Date:** 2026-09-28, during live Milestone 1 bring-up testing
**Severity:** High (real data loss), no real-world impact (dev/test environment, synthetic data only)
**Status:** Closed - root cause structurally eliminated, verification hardened, regression tests added

## What happened

While wiring up Postgres role separation (`burns_app` vs `burns_admin` -
KNOWN_LIMITS gap #10), `docker compose up -d` recreated the `litellm`
container. On startup, litellm ran its own Prisma-based schema
synchronization ("`db push`"), which logged:

```
Prisma schema loaded from schema.prisma
Datasource "client": PostgreSQL database "burns_os", schema "public" at "postgres:5432"
  • You are about to drop the `alembic_version` table, which is not empty (1 rows).
  • You are about to drop the `approvals` table, which is not empty (7 rows).
  • You are about to drop the `ledger` table, which is not empty (26 rows).
🚀  Your database is now in sync with your Prisma schema. Done in 632ms
```

`ledger`, `approvals`, and `alembic_version` were dropped outright,
seconds after litellm restarted, without any explicit instruction to
touch Burns OS's own data.

## Impact

- All 26 ledger rows and 7 approval rows at that point in time were lost,
  including the audit trail for a just-completed manual reconciliation
  (`d0521604-3749-4cfc-b3f5-0dafcc3681bf` - see `docs/KNOWN_LIMITS.md`
  bugs #11/#12) - that specific historical record is gone; the
  reconciliation *mechanism* itself remains fully tested and had been
  proven live once before this happened.
- `alembic_version` was also dropped, so migration history tracking for
  the live database was reset.
- No real user/production data was involved - everything in the ledger at
  that point was synthetic test/demo data generated during this same live
  bring-up session.
- The Docker volume-backed ledger anchor file (`ledger_anchor.jsonl`,
  outside Postgres) was untouched, which is exactly its job - but its
  existing records now referenced ledger rows that no longer existed,
  which correctly made `/ledger/verify`'s anchor check start failing
  (`anchor_ok: false`) the moment the ledger was later recreated. This is
  the verification tooling working as designed, not a second bug.

## Root cause

`docker-compose.yml`'s `litellm` service had `DATABASE_URL` pointing at
`${POSTGRES_DB}` - i.e. `burns_os`, the exact same database
`ledger`/`approvals`/`missions` live in - connected as `${POSTGRES_USER}`,
which at the time was the Postgres superuser. litellm's own
Prisma-managed schema-sync treats the *entire* database's `public` schema
as something it owns and reconciles against its own `schema.prisma` -
anything present in that schema that Prisma doesn't recognize is treated
as drift and dropped. Postgres databases are the actual isolation
boundary here (not schemas within one database), and nothing separated
litellm's from Burns OS's own.

A contributing factor: the connecting role was a superuser, so no
privilege boundary would have stopped this even if schemas had been kept
separate within the one database - litellm's own migration tooling had
unrestricted DDL rights over everything.

## Fix

Two independent, structural layers - either one alone would have
prevented this incident:

1. **Separate database.** litellm now connects to a dedicated `litellm_db`
   database on the same Postgres server, never `burns_os`. Postgres
   databases are fully isolated from each other - no cross-database
   query, DDL, or schema-sync can reach from one into another, regardless
   of what any application-level tooling (Prisma or otherwise) decides to
   do inside its own connection. This makes the specific failure mode
   (schema-sync drops unrecognized tables) **structurally impossible**
   against `burns_os`, not just avoided by current configuration.
2. **Separate, minimally-privileged role.** litellm now connects as
   `litellm_app` (owns `litellm_db` only, `NOSUPERUSER`), not
   `burns_admin`. Even if litellm were ever misconfigured to point at the
   wrong database again, `litellm_app` has no `CONNECT` privilege on
   `burns_os` at all (see "Prevention" below) - it cannot open a
   connection there, let alone run DDL.

Verification/robustness hardening that came out of investigating this:

3. `core.ledger.verify_chain()` and `core.ledger_anchor.verify_against_anchors()`
   previously raised an unhandled `DBAPIError` if the `ledger` table
   didn't exist at all (as opposed to existing-but-empty, which already
   worked correctly) - meaning `gateway/app.py`'s `/ledger/verify` would
   have 500'd during exactly this kind of incident instead of clearly
   reporting `ok: false`. Both now catch a missing-table failure and
   return a clean, explicit failure result. Proven live (see "Evidence"
   below) and covered by new unit tests
   (`tests/unit/test_ledger.py::test_verify_chain_fails_cleanly_when_the_table_is_missing_entirely`,
   `tests/unit/test_ledger_anchor.py::test_verify_against_anchors_fails_cleanly_when_the_table_is_missing_entirely`).

## Prevention (defense in depth against a repeat, or a similar mistake elsewhere)

- **Postgres databases, not schemas, are the isolation unit** for any
  third-party service sharing this Postgres server going forward -
  langfuse already followed this pattern independently (its own separate
  `langfuse-db` *container*, not just a separate database - an even
  stronger form of the same idea).
- **`PUBLIC`'s default `CONNECT` privilege is revoked on `burns_os`**
  (migration `0006_revoke_public_connect.py`, found while verifying this
  fix) - Postgres grants every role `CONNECT` on every database by
  default unless revoked; `litellm_app` could still open a connection
  and run read queries against `burns_os` even after being moved to its
  own database, simply because nothing had ever revoked the default. Only
  `burns_app` (and `burns_admin`, as superuser) can connect to `burns_os`
  now.
- **`burns_app` has no DDL rights at all** on `burns_os`'s schema (no
  `CREATE`/`ALTER`/`DROP`/`TRUNCATE`) - confirmed live, not just assumed
  from Postgres 16's default (`PUBLIC` no longer gets schema `CREATE` by
  default as of Postgres 15, but this is now also explicitly verified by
  a live test rather than relied upon implicitly).
- **`/ledger/verify` (and anything else that queries the ledger) fails
  loudly and cleanly** on a missing table now, rather than crashing -
  the fastest possible signal that something structurally wrong has
  happened, without needing to notice a stack trace in a log.

## Evidence

- litellm's own log output at the moment of the drop (quoted above,
  timestamps `2026-09-28T20:27:49-50Z`).
- Live proof the fix works, captured against the freshly-recreated
  `burns_os` (before migrations were reapplied, so the `ledger` table
  genuinely did not exist yet - the same situation this incident left
  behind):
  ```
  CHAIN VERIFY (ledger table does not exist yet):
    ChainVerification(ok=False, total_entries=0, first_broken_id=None,
      reason='Could not query the ledger table - it may be missing
      entirely: relation "ledger" does not exist ...')
  ANCHOR VERIFY (stale anchor file, no ledger table):
    AnchorVerification(ok=False, checked_anchors=1, failed_anchor=...,
      reason='Could not query the ledger table while checking anchor
      id=8 - it may be missing entirely: relation "ledger" does not
      exist ...')
  ```
- Live proof `litellm_app` cannot reach `burns_os` after migration 0006:
  `psql -U litellm_app -d burns_os` refuses to connect
  (`FATAL: permission denied for database "burns_os"`) - see
  `tests/postgres/test_postgres_live.py::test_litellm_app_cannot_connect_to_burns_os`.
- Live proof `burns_app` has no DDL rights: `CREATE TABLE` as `burns_app`
  against `burns_os` fails with `permission denied for schema public` -
  see `tests/postgres/test_postgres_live.py::test_burns_app_has_no_ddl_rights_on_burns_os`.
- The corrupted live database was archived, not deleted, before recovery:
  `backups/burns_os_incident_20260929.dump` (`pg_dump -Fc`) and the
  pre-incident anchor file `backups/ledger_anchor_incident_20260929.jsonl`,
  each with a `.sha256` checksum alongside it. The corrupted database
  itself was also kept, renamed to `burns_os_incident_20260929` rather
  than dropped, on the same Postgres server.

## Timeline

- **2026-09-28, ~19:23-20:27 UTC**: Live bring-up testing generates real
  ledger/approval activity (STEP 1-3 acceptance tests, the `d0521604`
  crash-safety incident and its reconciliation - see KNOWN_LIMITS bugs
  #11/#12).
- **~20:27:49 UTC**: `docker compose up -d` (applying Postgres role-
  separation changes) recreates the `litellm` container; its Prisma
  schema-sync drops `ledger`/`approvals`/`alembic_version` from `burns_os`.
- **Same session, minutes later**: Discovered while verifying the role
  separation work (an unexpectedly empty `\dt` output). Root-caused via
  litellm's own logs.
- **Same session**: litellm moved to its own database (`litellm_db`).
- **2026-09-29**: `litellm_app` role created (owns `litellm_db` only);
  `burns_os`'s `PUBLIC CONNECT` revoked (migration 0006); missing-table
  crash fixed in `core/ledger.py` and `core/ledger_anchor.py`; live
  database archived (`pg_dump` + checksum) and recreated cleanly with all
  6 migrations applied; this document written.
