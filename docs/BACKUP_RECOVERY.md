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

**Action needed from Mohit** - the private key still only exists at
`C:\gpgburns\burns_os_backups_private_OFFLINE_ONLY.asc` on this machine
(plus its passphrase, in plaintext, in `C:\gpgburns\keygen.batch`). Move
both into real offline storage:

1. **Bitwarden Secure Note** (recommended primary copy):
   - In PowerShell, copy the armored key straight to your clipboard
     without ever printing it to screen:
     ```powershell
     Get-Content C:\gpgburns\burns_os_backups_private_OFFLINE_ONLY.asc -Raw | Set-Clipboard
     ```
   - In Bitwarden: New item -> Secure Note -> name it e.g. "Burns OS - GPG
     Backup Private Key (OFFLINE)" -> paste (Ctrl+V) into the note body -> Save.
   - Copy the passphrase the same way (never re-type it from memory - a
     transcription error here makes the key permanently useless):
     ```powershell
     (Get-Content C:\gpgburns\keygen.batch | Select-String "^Passphrase:").ToString().Split(":",2)[1].Trim() | Set-Clipboard
     ```
   - Add it to the same Secure Note as a second field (Bitwarden's "Add
     Field" -> Hidden type), clearly labeled, or a second Secure Note -
     either way, keep it findable next to the key, not separately lost.

2. **USB copy** (secondary/offline-offline copy): plug in a USB drive
   ideally protected with BitLocker To Go, then:
   ```powershell
   Copy-Item C:\gpgburns\burns_os_backups_private_OFFLINE_ONLY.asc E:\burns_os_backups\
   Copy-Item C:\gpgburns\keygen.batch E:\burns_os_backups\   # has the passphrase
   ```
   (adjust `E:\` to your drive letter). Eject safely, store it physically
   separate from this machine.

3. **Verify the ROUND TRIP actually works** (not just that the original
   file on disk works - a copy-paste through a note-taking app is a real
   place text can get mangled): open the Bitwarden note again, copy the
   body back out to a fresh file, e.g. `C:\gpgburns\verify_from_bitwarden.asc`,
   then:
   ```powershell
   $env:GNUPGHOME = "C:\gpg_verify_bitwarden"
   New-Item -ItemType Directory -Force $env:GNUPGHOME | Out-Null
   gpg --batch --yes --import C:\gpgburns\verify_from_bitwarden.asc
   ```
   Tell me once you've done this and I'll run the same restore-test
   procedure below against that exact file and report the result, then
   clean up `$env:GNUPGHOME` and the verify file.

4. **Only once both copies exist and the round-trip verifies**, tell me
   and I'll delete `C:\gpgburns\burns_os_backups_private_OFFLINE_ONLY.asc`
   and `C:\gpgburns\keygen.batch` from this machine (there is no other
   copy anywhere else on this machine - confirmed 2026-09-29, see
   "Evidence" below) and confirm the deletion.

**Dress rehearsal already done (2026-09-29), proving the mechanism itself
is sound**: simulated "a fresh copy of the note text" two ways - a
byte-identical copy, and a CRLF-line-ending-mangled copy (the realistic
Windows-clipboard/note-app corruption risk) - imported each into a
brand-new, never-before-used GNUPGHOME, and ran the real, unmodified
`scripts/restore.py` against a live backup using the CRLF-safe import.
Result: **`RESTORE_VERIFY_CHAIN_OK=True entries=103`** - a full, real
recovery using a key that had been copied as plain text, not the
original file. This does not replace step 3 above (it doesn't touch your
actual Bitwarden vault), but it does confirm GPG's ASCII-armor format is
robust to the specific corruption a copy-paste through a plain-text note
field could realistically introduce.

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
- **2026-09-29, full-machine sweep for stray copies**: searched the whole
  user profile for any other copy of the private key or its passphrase
  file. Found and deleted one stray leftover `keygen.batch` (same
  plaintext passphrase, no private key material) in an earlier scratch
  directory from the first key-generation attempt that failed on a too-
  long path. Checked the Windows Recycle Bin (327 items) for either
  filename - no matches. Checked the default GPG keyring
  (`gpgconf --list-dirs homedir` -> `C:\Users\91813\.gnupg`) -
  `gpg --list-secret-keys` there is empty, confirming the day-to-day
  keyring never held the private key. Only `wsl -l -v` distro is Docker
  Desktop's own internal VM (never used to run gpg directly) - no WSL
  copy exists. **The only remaining copy on this machine right now is
  `C:\gpgburns\burns_os_backups_private_OFFLINE_ONLY.asc` +
  `C:\gpgburns\keygen.batch`**, pending the Bitwarden/USB handover above.
- **2026-09-29, dress rehearsal**: `RESTORE_VERIFY_CHAIN_OK=True
  entries=103` using a key re-imported from a plain-text copy (including
  a deliberately CRLF-mangled one) in a brand-new GNUPGHOME - see the
  "Action needed from Mohit" section above for the full test description.
