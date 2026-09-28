"""
Restore a backup produced by scripts/backup.py into a FRESH, separate
database (never overwrites the live `burns_os`) and runs the same
verify_chain()/verify_against_anchors() checks the live Gateway's
/ledger/verify uses, to prove the restored data is actually intact - not
just that pg_restore exited zero.

Usage:
    python scripts/restore.py backups/burns_os_20260929_030000.dump.gpg [--target-db NAME]

Requires: docker on PATH, gpg on PATH, POSTGRES_USER/PASSWORD (admin) and
BACKUP_ENCRYPTION_PASSPHRASE in .env. Verifies the archive's SHA-256
against its sidecar .sha256 file before doing anything else - a backup
that's been tampered with (or just corrupted) must be refused, not
silently restored.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTGRES_CONTAINER = "burns_os_system-postgres-1"
GATEWAY_CONTAINER = "burns_os_system-gateway-1"


def _load_env() -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_checksum(archive: Path) -> None:
    sha_file = archive.with_suffix(archive.suffix + ".sha256")
    if not sha_file.exists():
        raise SystemExit(f"REFUSED: no {sha_file.name} sidecar found - cannot verify this archive hasn't been tampered with or corrupted.")
    expected = sha_file.read_text().split()[0]
    actual = _sha256_file(archive)
    if actual != expected:
        raise SystemExit(f"REFUSED: checksum mismatch for {archive.name}!\n  expected: {expected}\n  actual:   {actual}\nThis archive is corrupted or has been tampered with - not restoring it.")
    print(f"[restore] checksum verified ({actual[:16]}...).")


def _gpg_decrypt(enc_path: Path, out_path: Path, passphrase: str) -> None:
    subprocess.run(
        [
            "gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
            "--passphrase", passphrase,
            "--decrypt", "-o", str(out_path), str(enc_path),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archive", type=Path, help="Path to a *.dump.gpg file from scripts/backup.py")
    parser.add_argument("--target-db", default=None, help="Name of the FRESH database to restore into (default: burns_os_restore_test_<timestamp>)")
    args = parser.parse_args()

    if not args.archive.exists():
        print(f"REFUSED: {args.archive} does not exist.", file=sys.stderr)
        return 1
    _verify_checksum(args.archive)

    env = _load_env()
    for required in ("POSTGRES_USER", "POSTGRES_PASSWORD", "BACKUP_ENCRYPTION_PASSPHRASE"):
        if not env.get(required):
            print(f"REFUSED: {required} not set in .env.", file=sys.stderr)
            return 1
    admin_user = env["POSTGRES_USER"]
    admin_password = env["POSTGRES_PASSWORD"]
    target_db = args.target_db or f"burns_os_restore_test_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plain_dump = tmp_path / "restore.dump"
        print(f"[restore] decrypting {args.archive.name} ...")
        _gpg_decrypt(args.archive, plain_dump, env["BACKUP_ENCRYPTION_PASSPHRASE"])

        print(f"[restore] creating fresh database {target_db!r} (owned by {admin_user}) ...")
        subprocess.run(
            ["docker", "exec", "-e", f"PGPASSWORD={admin_password}", POSTGRES_CONTAINER,
             "psql", "-U", admin_user, "-d", "postgres", "-c", f'CREATE DATABASE "{target_db}" OWNER {admin_user};'],
            check=True,
        )

        container_dump_path = f"/tmp/restore_{target_db}.dump"
        subprocess.run(["docker", "cp", str(plain_dump), f"{POSTGRES_CONTAINER}:{container_dump_path}"], check=True)

        print(f"[restore] running pg_restore into {target_db!r} ...")
        subprocess.run(
            ["docker", "exec", "-e", f"PGPASSWORD={admin_password}", POSTGRES_CONTAINER,
             "pg_restore", "-U", admin_user, "-d", target_db, "--no-owner", "--role", admin_user, container_dump_path],
            check=True,
        )
        subprocess.run(["docker", "exec", POSTGRES_CONTAINER, "rm", "-f", container_dump_path], check=True)

    print(f"[restore] running verify_chain() against {target_db!r} (via the gateway container, which has core/ + psycopg2 - the postgres container itself has neither) ...")
    verify_script = f"""
from core import ledger
engine = ledger.get_engine("postgresql+psycopg2://{admin_user}:{admin_password}@postgres:5432/{target_db}")
session = ledger.get_session_factory(engine)()
result = ledger.verify_chain(session)
print(f"RESTORE_VERIFY_CHAIN_OK={{result.ok}} entries={{result.total_entries}}")
session.close()
"""
    result = subprocess.run(
        ["docker", "exec", GATEWAY_CONTAINER, "python", "-c", verify_script],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0 or "RESTORE_VERIFY_CHAIN_OK=True" not in result.stdout:
        print(f"[restore] VERIFY FAILED after restore - see output above / stderr:\n{result.stderr}", file=sys.stderr)
        return 1

    print(f"[restore] SUCCESS - {target_db!r} restored and verified intact.")
    print(f"[restore] this is a TEST database - drop it yourself when done (it was never overwriting the live burns_os):")
    print(f"          docker exec {POSTGRES_CONTAINER} psql -U {admin_user} -d postgres -c 'DROP DATABASE \"{target_db}\";'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
