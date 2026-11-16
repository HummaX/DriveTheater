"""Unit tests for the standalone HTTPS/Caddy helper; all filesystem writes are temporary."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("https_setup", ROOT / "scripts" / "https_setup.py")
https_setup = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(https_setup)


class ValidationTests(unittest.TestCase):
    def test_canonical_domain_and_origin(self) -> None:
        result = https_setup.preflight("Media.Example.COM", 8080, listeners={80: False, 443: False, 8080: True})
        self.assertEqual(result["domain"], "media.example.com")
        self.assertEqual(result["public_origin"], "https://media.example.com")
        self.assertEqual(result["backend"]["url"], "http://127.0.0.1:8080")
        self.assertTrue(result["caddy_ports_available"])
        self.assertEqual(result["checks"]["backend_loopback"]["status"], "occupied")
        self.assertEqual(result["external"]["dns"]["status"], "unknown")

    def test_rejects_directive_and_control_character_injection(self) -> None:
        for domain in (
            "media.example.com { reverse_proxy attacker.invalid }",
            "media.example.com\nreverse_proxy attacker.invalid",
            "https://media.example.com",
            "media.example.com;import evil",
            "127.0.0.1",
            "localhost",
        ):
            with self.subTest(domain=domain):
                with self.assertRaises(ValueError):
                    https_setup.render_caddy(domain)

    def test_rejects_non_backend_ports_and_bad_types(self) -> None:
        for port in (0, 65536, True, "8080", 80, 443):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    https_setup.render_caddy("media.example.com", port)


class PreflightTests(unittest.TestCase):
    def test_reports_existing_caddy_listener_and_backend_absence(self) -> None:
        result = https_setup.preflight("media.example.com", 8080, listeners={80: True, 443: False, 8080: False})
        self.assertFalse(result["caddy_ports_available"])
        self.assertEqual(result["checks"]["caddy_port_80"]["status"], "occupied")
        self.assertEqual(result["checks"]["backend_loopback"]["status"], "free")
        self.assertIn("start DriveTheater separately", result["checks"]["backend_loopback"]["detail"])

    def test_supports_read_only_callable_listener_adapter(self) -> None:
        seen: list[int] = []

        def listener(port: int) -> str:
            seen.append(port)
            return "unknown"

        result = https_setup.preflight("media.example.com", 9090, listeners=listener)
        self.assertEqual(seen, [80, 443, 9090])
        self.assertEqual(result["checks"]["caddy_port_443"]["status"], "unknown")
        self.assertFalse(result["caddy_ports_available"])


class WriteConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="drivetheater-https-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_creates_private_new_file_and_refuses_replacement(self) -> None:
        destination = self.root / "Caddyfile"
        content = https_setup.render_caddy("media.example.com", 8080)
        self.assertEqual(https_setup.write_config(destination, content), destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), content)
        if os.name == "posix":
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            https_setup.write_config(destination, "replacement")
        self.assertEqual(destination.read_text(encoding="utf-8"), content)

    def test_propagates_read_only_write_failure_without_creating_file(self) -> None:
        destination = self.root / "Caddyfile"
        with patch.object(https_setup.os, "open", side_effect=PermissionError("read-only")):
            with self.assertRaises(PermissionError):
                https_setup.write_config(destination, "safe content\n")
        self.assertFalse(destination.exists())

    def test_requires_real_existing_parent(self) -> None:
        with self.assertRaises(ValueError):
            https_setup.write_config(self.root / "missing" / "Caddyfile", "safe content\n")


class RenderTests(unittest.TestCase):
    def test_output_uses_loopback_no_cache_and_streaming_support(self) -> None:
        config = https_setup.render_caddy("media.example.com", 8080)
        self.assertIn("https://media.example.com", config)
        self.assertIn("reverse_proxy 127.0.0.1:8080", config)
        self.assertIn("flush_interval -1", config)
        self.assertIn('Cache-Control "no-store, private"', config)
        self.assertIn("Range requests", config)
        self.assertNotIn("trusted_proxies", config)


if __name__ == "__main__":
    unittest.main()
