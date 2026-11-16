# Encrypted offline backups

`scripts/encrypted_backup.py` wraps an **already materialized, private, SQLite-consistent** directory produced by `scripts/bootstrap_config.py` in authenticated encryption. It does not back up a live database itself, restore anything, delete plaintext, install dependencies, start services, contact providers, or change deployment configuration.

This is an offline utility, not evidence of a successful deployment or recovery rehearsal. Keep the original backup until its encrypted copy has been verified and your own retention/recovery procedure permits removing it. The utility never performs that removal.

## Dependency and invocation

Python 3.10+ and an installed, reviewed `cryptography` package providing `AESGCM`, `Scrypt`, and `HKDF` are required for cryptographic operations. The isolated tests were run with **cryptography 50.0.2** on Windows/Python 3.10.11. No package was installed as part of this work. Host dependencies are separate from container dependencies; adding a container pin alone does not install the host package.

Run the script from a trusted checkout, not from inside a directory containing untrusted backup files. `-I` prevents Python from importing planted modules from the working directory. Use absolute paths for every backup/destination argument.

```bash
python3 -I /trusted/DriveTheater/scripts/encrypted_backup.py --help
```

```bash
python3 -I /trusted/DriveTheater/scripts/encrypted_backup.py encrypt /private/backups/SNAPSHOT /private/offline/SNAPSHOT.dtb
```

```bash
python3 -I /trusted/DriveTheater/scripts/encrypted_backup.py verify /private/offline/SNAPSHOT.dtb
```

For an installed server, the management wrapper offers `encrypt-backup BACKUP_DIR ARCHIVE` and `verify-backup ARCHIVE`; use the absolute directory printed by `manage.sh backup`. The wrapper does not install a missing host cryptography package. Keep both the plaintext snapshot and independent passphrase private; encrypted output never makes the original plaintext disappear.

`encrypt` prompts twice with `getpass`; `verify` prompts once. Hidden terminal input is required: the CLI refuses getpass's insecure fallback to echoing standard input. There is deliberately **no passphrase argument, environment variable, password file, or shell substitution**. Unknown arguments are rejected without echoing their values. `--help` and importing the module use only the standard library; unavailable cryptography produces a clear error before a password prompt.

Use an independent, randomly generated backup passphrase with at least 12 characters (maximum 1024); longer high-entropy passphrases are recommended. Store it in an independent password manager/offline recovery record. The utility rejects exact reuse of the materialized `DT_SECRET` or bootstrap `DT_ADMIN_PASSWORD`, but cannot detect reuse of every other account password. Losing the passphrase makes the encrypted archive unrecoverable; there is no recovery backdoor.

The destination must be outside the source directory, in an existing directory. It is created exclusively with mode `0600` and is never overwritten, including in a destination creation race. Source backups require a private directory (`0700`) and no group/other access to files (`0600` recommended). Windows POSIX mode bits do not enforce Windows ACL privacy: secure the source, destination and OS temporary-directory ACLs separately. Symbolic links, junctions and other reparse points are refused in paths, as are multi-link regular files.

On success the CLI prints a small JSON summary (revision, member names, byte counts, database integrity and operation status), never configuration, credentials, passphrases or decrypted database rows. Importable functions return the same summary as a dictionary:

```python
encrypt_backup(backup_dir: Path, destination: Path, passphrase: str) -> dict
verify_backup(archive: Path, passphrase: str) -> dict
```

The script can be loaded with `importlib.util.spec_from_file_location` without modifying `sys.path` or registering it in `sys.modules`. Invalid manifests/archives, unsafe paths, authentication failures or bad passwords raise `ValueError`; missing cryptography raises `RuntimeError`; filesystem failures may raise `OSError`. The CLI catches these and returns a nonzero status. `verify_backup` has no extraction destination and never restores or returns plaintext file contents.

## Exact plaintext backup contract

The accepted directory contains **only** these ordinary, private, single-link files:

- `manifest.json` and `install.env` (required).
- `drivetheater.sqlite3` (optional, a standalone consistent SQLite snapshot).
- `config.tar.gz` (optional, a bounded gzip-compressed credential tar).

No directories, cache/media, marker files, SQLite journal/WAL/SHM sidecars, extra files, or missing/unlisted members are accepted. If a snapshot contains SQLite sidecars, the backup producer must finish materializing/closing the snapshot correctly; do not simply remove WAL files, which may contain committed data. This encryptor neither reads live runtime paths nor repairs incomplete snapshots.

`manifest.json` must be bounded UTF-8 JSON with exactly these keys, matching the bootstrap backup's existing format (it has no `schema` key):

```json
{
  "revision": "revision-or-unknown",
  "files": {
    "install.env": "64-character-lowercase-sha256",
    "drivetheater.sqlite3": "64-character-lowercase-sha256",
    "config.tar.gz": "64-character-lowercase-sha256"
  },
  "media_included": false
}
```

The hash strings above are placeholders, not valid hashes. `files` includes the SHA-256 of the exact bytes of every present data member, not the manifest itself; omit optional keys when their files are absent. `revision` is a nonempty string of at most 200 characters without ASCII control characters. Duplicate JSON fields, unsupported keys, non-lowercase hashes, `media_included` values other than boolean `false`, and member/hash mismatches fail closed.

Within `config.tar.gz`, only regular entries named `config/<name>.json` or `artwork/<sha256>.png` are accepted. Credential `<name>` is 1–128 lowercase ASCII letters/digits/underscores/hyphens and begins with a letter or digit; `<sha256>` is exactly 64 lowercase hexadecimal characters. Windows device names (`con`, `nul`, `aux`, `prn`, `com1`–`com9`, `lpt1`–`lpt9`) are excluded. Normal bootstrap source-ID credential names satisfy this rule. Private, validated custom posters live in `artwork/`, not the disposable media cache. JSON credential entries remain limited to 1 MiB; an artwork entry is limited to 5 MiB. Other nested paths/extensions, absolute paths, traversal, backslashes, duplicate names, symbolic links, hardlinks, directories, devices, FIFOs, sparse entries and unsupported PAX fields are refused. Harmless PAX `path`/time fields are supported, with the effective path still allowlisted. A backup with unusual credential filenames must be reviewed rather than silently skipping those entries.

### Fixed safety limits

| Resource | Limit |
| --- | --- |
| Top-level members, including manifest | 4 |
| Manifest | 64 KiB |
| Each data member | 64 MiB |
| Total plaintext member contents | 128 MiB |
| Uncompressed outer tar payload | 129 MiB |
| Public encrypted-envelope JSON | 4 KiB |
| Configuration entries | 1024 |
| Each configuration/artwork entry | 5 MiB maximum; JSON credential entries remain 1 MiB |
| Total configuration entry contents | 32 MiB |
| Expanded configuration tar, including header allowance | 32 MiB + `(1024 * 6 + 20) * 512` bytes |
| SQLite integrity work | Progress-handler abort after 100 million VM instructions |

File kinds/link counts/counts/sizes are checked before reading source contents. Tar metadata for the entire bounded archive is checked before reading individual member contents. Gzip expansion is separately bounded, and hidden nonzero trailing tar payloads are rejected. The implementation uses bounded in-memory AES-GCM, not streaming encryption: peak memory can be several times the plaintext limit plus Scrypt's memory. Archives exceeding these limits need a separately designed format, not raised constants on untrusted input.

## Version 1 envelope and verification

The binary format is:

1. Literal bytes `DriveTheaterBackup\x00\x01`.
2. Four-byte unsigned big-endian JSON header length.
3. UTF-8 JSON header with exactly `schema`, `cipher`, `kdf`, `salt`, `nonce`, `payload`, `payload_size`.
4. AES-256-GCM ciphertext of an uncompressed tar containing the allowlisted backup files, followed by its 16-byte authentication tag.

The header uses `schema: 1`, `cipher: "AES-256-GCM"`, `payload: "tar"`, canonical base64 of a random 16-byte salt and random 12-byte nonce, and the exact plaintext tar byte length. Its `kdf` object is exactly `{"name":"scrypt","n":32768,"r":8,"p":1,"length":32}`. Parameters are fixed and validated before deriving a key; an archive cannot request an unbounded KDF cost. Scrypt requires approximately 32 MiB plus implementation overhead. A future parameter change needs a deliberately supported format version.

The magic, length and entire header are authenticated as AES-GCM associated data. Salt, nonce, sizes and format/KDF metadata are public; revision, filenames, hashes, database and configuration are inside the ciphertext. Random salts/nonces produce different ciphertext each time. Plaintext hashes detect member inconsistency after authentication; they are not a substitute for the GCM tag.

`verify_backup` first validates bounded public metadata and authenticates **all ciphertext before parsing any private tar/manifest/config payload or writing a temporary database**. Wrong password and tampering use the same authentication-failure message. It validates member names/types/counts/sizes, hashes and configuration archive constraints, then copies only the standalone database into a new private OS temporary directory. SQLite is opened with `mode=ro&immutable=1`, `query_only=ON`, `trusted_schema=OFF`, and bounded `integrity_check(1)` work. The connection is explicitly closed and the temporary copy/directory are cleaned on success and ordinary exceptions. No archive entry is extracted with `extract`/`extractall`.

Verification establishes authentication, internal consistency and offline SQLite integrity only. It does not prove app-level recoverability, current provider access, usable Google credentials, real deployment configuration, or a working restore. No live restore/rehearsal was run.

## Small application-secret API

`backend/drivetheater/crypto_utils.py` separately exposes:

```python
seal_secret(value: str, master_secret: str, purpose: str) -> str
open_secret(token: str, master_secret: str, purpose: str) -> str
```

These are for small application secrets such as TOTP enrollment material, not human-password storage or backup encryption. Callers pass a **random installation master secret** every time; no global key/secret is retained. A purpose-specific 32-byte key is derived with cryptography's HKDF-SHA256 using salt `DriveTheater/HKDF-SHA256/sealed-secret/v1` and info `DriveTheater/sealed-secret/v1\x00` followed by the ASCII purpose. That same context is GCM associated data. No homemade cipher is used.

- Master secret: 32–4096 characters; the configured `development-only-not-for-production-0000` fallback and all `development-only-` prefixed values are rejected.
- Purpose: 1–128 ASCII letters/digits or `_ . : / -`; callers must use stable, distinct purpose labels for distinct uses.
- Value: at most 64 KiB of valid UTF-8 (including an empty string).
- Token: `dtsec1.` followed by canonical padded URL-safe base64 of a random 12-byte nonce plus AES-GCM ciphertext/tag. Strict length/base64/version checks apply.
- Wrong key, wrong purpose, malformed encoding, invalid size or tampering: `ValueError`. Missing cryptography: `RuntimeError`.

Changing/losing the master secret or changing a purpose label prevents opening existing tokens. A calling feature must design explicit key rotation and account recovery; these helpers do not silently fall back to plaintext/development keys. Backup passphrases use Scrypt and are independent from these HKDF-derived application keys.

## Testing and limitations

Run only disposable, offline fixtures:

```bash
python3 -I -B /trusted/DriveTheater/tests/test_encrypted_backup.py -v
```

Tests cover secret roundtrip/domain separation/development-secret refusal/size bounds, encrypted roundtrip, independent/wrong passwords, salt/nonce/header/ciphertext tampering, fixed KDF parameters, strict manifests and hashes, unsafe authenticated outer archives and configuration archives, gzip/tar limits, hardlinks, destination exclusivity/races, authentication before any payload parse/temp writes, read-only SQLite copies/cleanup, unavailable dependency/help behavior, prompt-only CLI input, and no mutation of a disposable runtime using a real bootstrap-format fixture. Real symlink/POSIX-mode tests can skip on Windows; this is reported rather than claiming host-level validation.

Python cannot reliably erase immutable passphrase/plaintext objects from memory. OS crash, forced termination, swap, hibernation, crash dumps or filesystem snapshots can retain plaintext/temporary data; use an appropriately protected host and encrypted temporary storage where required. Link checks and `O_NOFOLLOW` (where supported) do not replace trusted parent-directory ownership: do not run against trees concurrently writable by an adversarial local user. Windows ACL enforcement, real Ubuntu permissions, deployment, backup transport/retention and full recovery rehearsals are outside the isolated tests.
