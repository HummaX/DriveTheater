#!/usr/bin/env python3
"""Generate a safe, new Caddy configuration for a DriveTheater HTTPS proxy.

This helper never installs, starts, reloads, or contacts Caddy.  Its preflight
only examines loopback listener state; DNS, ACME, firewall, and public reachability
are deliberately reported as unknown because they cannot be established locally.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
from typing import Any, Callable, Mapping

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _reject_controls(value: str, label: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters")


def validate_domain(domain: str) -> str:
    """Return a canonical public DNS hostname, rejecting Caddy-injection syntax."""
    if not isinstance(domain, str):
        raise ValueError("domain must be a string")
    _reject_controls(domain, "domain")
    if not domain or domain != domain.strip() or len(domain) > 253:
        raise ValueError("domain must be a non-empty DNS hostname")
    candidate = domain.lower()
    if candidate in {"localhost", "localhost.localdomain"} or ":" in candidate:
        raise ValueError("domain must be a public DNS hostname, not localhost or an IP address")
    labels = candidate.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("domain must be a multi-label DNS hostname using letters, digits, hyphens, and dots")
    if all(label.isdigit() for label in labels):
        raise ValueError("domain must be a DNS hostname, not an IP address")
    return candidate


def validate_port(port: int) -> int:
    """Validate an integer TCP backend port."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 through 65535")
    if port in (80, 443):
        raise ValueError("backend port must not be 80 or 443; reserve those ports for Caddy")
    return port


def _loopback_listener(port: int) -> str:
    """Read loopback connection state without binding or sending application data."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return "occupied"
    except ConnectionRefusedError:
        return "free"
    except OSError:
        return "unknown"


def _normalize_listener_fact(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("status")
    if value is True:
        return "occupied"
    if value is False:
        return "free"
    if value is None:
        return "unknown"
    if value in {"free", "occupied", "unknown"}:
        return value
    raise ValueError("listener adapter must return free, occupied, unknown, true, false, or null")


def _listener_fact(port: int, listeners: Any) -> str:
    if listeners is None:
        return _loopback_listener(port)
    if isinstance(listeners, Mapping):
        return _normalize_listener_fact(listeners.get(port))
    reader: Callable[[int], Any]
    if callable(listeners):
        reader = listeners
    elif callable(getattr(listeners, "listening", None)):
        reader = listeners.listening
    else:
        raise ValueError("listeners must be a mapping, callable, or object with listening(port)")
    try:
        return _normalize_listener_fact(reader(port))
    except OSError:
        return "unknown"


def _check(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def preflight(domain: str, port: int = 8080, *, listeners: Any = None) -> dict[str, Any]:
    """Return a read-only, local-only HTTPS proxy assessment.

    ``listeners`` is an injectable port-fact adapter for tests or host tooling. A
    mapping uses ``True`` for occupied and ``False`` for free; a callable or an
    object implementing ``listening(port)`` may instead return those booleans or
    the strings ``free``, ``occupied``, and ``unknown``.
    """
    hostname = validate_domain(domain)
    backend_port = validate_port(port)
    port_states = {number: _listener_fact(number, listeners) for number in (80, 443, backend_port)}

    checks: dict[str, dict[str, str]] = {}
    for number, purpose in ((80, "HTTP redirect"), (443, "HTTPS")):
        state = port_states[number]
        detail = (
            f"loopback port {number} is available for Caddy {purpose.lower()}"
            if state == "free"
            else f"loopback port {number} already has a listener; do not start Caddy until the owner is resolved"
            if state == "occupied"
            else f"could not determine whether loopback port {number} is available"
        )
        checks[f"caddy_port_{number}"] = _check(state, detail)

    backend_state = port_states[backend_port]
    backend_detail = (
        f"loopback backend 127.0.0.1:{backend_port} accepted a connection"
        if backend_state == "occupied"
        else f"no loopback listener accepted 127.0.0.1:{backend_port}; start DriveTheater separately before applying"
        if backend_state == "free"
        else f"could not determine backend listener state for 127.0.0.1:{backend_port}"
    )
    checks["backend_loopback"] = _check(backend_state, backend_detail)

    external = {
        "dns": _check("unknown", "not queried; confirm the public DNS record points to this server"),
        "acme": _check("unknown", "not attempted; confirm public port 80/443 reachability before Caddy obtains certificates"),
        "firewall": _check("unknown", "not inspected; confirm host and provider firewalls allow TCP 80 and 443"),
    }
    caddy_ports_free = all(port_states[number] == "free" for number in (80, 443))
    return {
        "domain": hostname,
        "public_origin": f"https://{hostname}",
        "backend": {"host": "127.0.0.1", "port": backend_port, "url": f"http://127.0.0.1:{backend_port}"},
        "checks": checks,
        "external": external,
        "caddy_ports_available": caddy_ports_free,
        "limitations": [
            "This local preflight does not verify public DNS, ACME issuance, firewall rules, NAT, or external reachability.",
            "It does not install, start, reload, or configure Caddy or DriveTheater.",
        ],
    }


def render_caddy(domain: str, port: int = 8080) -> str:
    """Return a deterministic Caddyfile that proxies the validated HTTPS origin."""
    hostname = validate_domain(domain)
    backend_port = validate_port(port)
    return f"""# Generated by scripts/https_setup.py. Review before applying; do not append untrusted text.
# Caddy owns public TCP ports 80 and 443. DriveTheater remains loopback-only.
http://{hostname} {{
\tredir https://{{host}}{{uri}} permanent
}}

https://{hostname} {{
\t# Standard security headers applied to all responses.
\theader {{
\t\tX-Content-Type-Options \"nosniff\"
\t\tX-Frame-Options \"DENY\"
\t\tReferrer-Policy \"same-origin\"
\t\tStrict-Transport-Security \"max-age=31536000; includeSubDomains\"
\t\t-Server
\t}}

\t# Cache-Control partitioning: long-term immutable caching for hashed UI bundles,
\t# revalidation for Service Worker, and no-store for dynamic media/api endpoints.
\t@static path /assets/* /favicon.svg /robots.txt
\theader @static Cache-Control \"public, max-age=31536000, immutable\"

\t@sw path /sw.js
\theader @sw Cache-Control \"no-cache\"

\t@dynamic not path /assets/* /favicon.svg /robots.txt /sw.js
\theader @dynamic {{
\t\tCache-Control \"no-store, private\"
\t\tPragma \"no-cache\"
\t}}

\t# Caddy forwards Range requests by default. Immediate flushing preserves SSE streaming.
\treverse_proxy 127.0.0.1:{backend_port} {{
\t\tflush_interval -1
\t\theader_up Host {hostname}
\t\theader_up X-Forwarded-Host {hostname}
\t\theader_up X-Forwarded-Proto {{scheme}}
\t}}
}}
"""


def write_config(destination: Path, content: str) -> Path:
    """Create one new private config file without replacing an existing path."""
    if not isinstance(destination, Path):
        raise ValueError("destination must be a pathlib.Path")
    if not isinstance(content, str) or not content or "\x00" in content:
        raise ValueError("content must be non-empty text without NUL bytes")
    parent = destination.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise ValueError("destination parent must already exist") from exc
    if parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError("destination parent must be a real directory")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # The file is exclusively ours. Remove a partial result rather than treating it as valid.
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DriveTheater HTTPS/Caddy preflight and config generator")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "render", "write"):
        command = subcommands.add_parser(name)
        command.add_argument("--domain", required=True, help="public DNS hostname, for example media.example.com")
        command.add_argument("--port", type=int, default=8080, help="loopback DriveTheater backend port (default: 8080)")
        if name == "write":
            command.add_argument("--destination", required=True, type=Path, help="new Caddyfile path; existing files are refused")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            print(json.dumps(preflight(args.domain, args.port), indent=2, sort_keys=True))
        else:
            config = render_caddy(args.domain, args.port)
            if args.command == "render":
                print(config, end="")
            else:
                print(write_config(args.destination, config))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
