#!/usr/bin/env python3
"""Read-only health checks for an existing DriveTheater installation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
from typing import Any

MiB = 1024 * 1024
GiB = 1024 * MiB
DEFAULT_ENV_FILE = Path("/etc/drivetheater/install.env")


def load_bootstrap_config(script_dir: Path) -> Any:
    """Load the existing installation validator without reading env values into output."""
    path = script_dir / "bootstrap_config.py"
    spec = importlib.util.spec_from_file_location("drivetheater_bootstrap_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("installer configuration helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(results: list[dict[str, Any]], name: str, status: str, detail: str) -> None:
    results.append({"check": name, "status": status, "detail": detail})


def private_path_check(results: list[dict[str, Any]], name: str, path: Path, *, directory: bool = True,
                       private: bool = False, allow_group: bool = False,
                       owner_uid: int | None = None, owner_gid: int | None = None) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        record(results, name, "error", f"path is unavailable ({exc.__class__.__name__})")
        return False
    if stat.S_ISLNK(info.st_mode):
        record(results, name, "error", "path must not be a symbolic link")
        return False
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        record(results, name, "error", "path has an unexpected type")
        return False
    if os.name == "posix":
        exposed = info.st_mode & (0o007 if allow_group else (0o077 if private else 0o022))
        if exposed:
            required = "not accessible by others" if allow_group else ("private" if private else "not writable by group or others")
            record(results, name, "error", f"permissions must be {required}")
            return False
        if owner_uid is not None and info.st_uid != owner_uid:
            record(results, name, "warning", "path owner differs from configured runtime user")
            return False
        if owner_gid is not None and not allow_group and info.st_gid != owner_gid:
            record(results, name, "warning", "path group differs from configured runtime group")
            return False
    record(results, name, "ok", "path and permissions are acceptable")
    return True


def run_version(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, exc.__class__.__name__
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return completed.returncode == 0, output[0] if output else f"exit {completed.returncode}"


def sqlite_checks(results: list[dict[str, Any]], database: Path, heartbeat_max_age: int) -> None:
    if database.is_symlink():
        record(results, "database", "error", "database path must not be a symbolic link")
        return
    if not database.is_file():
        record(results, "database", "warning", "database is not present yet")
        return
    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                record(results, "sqlite_quick_check", "error", "SQLite quick_check did not return ok")
            else:
                record(results, "sqlite_quick_check", "ok", "SQLite quick_check returned ok")
            try:
                heartbeat = connection.execute(
                    "SELECT value FROM settings WHERE key = 'worker_heartbeat'"
                ).fetchone()
            except sqlite3.Error:
                heartbeat = None
                record(results, "worker_heartbeat", "warning", "worker heartbeat is unavailable")
            else:
                if not heartbeat:
                    record(results, "worker_heartbeat", "warning", "no worker heartbeat was recorded")
                else:
                    try:
                        age = max(0, datetime.now(timezone.utc).timestamp() - float(heartbeat[0]))
                    except (TypeError, ValueError):
                        record(results, "worker_heartbeat", "error", "worker heartbeat has an invalid timestamp")
                    else:
                        detail = f"last heartbeat {age:.0f}s ago"
                        status = "ok" if age <= heartbeat_max_age else "warning"
                        record(results, "worker_heartbeat", status, detail)
    except (OSError, sqlite3.Error, ValueError) as exc:
        record(results, "database", "error", f"read-only database check failed ({exc.__class__.__name__})")


def tool_checks(results: list[dict[str, Any]]) -> None:
    for name in ("ffmpeg", "ffprobe"):
        location = shutil.which(name)
        record(results, name, "ok" if location else "warning",
               "available" if location else "not found on PATH")

    docker = shutil.which("docker")
    if not docker:
        record(results, "docker", "warning", "not found on PATH")
        record(results, "docker_compose", "warning", "not checked because Docker is unavailable")
        return
    available, version = run_version([docker, "--version"])
    record(results, "docker", "ok" if available else "warning",
           version if available else f"installed but unusable ({version})")
    available, version = run_version([docker, "compose", "version"])
    record(results, "docker_compose", "ok" if available else "warning",
           version if available else f"unavailable ({version})")


def inspect(env_file: Path, heartbeat_max_age: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        helper = load_bootstrap_config(Path(__file__).resolve().parent)
    except (OSError, RuntimeError, ImportError) as exc:
        record(results, "installation_configuration", "error",
               f"installer configuration helper is unavailable ({exc.__class__.__name__})")
        return results
    try:
        values = helper.verify_installation(env_file)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        record(results, "installation_configuration", "error",
               f"installer configuration validation failed ({exc.__class__.__name__})")
        return results

    record(results, "installation_configuration", "ok", "installation identity validated")
    data_dir = Path(values["DT_HOST_DATA_DIR"])
    runtime_uid = int(values["DT_UID"])
    runtime_gid = int(values["DT_GID"])
    private_path_check(results, "install_env", env_file, directory=False, private=True)
    private_path_check(results, "data_directory", data_dir, private=False, allow_group=True)
    private_path_check(results, "credentials_directory", data_dir / "config", private=True,
                       owner_uid=runtime_uid, owner_gid=runtime_gid)
    private_path_check(results, "data_db_directory", data_dir / "db", private=False, allow_group=True,
                       owner_uid=runtime_uid)
    private_path_check(results, "data_cache_directory", data_dir / "cache", private=False, allow_group=True,
                       owner_uid=runtime_uid)
    private_path_check(results, "data_backups_directory", data_dir / "backups", private=False, allow_group=True,
                       owner_uid=runtime_uid)

    try:
        free = shutil.disk_usage(data_dir).free
    except OSError as exc:
        record(results, "disk_free", "error", f"unable to inspect free space ({exc.__class__.__name__})")
    else:
        status = "ok" if free >= 2 * GiB else "warning"
        record(results, "disk_free", status, f"{free / GiB:.2f} GiB free")

    sqlite_checks(results, data_dir / "db" / "drivetheater.sqlite3", heartbeat_max_age)
    tool_checks(results)
    return results


def render_text(results: list[dict[str, Any]]) -> None:
    for result in results:
        print(f"{result['status'].upper():7} {result['check']}: {result['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DriveTheater installation diagnostics")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_FILE,
                        help="installation environment file (default: /etc/drivetheater/install.env)")
    parser.add_argument("--json", action="store_true", help="emit structured diagnostic output")
    parser.add_argument("--heartbeat-max-age", type=int, default=90,
                        help="warn when worker heartbeat is older than this many seconds (default: 90)")
    args = parser.parse_args()
    if args.heartbeat_max_age < 1:
        parser.error("--heartbeat-max-age must be positive")

    results = inspect(args.env, args.heartbeat_max_age)
    if args.json:
        print(json.dumps({"results": results, "healthy": all(item["status"] == "ok" for item in results)}, indent=2))
    else:
        render_text(results)
    return 0 if all(item["status"] == "ok" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
