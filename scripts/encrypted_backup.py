#!/usr/bin/env python3
"""Encrypt or read-only verify an existing private bootstrap backup.

Standalone: only the two operations import cryptography; --help needs stdlib only.
Never reads live runtime paths from install.env, extracts into the runtime, deletes
plaintext backups, or accepts passphrases in arguments/environment variables.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Callable
from contextlib import closing
import getpass
import gzip
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import sys
import tarfile
import tempfile
import warnings
import zlib

MAGIC = b"DriveTheaterBackup\x00\x01"
MAX_HEADER_BYTES = 4096
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_BACKUP_BYTES = 128 * 1024 * 1024
MAX_PAYLOAD_BYTES = MAX_BACKUP_BYTES + 1024 * 1024
MAX_ARCHIVE_BYTES = len(MAGIC) + 4 + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES + 16
MAX_CONFIG_MEMBERS = 1024
MAX_CONFIG_FILE_BYTES = 5 * 1024 * 1024
MAX_CONFIG_BYTES = 32 * 1024 * 1024
MAX_CONFIG_TAR_BYTES = MAX_CONFIG_BYTES + (MAX_CONFIG_MEMBERS * 6 + 20) * 512
MEMBERS = frozenset({"manifest.json", "install.env", "drivetheater.sqlite3", "config.tar.gz"})
DATA_MEMBERS = MEMBERS - {"manifest.json"}
KDF_PARAMETERS = {"name": "scrypt", "n": 32768, "r": 8, "p": 1, "length": 32}


def _crypto():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise RuntimeError(
            "Encrypted backups require an installed cryptography package with AESGCM and Scrypt; "
            "no dependencies were installed"
        ) from exc
    return AESGCM, Scrypt, InvalidTag


def _passphrase(value: str) -> bytes:
    if not isinstance(value, str) or not 12 <= len(value) <= 1024:
        raise ValueError("Use an independent backup passphrase of 12..1024 characters")
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Backup passphrase must be valid UTF-8") from exc


def _independent(password: bytes, env: bytes) -> None:
    # Treat dotenv only as data. Never resolve or open any paths in it.
    for line in env.splitlines():
        name, separator, value = line.partition(b"=")
        if separator and name in (b"DT_SECRET", b"DT_ADMIN_PASSWORD") and hmac.compare_digest(password, value):
            raise ValueError("Backup passphrase must differ from installation secrets/passwords")


def _linked(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _path(value: Path, label: str, *, output: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ValueError(label + " must be an absolute, non-root path without traversal")
    # Includes Windows junctions/reparse points, not only symbolic links.
    for item in reversed((path.parent if output else path, *(path.parent if output else path).parents)):
        if _linked(item.lstat()):
            raise ValueError(label + " must not contain symlinks or reparse points")
    return path


def _regular(info, label: str, limit: int) -> None:
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(label + " must be a regular, single-link file")
    if os.name == "posix" and info.st_mode & 0o077:
        raise ValueError(label + " must be private (mode 600)")
    if info.st_size < 0 or info.st_size > limit:
        raise ValueError(label + " exceeds the size limit")


def _identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_file(path: Path, label: str, limit: int, expected=None) -> bytes:
    before = path.lstat()
    _regular(before, label, limit)
    if expected is not None and _identity(before) != _identity(expected):
        raise ValueError(label + " changed during validation")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened, label, limit)
        if _identity(opened) != _identity(before):
            raise ValueError(label + " changed during validation")
        contents = stream.read(opened.st_size + 1)
        if len(contents) != opened.st_size or _identity(os.fstat(stream.fileno())) != _identity(opened):
            raise ValueError(label + " changed while reading")
    if _identity(path.lstat()) != _identity(before):
        raise ValueError(label + " changed while reading")
    return contents


def _write_new(path: Path, contents: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # Only remove our own incomplete output, never an existing/replaced file.
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except OSError:
            pass
        raise


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields are not allowed")
        result[key] = value
    return result


def _json(contents: bytes, label: str) -> dict:
    try:
        value = json.loads(contents.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(label + " must contain bounded UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(label + " must be a JSON object")
    return value


def _manifest(contents: bytes) -> dict:
    if len(contents) > MAX_MANIFEST_BYTES:
        raise ValueError("Backup manifest exceeds the size limit")
    manifest = _json(contents, "Backup manifest")
    if set(manifest) != {"revision", "files", "media_included"} or manifest["media_included"] is not False:
        raise ValueError("Unsupported backup manifest; media/cache must not be included")
    revision, files = manifest["revision"], manifest["files"]
    if not isinstance(revision, str) or not 1 <= len(revision) <= 200 or any(ord(c) < 32 for c in revision):
        raise ValueError("Invalid backup revision")
    if not isinstance(files, dict) or "install.env" not in files or not set(files) <= DATA_MEMBERS:
        raise ValueError("Backup manifest contains missing or unsupported members")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in files.values()):
        raise ValueError("Backup manifest must contain lowercase SHA-256 hashes")
    return manifest


def _tar_members(raw: bytes, allowed, count_limit: int, member_limit: int | Callable[[str], int], total_limit: int):
    """Validate ALL metadata before returning any member contents; never extract."""
    if len(raw) % 512:
        raise ValueError("Invalid tar block alignment")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members, names, total = [], set(), 0
            for member in archive:
                if len(members) >= count_limit:
                    raise ValueError("Archive member count exceeds the limit")
                if (
                    not allowed(member.name) or member.name in names
                    or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                    or member.linkname or member.sparse is not None
                    or set(member.pax_headers) - {"path", "mtime", "atime", "ctime"}
                ):
                    raise ValueError("Archive contains an unsafe or unsupported member")
                limit = member_limit(member.name) if callable(member_limit) else member_limit
                if not 0 <= member.size <= limit:
                    raise ValueError("Archive member exceeds the size limit")
                total += member.size
                if total > total_limit:
                    raise ValueError("Archive total exceeds the size limit")
                names.add(member.name)
                members.append(member)
            # tarfile normally ignores bytes after the first end-of-archive block.
            # Do not allow hidden second archives or nonzero trailing payloads.
            end = archive.offset
            if len(raw) < end + 1024 or any(raw[end:]):
                raise ValueError("Archive has a missing terminator or unexpected trailing data")
            contents = {}
            for member in members:
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Archive member cannot be read")
                with stream:
                    contents[member.name] = stream.read(member.size + 1)
                if len(contents[member.name]) != member.size:
                    raise ValueError("Archive member is truncated")
            return contents
    except (tarfile.TarError, EOFError, OverflowError) as exc:
        raise ValueError("Invalid backup tar archive") from exc


def _config_name(name: str) -> bool:
    if re.fullmatch(r'artwork/[a-f0-9]{64}\.png', name):
        return True
    match = re.fullmatch(r"config/([a-z0-9][a-z0-9_-]{0,127})\.json", name)
    return bool(match) and not re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", match[1])


def _check_config(contents: bytes) -> int:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(contents), mode="rb") as compressed:
            raw = compressed.read(MAX_CONFIG_TAR_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("Invalid compressed configuration archive") from exc
    if len(raw) > MAX_CONFIG_TAR_BYTES:
        raise ValueError("Configuration archive exceeds the expanded size limit")
    members = _tar_members(raw, _config_name, MAX_CONFIG_MEMBERS, MAX_CONFIG_FILE_BYTES, MAX_CONFIG_BYTES)
    if any(name.startswith('config/') and len(body) > 1024 * 1024 for name, body in members.items()):
        raise ValueError('Configuration JSON file exceeds its size limit')
    return len(members)


def _check_database(contents: bytes) -> None:
    if not contents.startswith(b"SQLite format 3\x00"):
        raise ValueError("Backup database is not a materialized SQLite snapshot")
    # Never attach the original, live database or trust any paths from its rows.
    # immutable=1 avoids even journal/SHM side effects on this disposable copy.
    with tempfile.TemporaryDirectory(prefix="drivetheater-backup-check-") as folder:
        path = Path(folder) / "snapshot.sqlite3"
        _write_new(path, contents)
        calls = 0

        def budget():
            nonlocal calls
            calls += 1
            return int(calls > 100000)

        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                db.set_progress_handler(budget, 1000)
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA trusted_schema=OFF")
                if db.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
                    raise ValueError("Backup database integrity check failed")
        except sqlite3.Error as exc:
            raise ValueError("Backup database integrity check failed") from exc


def _validate_files(files: dict[str, bytes], password: bytes) -> dict:
    if "manifest.json" not in files or not set(files) <= MEMBERS:
        raise ValueError("Backup contains missing or unsupported members")
    if any(len(contents) > (MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES)
           for name, contents in files.items()) or sum(map(len, files.values())) > MAX_BACKUP_BYTES:
        raise ValueError("Backup contents exceed size limits")
    manifest = _manifest(files["manifest.json"])
    if set(files) != {"manifest.json", *manifest["files"]}:
        raise ValueError("Backup members do not match the manifest")
    for name, expected in manifest["files"].items():
        if not hmac.compare_digest(hashlib.sha256(files[name]).hexdigest(), expected):
            raise ValueError("Backup member SHA-256 does not match the manifest")
    _independent(password, files["install.env"])
    config_members = _check_config(files["config.tar.gz"]) if "config.tar.gz" in files else 0
    if "drivetheater.sqlite3" in files:
        _check_database(files["drivetheater.sqlite3"])
    return {
        "format": "drivetheater-encrypted-backup", "schema": 1,
        "revision": manifest["revision"], "files": sorted(files), "media_included": False,
        "database_integrity": "ok" if "drivetheater.sqlite3" in files else "not-present",
        "config_members": config_members, "plaintext_bytes": sum(map(len, files.values())),
    }


def _read_backup(backup_dir: Path) -> dict[str, bytes]:
    root = _path(backup_dir, "Backup directory")
    before = root.lstat()
    if not stat.S_ISDIR(before.st_mode) or (os.name == "posix" and before.st_mode & 0o077):
        raise ValueError("Backup directory must be private (mode 700)")
    entries, total = {}, 0
    with os.scandir(root) as listing:
        for entry in listing:
            if len(entries) >= len(MEMBERS) or entry.name not in MEMBERS:
                raise ValueError("Backup directory contains an unsupported member")
            # DirEntry.stat() on Windows can report st_nlink/st_ino as zero;
            # lstat performs the full handle-based metadata query we require.
            info = (root / entry.name).lstat()
            _regular(info, "Backup member", MAX_MANIFEST_BYTES if entry.name == "manifest.json" else MAX_FILE_BYTES)
            total += info.st_size
            if total > MAX_BACKUP_BYTES:
                raise ValueError("Backup total exceeds the size limit")
            entries[entry.name] = info
    if not {"manifest.json", "install.env"} <= set(entries):
        raise ValueError("Backup directory is missing required members")
    manifest_bytes = _read_file(root / "manifest.json", "Backup manifest", MAX_MANIFEST_BYTES, entries["manifest.json"])
    manifest = _manifest(manifest_bytes)
    if set(entries) != {"manifest.json", *manifest["files"]}:
        raise ValueError("Backup directory does not match the manifest")
    files = {"manifest.json": manifest_bytes}
    for name in sorted(manifest["files"]):
        files[name] = _read_file(root / name, "Backup member", MAX_FILE_BYTES, entries[name])
    if _identity(root.lstat()) != _identity(before):
        raise ValueError("Backup directory changed while reading")
    return files


def _pack(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(files):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(files[name]), 0o600
            archive.addfile(member, io.BytesIO(files[name]))
    contents = stream.getvalue()
    if len(contents) > MAX_PAYLOAD_BYTES:
        raise ValueError("Backup payload exceeds the size limit")
    return contents


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value, length: int) -> bytes:
    if not isinstance(value, str) or len(value) != 4 * ((length + 2) // 3):
        raise ValueError("Invalid encrypted backup metadata")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid encrypted backup metadata") from exc
    if len(raw) != length or _encode(raw) != value:
        raise ValueError("Invalid encrypted backup metadata")
    return raw


def _metadata(contents: bytes) -> tuple[dict, bytes, bytes, bytes]:
    prefix_size = len(MAGIC) + 4
    if not contents.startswith(MAGIC) or len(contents) < prefix_size:
        raise ValueError("Unsupported or truncated encrypted backup")
    length = struct.unpack(">I", contents[len(MAGIC):prefix_size])[0]
    if not 1 <= length <= MAX_HEADER_BYTES or len(contents) < prefix_size + length + 16:
        raise ValueError("Invalid encrypted backup header length")
    end = prefix_size + length
    header = _json(contents[prefix_size:end], "Encrypted backup header")
    if (
        set(header) != {"schema", "cipher", "kdf", "salt", "nonce", "payload", "payload_size"}
        or type(header["schema"]) is not int or header["schema"] != 1
        or header["cipher"] != "AES-256-GCM" or header["payload"] != "tar"
        or not isinstance(header["kdf"], dict) or header["kdf"] != KDF_PARAMETERS
        or any(type(header["kdf"].get(key)) is not int for key in ("n", "r", "p", "length"))
        or type(header["payload_size"]) is not int
        or not 1024 <= header["payload_size"] <= MAX_PAYLOAD_BYTES
        or len(contents) != end + header["payload_size"] + 16
    ):
        raise ValueError("Unsupported encrypted backup parameters or size")
    return header, _decode(header["salt"], 16), _decode(header["nonce"], 12), contents[:end]


def encrypt_backup(backup_dir: Path, destination: Path, passphrase: str) -> dict:
    """Encrypt an already-materialized backup to a new mode-600 file; never overwrite."""
    password = _passphrase(passphrase)
    aesgcm, scrypt, _ = _crypto()
    source = _path(backup_dir, "Backup directory")
    output = _path(destination, "Encrypted backup destination", output=True)
    if not output.parent.is_dir() or source == output or source in output.parents:
        raise ValueError("Destination must be outside the backup directory, in an existing directory")
    if output.exists() or output.is_symlink():
        raise ValueError("Encrypted backup destination already exists")
    files = _read_backup(source)
    summary = _validate_files(files, password)
    payload = _pack(files)
    salt, nonce = os.urandom(16), os.urandom(12)
    metadata = {
        "schema": 1, "cipher": "AES-256-GCM", "kdf": dict(KDF_PARAMETERS),
        "salt": _encode(salt), "nonce": _encode(nonce), "payload": "tar", "payload_size": len(payload),
    }
    header = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("ascii")
    aad = MAGIC + struct.pack(">I", len(header)) + header
    key = scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password)
    contents = aad + aesgcm(key).encrypt(nonce, payload, aad)
    _write_new(output, contents)
    return {**summary, "encrypted": True, "archive_bytes": len(contents)}


def verify_backup(archive: Path, passphrase: str) -> dict:
    """Authenticate, validate hashes/archives and integrity-check an isolated DB copy.

    No plaintext files are returned or restored. The sole temporary DB copy and
    its private temporary directory are cleaned on both success and failure.
    """
    password = _passphrase(passphrase)
    aesgcm, scrypt, invalid_tag = _crypto()
    path = _path(archive, "Encrypted backup")
    contents = _read_file(path, "Encrypted backup", MAX_ARCHIVE_BYTES)
    header, salt, nonce, aad = _metadata(contents)
    key = scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(password)
    try:
        payload = aesgcm(key).decrypt(nonce, contents[len(aad):], aad)
    except invalid_tag as exc:
        raise ValueError("Encrypted backup authentication failed (wrong passphrase or damaged archive)") from exc
    # Authentication MUST precede tar/manifest/config parsing or any temp writes.
    if len(payload) != header["payload_size"]:
        raise ValueError("Invalid authenticated backup payload size")
    files = _tar_members(
        payload, lambda name: name in MEMBERS, len(MEMBERS),
        lambda name: MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES,
        MAX_BACKUP_BYTES,
    )
    summary = _validate_files(files, password)
    return {**summary, "verified": True, "archive_bytes": len(contents)}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help. Enter passphrases only at the hidden prompt.\n")


def _prompt(label: str) -> str:
    # getpass otherwise falls back to potentially echoing stdin without a TTY.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(label)
        except getpass.GetPassWarning as exc:
            raise ValueError("A terminal with hidden passphrase input is required") from exc


def main(argv=None) -> int:
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    encrypt = commands.add_parser("encrypt", help="Encrypt an existing private bootstrap backup directory")
    encrypt.add_argument("backup_dir", type=Path)
    encrypt.add_argument("destination", type=Path)
    verify = commands.add_parser("verify", help="Read-only authentication, manifest and SQLite verification")
    verify.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    try:
        # Check availability before prompting, without importing crypto for --help.
        _crypto()
        password = _prompt("Independent backup passphrase (not stored): ")
        if args.command == "encrypt":
            confirmation = _prompt("Confirm backup passphrase: ")
            if not hmac.compare_digest(_passphrase(password), _passphrase(confirmation)):
                raise ValueError("Backup passphrases did not match")
            result = encrypt_backup(args.backup_dir, args.destination, password)
        else:
            result = verify_backup(args.archive, password)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        # Never echo paths, passphrases, dotenv, SQL or credential contents.
        if isinstance(exc, (ValueError, RuntimeError)):
            print(str(exc), file=sys.stderr)
        else:
            print("Backup operation failed; check paths and private permissions. No runtime data was changed.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Backup operation cancelled. No runtime data was changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
