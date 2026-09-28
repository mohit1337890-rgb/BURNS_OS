# Burns OS Makefile.
# up/down/test-pg/logs are WRITTEN, NOT YET RUN - this dev machine has no
# Docker/WSL2 (see docs/KNOWN_LIMITS.md). `test` and `verify-ledger` work
# today against SQLite with no Docker dependency.

.PHONY: up down migrate test test-pg verify-ledger logs backup restore install-hooks

up:
	docker compose up -d --build
	$(MAKE) migrate

# Admin (burns_admin/superuser) creds are injected here ONLY, for this one
# transient `exec` - never baked into the long-running gateway/
# approvals_bot/scheduler containers' own environment (they connect as the
# restricted burns_app role - core/app_config.py, KNOWN_LIMITS gap #10).
# Reads them straight from .env rather than requiring the caller's shell to
# have them exported.
migrate:
	docker compose exec \
		-e POSTGRES_USER=$$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-) \
		-e POSTGRES_PASSWORD=$$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2-) \
		gateway alembic -c alembic.ini upgrade head

down:
	docker compose down

# Runs the real pytest suite (SQLite in-memory, no external services
# required) - this is what CI/a pre-push check should run.
test:
	.venv/Scripts/python.exe -m pytest tests/unit/ -v

# Same suite, but against a real running Postgres (via docker-compose) -
# exercises the append-only trigger (migration 0002) and any other
# Postgres-specific behavior SQLite can't. Requires `make up` first.
# Scoped to tests/postgres/ rather than tests/ - the gateway image doesn't
# (and shouldn't) contain approvals_bot/, whose tests/unit/ files would
# otherwise fail to even import inside this container.
test-pg:
	docker compose exec gateway python -m pytest tests/postgres/ -v -m postgres

# Runs `core.ledger.verify_chain` + `core.ledger_anchor.verify_against_anchors`
# against the real running ledger via the Gateway's own /ledger/verify
# endpoint - requires GATEWAY_INTERNAL_TOKEN in .env and `make up` first.
verify-ledger:
	@curl -s -X POST http://localhost:8080/ledger/verify \
		-H "X-API-Key: $${GATEWAY_INTERNAL_TOKEN}" | python -m json.tool

logs:
	docker compose logs -f --tail=200

# Nightly pg_dump + anchor file, GPG-encrypted, 14-day local retention +
# a second-location copy (see BACKUP_OFFSITE_DIR's TODO in .env - not
# actually offsite yet). Requires `make up` first.
backup:
	.venv/Scripts/python.exe scripts/backup.py

# Restores a backup into a FRESH test database (never touches the live
# burns_os) and verifies it. Usage: make restore ARCHIVE=backups/burns_os_<ts>.dump.gpg
restore:
	.venv/Scripts/python.exe scripts/restore.py $(ARCHIVE)

# .git/hooks/ is never tracked by git - this copies the checked-in hook
# (scripts/git-hooks/pre-commit) into place. Run this once after cloning.
install-hooks:
	cp scripts/git-hooks/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "Installed gitleaks pre-commit hook."
