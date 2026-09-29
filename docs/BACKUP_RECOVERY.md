# Backups: keys, storage, and recovery

Real, tested, asymmetric GPG backups (2026-09-29). This machine can
**encrypt** backups; it deliberately **cannot decrypt** them day-to-day -
only the offline private key can. This document is the actual recovery
procedure, written from a live test of it (see "Evidence" at the bottom),
not a plan that's never been run.

## Why asymmetric, not a shared passphrase

The first version of `scripts/backup.py`/`scripts/restore.py` used GPG
*symmetric* encryption (one shared passphrase, stored in `.env`). That
means anything that can read `.env` on this machine - a compromise, a
misconfigured backup of `.env` itself, a mistake - can also decrypt every
past backup. Asymmetric GPG fixes this structurally: `scripts/backup.py`
only ever needs the **public** key (safe to keep on this machine, even
safe to commit to the repo - it can encrypt but not decrypt anything).
The **private** key, which is the only thing that can actually decrypt a
backup, lives offline and is only ever imported onto a machine
temporarily, during an actual recovery.

## The key

- **Key ID**: `8126E99EB6C21688`
- **Fingerprint**: `0159 D098 53A9 5BE3 00AF F901 8126 E99E B6C2 1688`
- **Identity**: `Burns OS Backups <backups@burns-os.local>`
- **Public key**: `deploy/keys/burns_os_backups_public.asc` (committed to
  the repo - this is safe, it cannot decrypt anything)
- **Private key**: `burns_os_backups_private_OFFLINE_ONLY.asc` -
  **NOT in this repo, NOT on this machine's disk day-to-day.** Handed to
  Mohit directly; must be moved to durable offline storage (a password
  manager's file-attachment feature, an encrypted USB drive kept
  physically separate, or similar) and then deleted from wherever it was
  first received. The private key itself is passphrase-protected (a
  second secret, also only in that same offline location) - losing
  either one makes past backups unrecoverable.

**Action needed from Mohit**: move the private key export to real offline
storage now, if you haven't already - it is currently only wherever this
session handed it to you.

## Day-to-day: this machine only ever needs the public key

```powershell
gpg --import deploy\keys\burns_os_backups_public.asc
```

`scripts/backup.py` uses this to encrypt (`BACKUP_GPG_RECIPIENT` in
`.env`, set to `backups@burns-os.local`). No private key, no passphrase,
nothing decrypt-capable is ever needed for a normal backup run.

## Recovery procedure (tested live, 2026-09-29)

1. Retrieve the private key export from offline storage.
2. Import it (you'll be asked for its own passphrase - the one stored
   alongside it, not `BACKUP_GPG_RECIPIENT`):
   ```powershell
   gpg --import burns_os_backups_private_OFFLINE_ONLY.asc
   ```
3. Restore a specific archive into a **fresh, separate** database (never
   overwrites the live `burns_os`):
   ```powershell
   .venv\Scripts\python.exe scripts\restore.py backups\burns_os_<timestamp>.dump.gpg
   ```
   This verifies the archive's SHA-256 checksum first (refuses a
   tampered/corrupted file), decrypts, restores into
   `burns_os_restore_test_<timestamp>`, and runs `core.ledger.verify_chain()`
   against the restored data - it only reports success if the restored
   ledger's hash chain actually checks out, not just that `pg_restore`
   exited zero.
4. **Remove the private key again** once you're done - it should not sit
   on a live machine's keyring between recoveries:
   ```powershell
   gpg --delete-secret-keys backups@burns-os.local
   ```

### If GPG hangs during import or decrypt (Windows)

Found live during testing: a stale `gpg-agent` process can make `gpg
--decrypt`/`gpg --import` for a passphrase-protected key hang
indefinitely with no error, even with `--pinentry-mode loopback
--passphrase` supplied. Fix:
```powershell
gpgconf --kill gpg-agent
```
then retry the same command - a fresh agent starts automatically. This
is a real GPG/Windows quirk, not specific to Burns OS's own scripts.

## What `make backup` actually does

`scripts/backup.py` (`make backup`):
1. `pg_dump -Fc` the live `burns_os` database (via `docker exec` into the
   `postgres` container, as `burns_admin`).
2. Copies the current ledger anchor file out of the `ledger_anchor_data`
   Docker volume.
3. GPG-encrypts both to the public key above.
4. Writes a `.sha256` checksum sidecar next to each encrypted file.
5. Prunes local backups older than `BACKUP_RETENTION_DAYS` (default 14).
6. Copies this run's files to `BACKUP_OFFSITE_DIR`.

**`BACKUP_OFFSITE_DIR` is not actually offsite yet** - it defaults to a
second folder next to this repo on the SAME machine, which provides zero
protection if this machine's disk fails. This is explicitly still open -
see "Offsite: rclone (pending Mohit's setup)" below.

Not yet wired to a real nightly cron/scheduler - same status
`core/scheduler.py` was in before `scripts/scheduler_loop.py` existed;
that's the natural next step once this whole approach is confirmed.

## Offsite: rclone (pending Mohit's setup)

Mohit's own instruction: rclone offsite sync "needs my setup" - i.e. he
needs to choose and configure a real remote (S3, Backblaze B2, a second
VPS, Google Drive, etc.) before this can be wired in for real. Once a
remote is chosen:

1. `rclone config` (interactive, one-time) to set up the chosen remote.
2. Add a final step to `scripts/backup.py`'s `main()` that runs
   `rclone copy <this run's files> <remote>:<path>` after the local/
   BACKUP_OFFSITE_DIR copy - encrypted files only (the `.gpg` archives,
   never the plaintext dump), so the offsite destination never needs to
   be trusted with plaintext data.
3. Update this document with the chosen remote's name and the retention
   policy on that end (mirroring `BACKUP_RETENTION_DAYS`, or intentionally
   longer, since offsite storage is exactly where you'd want to survive
   this machine being lost entirely).

**Until this is done, a total loss of this machine (disk failure, theft,
fire) means the backups are lost too** - `BACKUP_OFFSITE_DIR` alone does
not protect against that. This is the single biggest open item in the
backup story.

## Evidence (2026-09-29)

- Key generated: `gpg --batch --gen-key` (4096-bit RSA, no expiry).
- `make backup` (`scripts/backup.py`) run live: wrote
  `backups/burns_os_20260929_055658.dump.gpg` (17,390 bytes) and
  `backups/burns_os_20260929_055658_anchor.jsonl.gpg` (864 bytes), each
  with a `.sha256` sidecar.
- Confirmed this machine **cannot** decrypt without the private key:
  `gpg --decrypt` on the archive (private key not in the local keyring)
  failed with `decryption failed: No secret key`.
- Imported the private key from its offline export, re-ran
  `scripts/restore.py backups/burns_os_20260929_055658.dump.gpg`:
  checksum verified, decrypted, restored into
  `burns_os_restore_test_20260929_060006`, and
  `RESTORE_VERIFY_CHAIN_OK=True entries=99` - a real, intact restore, not
  just a non-zero exit code.
- Removed the private key from the local keyring again afterward and
  confirmed it's gone (`gpg --list-secret-keys` -> "No secret key").
