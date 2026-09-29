"""
Nightly backup: pg_dump of the live `burns_os` database + the ledger
anchor file, both GPG-encrypted with the Burns OS Backups PUBLIC key
(asymmetric - this script only ever needs the PUBLIC key, never the
private one, so this machine having its backup process compromised does
NOT also compromise the ability to decrypt past backups - see
docs/BACKUP_RECOVERY.md for the private key's offline storage and the
recovery procedure). Kept for BACKUP_RETENTION_DAYS (default 14), plus a
copy in a second location (BACKUP_OFFSITE_DIR - see its own TODO in
.env/.env.example: not actually offsite yet, Mohit still needs to pick a
real destination).

Runs on the HOST (not inside a container) - orchestrates via `docker
compose exec`/`docker cp`, same pattern used manually during the
2026-09-29 incident response (docs/incidents/2026-09-litellm-table-drop.md).
Requires: docker on PATH, gpg on PATH with the Burns OS Backups public key
already imported (docs/BACKUP_RECOVERY.md), POSTGRES_USER/PASSWORD (admin)
and BACKUP_GPG_RECIPIENT in the environment (.env - this script loads it
itself via python-dotenv, doesn't require the caller to have exported it).

Usage: python scripts/backup.py   (or `make backup`)
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = REPO_ROOT / "backups"
POSTGRES_CONTAINER = "burns_os_system-postgres-1"
ANCHOR_VOLUME = "burns_os_system_ledger_anchor_data"


def _load_env() -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _gpg_encrypt(plain_path: Path, out_path: Path, recipient: str) -> None:
    """Encrypts to the Burns OS Backups PUBLIC key only - this process
    never touches the private key (docs/BACKUP_RECOVERY.md). --trust-model
    always: we're encrypting to our own dedicated backup key, not
    verifying a third party's identity - the normal WoT trust prompt
    would otherwise block unattended/automated runs."""
    subprocess.run(
        [
            "gpg", "--batch", "--yes", "--trust-model", "always",
            "--recipient", recipient, "--encrypt",
            "-o", str(out_path), str(plain_path),
        ],
        check=True,
    )


def _dump_database(env: dict[str, str], out_path: Path) -> None:
    admin_user = env["POSTGRES_USER"]
    admin_password = env["POSTGRES_PASSWORD"]
    db = env.get("POSTGRES_DB", "burns_os")
    with open(out_path, "wb") as f:
        subprocess.run(
            ["docker", "exec", "-e", f"PGPASSWORD={admin_password}", POSTGRES_CONTAINER,
             "pg_dump", "-U", admin_user, "-d", db, "-Fc"],
            stdout=f, check=True,
        )


def _copy_anchor_file(env: dict[str, str], out_path: Path) -> bool:
    anchor_path_in_volume = env.get("LEDGER_ANCHOR_PATH", "/data/ledger_anchor.jsonl")
    # env's LEDGER_ANCHOR_PATH for the host process is a relative ./data/...
    # path (see .env) - the CONTAINER path is always /data/<filename>,
    # same volume, different mount point naming.
    filename = Path(anchor_path_in_volume).name
    container_name = f"burns_os_backup_anchor_copy_{os.getpid()}"
    subprocess.run(["docker", "create", "--name", container_name, "-v", f"{ANCHOR_VOLUME}:/data", "alpine", "true"], check=True, capture_output=True)
    try:
        result = subprocess.run(
            ["docker", "cp", f"{container_name}:/data/{filename}", str(out_path)],
            capture_output=True,
        )
        return result.returncode == 0
    finally:
        subprocess.run(["docker", "rm", container_name], check=True, capture_output=True)


def _prune_old_backups(retention_days: int) -> list[Path]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    pruned = []
    for f in BACKUPS_DIR.glob("burns_os_*.gpg"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            f.unlink()
            sha_file = f.with_suffix(f.suffix + ".sha256")
            if sha_file.exists():
                sha_file.unlink()
            pruned.append(f)
    return pruned


def main() -> int:
    env = _load_env()
    for required in ("POSTGRES_USER", "POSTGRES_PASSWORD", "BACKUP_GPG_RECIPIENT"):
        if not env.get(required):
            print(f"REFUSED: {required} not set in .env - cannot back up.", file=sys.stderr)
            return 1

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    recipient = env["BACKUP_GPG_RECIPIENT"]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        print(f"[backup] dumping burns_os ...")
        dump_plain = tmp_path / "burns_os.dump"
        _dump_database(env, dump_plain)
        dump_enc = BACKUPS_DIR / f"burns_os_{stamp}.dump.gpg"
        _gpg_encrypt(dump_plain, dump_enc, recipient)
        dump_sha = _sha256_file(dump_enc)
        (BACKUPS_DIR / f"burns_os_{stamp}.dump.gpg.sha256").write_text(f"{dump_sha}  {dump_enc.name}\n")
        print(f"[backup] wrote {dump_enc} ({dump_enc.stat().st_size} bytes, sha256={dump_sha[:16]}...)")

        print(f"[backup] copying ledger anchor file ...")
        anchor_plain = tmp_path / "ledger_anchor.jsonl"
        if _copy_anchor_file(env, anchor_plain) and anchor_plain.exists():
            anchor_enc = BACKUPS_DIR / f"burns_os_{stamp}_anchor.jsonl.gpg"
            _gpg_encrypt(anchor_plain, anchor_enc, recipient)
            anchor_sha = _sha256_file(anchor_enc)
            (BACKUPS_DIR / f"burns_os_{stamp}_anchor.jsonl.gpg.sha256").write_text(f"{anchor_sha}  {anchor_enc.name}\n")
            print(f"[backup] wrote {anchor_enc} ({anchor_enc.stat().st_size} bytes, sha256={anchor_sha[:16]}...)")
        else:
            print("[backup] WARNING: no anchor file found in the volume yet - skipping (not a failure on a brand-new deployment).")

    retention_days = int(env.get("BACKUP_RETENTION_DAYS", "14"))
    pruned = _prune_old_backups(retention_days)
    if pruned:
        print(f"[backup] pruned {len(pruned)} backup(s) older than {retention_days} days: {[p.name for p in pruned]}")

    offsite_dir_raw = env.get("BACKUP_OFFSITE_DIR")
    if offsite_dir_raw:
        offsite_dir = (REPO_ROOT / offsite_dir_raw).resolve()
        offsite_dir.mkdir(parents=True, exist_ok=True)
        for f in BACKUPS_DIR.glob(f"burns_os_{stamp}*"):
            shutil.copy2(f, offsite_dir / f.name)
        print(f"[backup] copied this run's files to {offsite_dir} - NOTE: see BACKUP_OFFSITE_DIR's TODO in .env, this is not actually offsite yet.")

    print("[backup] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
