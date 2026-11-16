"""Offline encryption tests. All application data lives in disposable directories."""
import base64
from contextlib import closing, redirect_stderr, redirect_stdout
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup = load("encrypted_backup_test_subject", "scripts/encrypted_backup.py")
crypto = load("crypto_utils_test_subject", "backend/drivetheater/crypto_utils.py")
PASSPHRASE = "fixture-only long independent passphrase"
MASTER = "fixture-master-secret-not-for-production-0123456789"


def tar_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, contents, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o600
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "../../outside"
            else:
                member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents) if member.isreg() else None)
    return stream.getvalue()


class SecretTests(unittest.TestCase):
    def test_roundtrip_unicode_randomized_nonce_stable_key_no_global_secret(self):
        first = crypto.seal_secret("synthetic TOTP seed \u2603", MASTER, "totp:v1")
        second = crypto.seal_secret("synthetic TOTP seed \u2603", MASTER, "totp:v1")
        self.assertNotEqual(first, second)
        fresh = load("fresh_crypto_utils", "backend/drivetheater/crypto_utils.py")
        self.assertEqual(fresh.open_secret(first, MASTER, "totp:v1"), "synthetic TOTP seed \u2603")
        self.assertTrue(first.startswith("dtsec1."))
        self.assertNotIn("synthetic", first)
        self.assertNotIn(MASTER, vars(crypto).values())

    def test_wrong_master_purpose_and_tampering_rejected(self):
        token = crypto.seal_secret("fixture-only", MASTER, "totp")
        raw = bytearray(base64.urlsafe_b64decode(token.split(".", 1)[1]))
        raw[-1] ^= 1
        corrupt = "dtsec1." + base64.urlsafe_b64encode(raw).decode("ascii")
        for value, master, purpose in (
            (token, MASTER + "x", "totp"), (token, MASTER, "other-purpose"),
            (corrupt, MASTER, "totp"), ("dtsec2." + token[7:], MASTER, "totp"),
        ):
            with self.subTest(value=value[:10], purpose=purpose), self.assertRaises(ValueError):
                crypto.open_secret(value, master, purpose)

    def test_development_short_and_invalid_parameters_rejected(self):
        for master in ("", "x" * 31, "development-only-not-for-production-0000", "development-only-" + "x" * 40, "x" * 4097, None):
            for operation, value in ((crypto.seal_secret, "seed"), (crypto.open_secret, "dtsec1.")):
                with self.subTest(master=bool(master), operation=operation.__name__), self.assertRaises(ValueError):
                    operation(value, master, "totp")
        for purpose in ("", "x" * 129, "invalid\ncontext", "has spaces", "\u2603", None):
            with self.subTest(purpose=purpose), self.assertRaises(ValueError):
                crypto.seal_secret("seed", MASTER, purpose)

    def test_empty_value_and_size_boundaries(self):
        self.assertEqual(crypto.open_secret(crypto.seal_secret("", MASTER, "empty"), MASTER, "empty"), "")
        value = "x" * crypto.MAX_SECRET_BYTES
        token = crypto.seal_secret(value, MASTER, "bounded")
        self.assertEqual(crypto.open_secret(token, MASTER, "bounded"), value)
        for value in ("x" * (crypto.MAX_SECRET_BYTES + 1), "\u2603" * (crypto.MAX_SECRET_BYTES // 3 + 1), "\ud800", 123):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                crypto.seal_secret(value, MASTER, "bounded")

    def test_malformed_base64_and_oversized_tokens(self):
        token = crypto.seal_secret("fixture", MASTER, "totp")
        for value in (None, "", "dtsec1.!", "dtsec1." + "A" * (crypto.MAX_TOKEN_CHARS + 1), token + "=", token + "\n"):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                crypto.open_secret(value, MASTER, "totp")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="drivetheater-encrypted-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "snapshot"
        self.source.mkdir(mode=0o700)
        self.destination = self.root / "snapshot.dtb"
        self.write("install.env", ("DT_SECRET=" + MASTER + "\nDT_ADMIN_PASSWORD=fixture-admin-password\nDT_HOST_DATA_DIR=/never/open/this/path\n").encode())
        with closing(sqlite3.connect(self.source / "drivetheater.sqlite3")) as db:
            db.execute("CREATE TABLE fixture(value TEXT)")
            db.execute("INSERT INTO fixture VALUES ('preserved')")
            db.commit()
        os.chmod(self.source / "drivetheater.sqlite3", 0o600)
        self.write("config.tar.gz", gzip.compress(tar_bytes([("config/abcd1234.json", b'{"fixture":"not-a-real-key"}', tarfile.REGTYPE)])))
        self.manifest()

    def write(self, name, contents):
        path = self.source / name
        path.write_bytes(contents)
        os.chmod(path, 0o600)
        return path

    def manifest(self, **changes):
        value = {
            "revision": "fixture-revision",
            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir() if p.name != "manifest.json"},
            "media_included": False,
        }
        value.update(changes)
        self.write("manifest.json", json.dumps(value).encode())
        return value

    def encrypt(self):
        return backup.encrypt_backup(self.source, self.destination, PASSPHRASE)

    def snapshot(self, folder):
        return {str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}

    def signed_payload(self, payload, destination=None, **changes):
        # Construct authenticated malicious fixtures using the vetted primitives.
        aesgcm, scrypt, _ = backup._crypto()
        salt, nonce = os.urandom(16), os.urandom(12)
        metadata = {"schema": 1, "cipher": "AES-256-GCM", "kdf": dict(backup.KDF_PARAMETERS),
                    "salt": backup._encode(salt), "nonce": backup._encode(nonce), "payload": "tar", "payload_size": len(payload)}
        metadata.update(changes)
        header = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("ascii")
        aad = backup.MAGIC + struct.pack(">I", len(header)) + header
        key = scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(PASSPHRASE.encode())
        destination = destination or self.destination
        destination.write_bytes(aad + aesgcm(key).encrypt(nonce, payload, aad))
        os.chmod(destination, 0o600)
        return destination

    def test_roundtrip_no_plaintext_and_source_unchanged(self):
        before = self.snapshot(self.source)
        encrypted = self.encrypt()
        verified = backup.verify_backup(self.destination, PASSPHRASE)
        self.assertTrue(encrypted["encrypted"])
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["revision"], "fixture-revision")
        self.assertEqual(verified["database_integrity"], "ok")
        self.assertEqual(verified["config_members"], 1)
        self.assertFalse(verified["media_included"])
        self.assertEqual(verified["files"], sorted(backup.MEMBERS))
        contents = self.destination.read_bytes()
        for private in (b"DT_SECRET", MASTER.encode(), b"not-a-real-key", PASSPHRASE.encode()):
            self.assertNotIn(private, contents)
        self.assertEqual(self.snapshot(self.source), before)
        self.assertEqual(set(p.name for p in self.root.iterdir()), {"snapshot", "snapshot.dtb"})
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o600)

    def test_random_salt_and_nonce_on_each_encryption(self):
        self.encrypt()
        second = self.root / "other.dtb"
        backup.encrypt_backup(self.source, second, PASSPHRASE)
        self.assertNotEqual(self.destination.read_bytes(), second.read_bytes())
        self.assertTrue(backup.verify_backup(second, PASSPHRASE)["verified"])

    def test_wrong_passphrase_authenticates_before_payload_parsing_or_temp_files(self):
        self.encrypt()
        with patch.object(backup, "_tar_members") as parse, patch.object(backup.tempfile, "TemporaryDirectory") as temporary:
            with self.assertRaisesRegex(ValueError, "authentication failed"):
                backup.verify_backup(self.destination, "wrong fixture passphrase")
            parse.assert_not_called()
            temporary.assert_not_called()

    def test_ciphertext_header_nonce_salt_tamper_and_truncation(self):
        self.encrypt()
        original = self.destination.read_bytes()
        header, _, _, aad = backup._metadata(original)
        mutations = []
        for offset in (len(aad), len(original) - 1):
            changed = bytearray(original)
            changed[offset] ^= 1
            mutations.append(bytes(changed))
        for field in ("salt", "nonce"):
            value = json.loads(original[len(backup.MAGIC) + 4:len(aad)])
            raw = bytearray(base64.b64decode(value[field]))
            raw[0] ^= 1
            value[field] = backup._encode(bytes(raw))
            altered = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
            mutations.append(backup.MAGIC + struct.pack(">I", len(altered)) + altered + original[len(aad):])
        mutations.extend((original[:-1], original + b"x", b"bad" + original[3:]))
        for index, changed in enumerate(mutations):
            with self.subTest(index=index), patch.object(backup, "_tar_members") as parse:
                self.destination.write_bytes(changed)
                with self.assertRaises(ValueError):
                    backup.verify_backup(self.destination, PASSPHRASE)
                parse.assert_not_called()

    def test_metadata_kdf_bounds_rejected_before_derivation(self):
        self.encrypt()
        original = self.destination.read_bytes()
        _, _, _, aad = backup._metadata(original)
        for parameters in ({**backup.KDF_PARAMETERS, "n": 2**30}, {**backup.KDF_PARAMETERS, "p": True}, {**backup.KDF_PARAMETERS, "length": 64}):
            header = json.loads(original[len(backup.MAGIC) + 4:len(aad)])
            header["kdf"] = parameters
            raw = json.dumps(header).encode("ascii")
            contents = backup.MAGIC + struct.pack(">I", len(raw)) + raw + original[len(aad):]
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                backup._metadata(contents)
        for length in (0, backup.MAX_HEADER_BYTES + 1, 2**32 - 1):
            with self.subTest(length=length), self.assertRaises(ValueError):
                backup._metadata(backup.MAGIC + struct.pack(">I", length) + b"x" * 32)

    def test_short_or_reused_passphrase_refused(self):
        for password in ("short", "x" * 1025, None, MASTER, "fixture-admin-password"):
            with self.subTest(kind=type(password).__name__), self.assertRaises(ValueError):
                backup.encrypt_backup(self.source, self.destination, password)
            self.assertFalse(self.destination.exists())

    def test_existing_destination_no_overwrite(self):
        self.destination.write_bytes(b"preserve this file")
        with self.assertRaises(ValueError):
            self.encrypt()
        self.assertEqual(self.destination.read_bytes(), b"preserve this file")

    def test_exclusive_create_stops_destination_race(self):
        original = backup._write_new

        def competing_writer(path, contents):
            if path == self.destination:
                path.write_bytes(b"competing destination")
            return original(path, contents)

        with patch.object(backup, "_write_new", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                self.encrypt()
        self.assertEqual(self.destination.read_bytes(), b"competing destination")

    def test_source_cannot_be_destination(self):
        for destination in (self.source / "result.dtb", self.source / "install.env"):
            with self.subTest(destination=destination.name), self.assertRaises(ValueError):
                backup.encrypt_backup(self.source, destination, PASSPHRASE)

    def test_unsupported_files_rejected_before_any_private_read(self):
        for name in ("cache", "movie.mp4", "drivetheater.sqlite3-wal", "unlisted.json"):
            extra = self.source / name
            extra.write_bytes(b"not allowed")
            try:
                with patch.object(backup, "_read_file") as read, self.assertRaises(ValueError):
                    self.encrypt()
                read.assert_not_called()
            finally:
                extra.unlink()

    def test_manifest_traversal_absolute_missing_media_hash_and_unknown_fields(self):
        original = json.loads((self.source / "manifest.json").read_bytes())
        bad = [
            {**original, "files": {"../install.env": "0" * 64}},
            {**original, "files": {"/install.env": "0" * 64}},
            {**original, "files": {"install.env": "0" * 64}},
            {**original, "files": {}}, {**original, "media_included": True},
            {**original, "extra": True}, {**original, "revision": "line\nbreak"},
            {**original, "files": {**original["files"], "config.tar.gz": "F" * 64}},
        ]
        for index, value in enumerate(bad):
            self.write("manifest.json", json.dumps(value).encode())
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.encrypt()
            self.assertFalse(self.destination.exists())

    def test_duplicate_json_manifest_fields_rejected(self):
        self.write("manifest.json", b'{"revision":"first","revision":"second","files":{},"media_included":false}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.encrypt()

    def test_size_count_limits_apply_before_reading(self):
        with patch.object(backup, "MAX_FILE_BYTES", 4), patch.object(backup, "_read_file") as read:
            with self.assertRaises(ValueError):
                self.encrypt()
            read.assert_not_called()
        with patch.object(backup, "MAX_BACKUP_BYTES", 4), patch.object(backup, "_read_file") as read:
            with self.assertRaises(ValueError):
                self.encrypt()
            read.assert_not_called()

    def test_unsafe_config_archive_names_links_types_and_duplicates(self):
        for name, kind in (
            ("../outside.json", tarfile.REGTYPE), ("/config/key.json", tarfile.REGTYPE),
            ("config/../key.json", tarfile.REGTYPE), ("config\\key.json", tarfile.REGTYPE),
            ("cache/movie.mp4", tarfile.REGTYPE), ("config/key.pem", tarfile.REGTYPE),
            ("config/con.json", tarfile.REGTYPE), ("config/nested/key.json", tarfile.REGTYPE),
            ("config/key.json", tarfile.SYMTYPE), ("config/key.json", tarfile.LNKTYPE),
            ("config/key.json", tarfile.FIFOTYPE), ("config/key.json", tarfile.DIRTYPE),
        ):
            self.write("config.tar.gz", gzip.compress(tar_bytes([(name, b"{}", kind)])))
            self.manifest()
            with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                self.encrypt()
        repeated = [("config/key.json", b"{}", tarfile.REGTYPE)] * 2
        self.write("config.tar.gz", gzip.compress(tar_bytes(repeated)))
        self.manifest()
        with self.assertRaises(ValueError):
            self.encrypt()

    def test_config_expansion_member_count_and_size_limits(self):
        contents = (self.source / "config.tar.gz").read_bytes()
        for name, value in (("MAX_CONFIG_TAR_BYTES", 1), ("MAX_CONFIG_MEMBERS", 0), ("MAX_CONFIG_FILE_BYTES", 1), ("MAX_CONFIG_BYTES", 1)):
            with self.subTest(bound=name), patch.object(backup, name, value), self.assertRaises(ValueError):
                backup._check_config(contents)

    def test_corrupt_sqlite_and_hashes_refused(self):
        self.write("drivetheater.sqlite3", b"not a database")
        self.manifest()
        with self.assertRaisesRegex(ValueError, "SQLite"):
            self.encrypt()
        self.write("drivetheater.sqlite3", b"SQLite format 3\x00" + b"\x00" * 4096)
        self.manifest()
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.encrypt()

    def test_minimal_manifest_without_db_or_config_is_supported(self):
        (self.source / "drivetheater.sqlite3").unlink()
        (self.source / "config.tar.gz").unlink()
        self.manifest()
        self.encrypt()
        result = backup.verify_backup(self.destination, PASSPHRASE)
        self.assertEqual(result["database_integrity"], "not-present")
        self.assertEqual(result["config_members"], 0)
        self.assertEqual(result["files"], ["install.env", "manifest.json"])

    def test_authenticated_unsafe_outer_archives_rejected_without_extraction(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("/install.env", tarfile.REGTYPE),
                           ("install.env", tarfile.SYMTYPE), ("install.env", tarfile.LNKTYPE),
                           ("cache/movie.mp4", tarfile.REGTYPE)):
            payload = tar_bytes([(name, b"fixture", kind)])
            self.signed_payload(payload)
            with self.subTest(name=name, kind=kind), patch.object(tarfile.TarFile, "extractall") as extract:
                with self.assertRaises(ValueError):
                    backup.verify_backup(self.destination, PASSPHRASE)
                extract.assert_not_called()
        self.assertFalse((self.root / "outside").exists())

    def test_authenticated_missing_duplicate_or_mismatched_outer_members(self):
        files = {p.name: p.read_bytes() for p in self.source.iterdir()}
        good = [(name, value, tarfile.REGTYPE) for name, value in files.items()]
        cases = [good + [good[0]], good[1:], [(name, b"modified" if name == "install.env" else value, kind) for name, value, kind in good]]
        for index, entries in enumerate(cases):
            self.signed_payload(tar_bytes(entries))
            with self.subTest(index=index), self.assertRaises(ValueError):
                backup.verify_backup(self.destination, PASSPHRASE)

    def test_trailing_tar_data_and_gzip_corruption_refused(self):
        raw = tar_bytes([("config/key.json", b"{}", tarfile.REGTYPE)])
        for contents in (gzip.compress(raw + b"hidden" + b"\x00" * 506), b"not gzip", gzip.compress(raw)[:-4]):
            with self.subTest(length=len(contents)), self.assertRaises(ValueError):
                backup._check_config(contents)

    def test_readonly_sqlite_copy_is_private_and_cleaned(self):
        self.encrypt()
        original_connect = backup.sqlite3.connect
        original_temporary = backup.tempfile.TemporaryDirectory
        opened, folders = [], []

        def controlled(**kwargs):
            result = original_temporary(dir=self.root, **kwargs)
            folder = Path(result.name)
            folders.append(folder)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            return result

        def connect(database_uri, **kwargs):
            self.assertIn("?mode=ro&immutable=1", database_uri)
            opened.append(database_uri)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE((folders[-1] / "snapshot.sqlite3").stat().st_mode), 0o600)
            return original_connect(database_uri, **kwargs)

        with patch.object(backup.sqlite3, "connect", side_effect=connect), patch.object(backup.tempfile, "TemporaryDirectory", side_effect=controlled):
            result = backup.verify_backup(self.destination, PASSPHRASE)
        self.assertTrue(result["verified"])
        self.assertEqual(len(opened), 1)
        self.assertTrue(folders)
        self.assertTrue(all(not folder.exists() for folder in folders))
        self.assertNotIn(self.source.as_uri(), opened[0])
        self.assertEqual(set(p.name for p in self.root.iterdir()), {"snapshot", "snapshot.dtb"})

    def test_temp_copy_cleanup_on_failed_integrity(self):
        files = {p.name: p.read_bytes() for p in self.source.iterdir()}
        files["drivetheater.sqlite3"] = b"SQLite format 3\x00" + b"\x00" * 4096
        manifest = json.loads(files["manifest.json"])
        manifest["files"]["drivetheater.sqlite3"] = hashlib.sha256(files["drivetheater.sqlite3"]).hexdigest()
        files["manifest.json"] = json.dumps(manifest).encode()
        self.signed_payload(tar_bytes([(name, value, tarfile.REGTYPE) for name, value in files.items()]))
        original = backup.tempfile.TemporaryDirectory
        folders = []

        def controlled(**kwargs):
            result = original(dir=self.root, **kwargs)
            folders.append(Path(result.name))
            return result

        with patch.object(backup.tempfile, "TemporaryDirectory", side_effect=controlled), self.assertRaises(ValueError):
            backup.verify_backup(self.destination, PASSPHRASE)
        self.assertTrue(folders)
        self.assertTrue(all(not p.exists() for p in folders))

    def test_link_and_posix_permission_metadata_checks_without_symlink_privilege(self):
        ordinary = {"st_mode": stat.S_IFREG | 0o600, "st_nlink": 1, "st_size": 0, "st_file_attributes": 0}
        for changes in ({"st_mode": stat.S_IFLNK | 0o600}, {"st_nlink": 2}, {"st_file_attributes": 0x400}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                backup._regular(SimpleNamespace(**{**ordinary, **changes}), "Fixture", 100)
        with patch.object(backup.os, "name", "posix"), self.assertRaisesRegex(ValueError, "private"):
            backup._regular(SimpleNamespace(**{**ordinary, "st_mode": stat.S_IFREG | 0o640}), "Fixture", 100)

    def test_authenticated_oversized_manifest_rejected_before_member_reads(self):
        payload = tar_bytes([("manifest.json", b"x" * (backup.MAX_MANIFEST_BYTES + 1), tarfile.REGTYPE)])
        self.signed_payload(payload)
        with patch.object(tarfile.TarFile, "extractfile") as read, self.assertRaisesRegex(ValueError, "size limit"):
            backup.verify_backup(self.destination, PASSPHRASE)
        read.assert_not_called()

    def test_authenticated_unsafe_nested_config_refused_after_authentication(self):
        files = {p.name: p.read_bytes() for p in self.source.iterdir()}
        files["config.tar.gz"] = gzip.compress(tar_bytes([("config/key.json", b"", tarfile.SYMTYPE)]))
        manifest = json.loads(files["manifest.json"])
        manifest["files"]["config.tar.gz"] = hashlib.sha256(files["config.tar.gz"]).hexdigest()
        files["manifest.json"] = json.dumps(manifest).encode()
        self.signed_payload(tar_bytes([(name, value, tarfile.REGTYPE) for name, value in files.items()]))
        with self.assertRaisesRegex(ValueError, "unsafe"):
            backup.verify_backup(self.destination, PASSPHRASE)

    def test_source_hardlinks_refused(self):
        target = self.root / "original.env"
        target.write_bytes((self.source / "install.env").read_bytes())
        os.chmod(target, 0o600)
        (self.source / "install.env").unlink()
        try:
            os.link(target, self.source / "install.env")
        except OSError:
            self.skipTest("Hardlinks are unavailable on this filesystem")
        with self.assertRaises(ValueError):
            self.encrypt()

    def test_source_symlink_directory_and_destination_parent_symlink_refused(self):
        target = self.root / "real"
        target.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks require unavailable Windows privileges")
        with self.assertRaises(ValueError):
            backup.encrypt_backup(link, self.destination, PASSPHRASE)
        with self.assertRaises(ValueError):
            backup.encrypt_backup(self.source, link / "output.dtb", PASSPHRASE)
        real_env = self.root / "real.env"
        real_env.write_bytes(b"fixture")
        (self.source / "install.env").unlink()
        (self.source / "install.env").symlink_to(real_env)
        with self.assertRaises(ValueError):
            self.encrypt()

    def test_relative_and_parent_traversal_paths_refused(self):
        for source in (Path("snapshot"), self.root / "snapshot" / ".." / "snapshot"):
            with self.subTest(source=str(source)), self.assertRaises(ValueError):
                backup.encrypt_backup(source, self.destination, PASSPHRASE)
        with self.assertRaises(ValueError):
            backup.encrypt_backup(self.source, Path("result.dtb"), PASSPHRASE)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_unprivate_source_and_archive_refused(self):
        os.chmod(self.source, 0o750)
        with self.assertRaises(ValueError):
            self.encrypt()
        os.chmod(self.source, 0o700)
        os.chmod(self.source / "install.env", 0o640)
        with self.assertRaises(ValueError):
            self.encrypt()
        os.chmod(self.source / "install.env", 0o600)
        self.encrypt()
        os.chmod(self.destination, 0o644)
        with self.assertRaises(ValueError):
            backup.verify_backup(self.destination, PASSPHRASE)

    def test_actual_bootstrap_manifest_compatible_and_runtime_not_touched(self):
        bootstrap = load("bootstrap_for_encryption", "scripts/bootstrap_config.py")
        env, data = self.root / "live.env", self.root / "runtime"
        with patch.object(bootstrap.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10**12, 0, 10**12)):
            bootstrap.initialize(env, data, 10001, 10001, 8080)
        with closing(sqlite3.connect(data / "db" / "drivetheater.sqlite3")) as db:
            db.execute("CREATE TABLE fixture(value TEXT)")
            db.execute("INSERT INTO fixture VALUES ('snapshot')")
            db.commit()
        credential = data / "config" / "abcd1234.json"
        credential.write_text('{"fixture":"not-a-real-credential"}')
        os.chmod(credential, 0o600)
        (data / "cache" / "movie.mp4").write_bytes(b"must not be backed up")
        materialized = bootstrap.backup(env, "fixture-bootstrap")
        before = self.snapshot(data)
        env_before = env.read_bytes()
        backup.encrypt_backup(materialized, self.destination, PASSPHRASE)
        result = backup.verify_backup(self.destination, PASSPHRASE)
        self.assertEqual(result["database_integrity"], "ok")
        self.assertEqual(result["revision"], "fixture-bootstrap")
        self.assertEqual(self.snapshot(data), before)
        self.assertEqual(env.read_bytes(), env_before)
        self.assertFalse(any("cache" in name or "media" in name for name in result["files"]))


class InterfaceTests(unittest.TestCase):
    def test_import_and_help_need_no_cryptography(self):
        real_import = __import__

        def unavailable(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError("fixture missing dependency")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=unavailable):
            module = load("backup_without_cryptography", "scripts/encrypted_backup.py")
            with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as exit_result:
                module.main(["--help"])
            self.assertEqual(exit_result.exception.code, 0)
            self.assertIn("verify", output.getvalue())
            with self.assertRaisesRegex(RuntimeError, "installed cryptography"):
                module.verify_backup(Path("unused"), PASSPHRASE)
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(module.main(["verify", "unused"]), 1)
            self.assertIn("installed cryptography", errors.getvalue())

    def test_cli_passphrase_is_prompt_only_not_echoed_or_accepted_as_arg(self):
        result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "encrypted_backup.py"), "verify", "unused", "--passphrase", PASSPHRASE], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(PASSPHRASE, result.stdout + result.stderr)
        with patch.object(backup.getpass, "getpass", side_effect=[PASSPHRASE, PASSPHRASE]), patch.object(backup, "encrypt_backup", return_value={"encrypted": True}) as encrypt:
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(backup.main(["encrypt", "source", "destination"]), 0)
            self.assertNotIn(PASSPHRASE, output.getvalue())
            encrypt.assert_called_once_with(Path("source"), Path("destination"), PASSPHRASE)

    def test_cli_mismatch_unavailable_hidden_input_and_no_env_passphrase(self):
        with patch.dict(os.environ, {"DT_BACKUP_PASSPHRASE": PASSPHRASE}):
            with patch.object(backup.getpass, "getpass", side_effect=EOFError), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(backup.main(["verify", "unused"]), 1)
            self.assertNotIn(PASSPHRASE, errors.getvalue())
        with patch.object(backup.getpass, "getpass", side_effect=[PASSPHRASE, "different confirmation"]), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(backup.main(["encrypt", "source", "destination"]), 1)
        self.assertIn("did not match", errors.getvalue())

        def warning(_):
            warnings.warn("fixture no terminal", backup.getpass.GetPassWarning)

        with patch.object(backup.getpass, "getpass", side_effect=warning), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(backup.main(["verify", "unused"]), 1)
        self.assertIn("hidden passphrase input", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
