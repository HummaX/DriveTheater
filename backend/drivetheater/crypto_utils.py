"""Purpose-bound authenticated encryption for small application secrets.

Callers supply the master secret for every operation. No key or secret is cached.
These tokens are not password hashes and must not be used as session cookies.
"""
from __future__ import annotations

import base64
import binascii
import os
import re

_PREFIX = "dtsec1."
_DOMAIN = b"DriveTheater/sealed-secret/v1\x00"
_DEVELOPMENT_SECRET = "development-only-not-for-production-0000"
MAX_SECRET_BYTES = 64 * 1024
MAX_TOKEN_CHARS = len(_PREFIX) + 4 * ((12 + MAX_SECRET_BYTES + 16 + 2) // 3)


def _parameters(master_secret: str, purpose: str) -> tuple[bytes, bytes]:
    if (
        not isinstance(master_secret, str)
        or not 32 <= len(master_secret) <= 4096
        or master_secret == _DEVELOPMENT_SECRET
        or master_secret.startswith("development-only-")
    ):
        raise ValueError("A non-development master secret of 32..4096 characters is required")
    if not isinstance(purpose, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", purpose):
        raise ValueError("Purpose must contain 1..128 ASCII letters, digits or _ . : / -")
    try:
        master = master_secret.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Master secret must be valid UTF-8") from exc
    return master, _DOMAIN + purpose.encode("ascii")


def _crypto():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise RuntimeError("Secret encryption requires the installed cryptography package") from exc
    return AESGCM, HKDF, hashes, InvalidTag


def _key(master: bytes, context: bytes, hkdf, hashes) -> bytes:
    # Stable domain-separated key derivation, not password stretching. The master
    # must be a randomly generated installation secret, not a human password.
    return hkdf(
        algorithm=hashes.SHA256(), length=32,
        salt=b"DriveTheater/HKDF-SHA256/sealed-secret/v1", info=context,
    ).derive(master)


def seal_secret(value: str, master_secret: str, purpose: str) -> str:
    """Seal at most 64 KiB of UTF-8 under a purpose-specific AES-256-GCM key."""
    master, context = _parameters(master_secret, purpose)
    if not isinstance(value, str) or len(value) > MAX_SECRET_BYTES:
        raise ValueError("Secret must be a UTF-8 string of at most 64 KiB")
    try:
        plaintext = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Secret must be valid UTF-8") from exc
    if len(plaintext) > MAX_SECRET_BYTES:
        raise ValueError("Secret must be a UTF-8 string of at most 64 KiB")
    aesgcm, hkdf, hashes, _ = _crypto()
    nonce = os.urandom(12)
    ciphertext = aesgcm(_key(master, context, hkdf, hashes)).encrypt(nonce, plaintext, context)
    return _PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def open_secret(token: str, master_secret: str, purpose: str) -> str:
    """Authenticate and open a v1 token; malformed/wrong-key tokens raise ValueError."""
    master, context = _parameters(master_secret, purpose)
    if not isinstance(token, str) or not token.startswith(_PREFIX) or len(token) > MAX_TOKEN_CHARS:
        raise ValueError("Invalid encrypted secret")
    encoded = token[len(_PREFIX):]
    try:
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid encrypted secret") from exc
    if (
        not 28 <= len(raw) <= 12 + MAX_SECRET_BYTES + 16
        or base64.urlsafe_b64encode(raw).decode("ascii") != encoded
    ):
        raise ValueError("Invalid encrypted secret")
    aesgcm, hkdf, hashes, invalid_tag = _crypto()
    try:
        plaintext = aesgcm(_key(master, context, hkdf, hashes)).decrypt(raw[:12], raw[12:], context)
        return plaintext.decode("utf-8")
    except (invalid_tag, UnicodeError) as exc:
        raise ValueError("Invalid encrypted secret") from exc
