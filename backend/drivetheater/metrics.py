from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import time
from pathlib import Path
from typing import Any

import psutil

from .db import Database

_TABLES = ("users", "sessions", "sources", "videos", "jobs", "activity_events", "watch_history")


class ServerMetrics:
    """Collect local process/namespace facts without fabricating host visibility."""

    def __init__(self) -> None:
        self._previous_network: tuple[float, int, int] | None = None
        self._started_at = time.time()

    @staticmethod
    def _scope() -> str:
        if platform.system() != "Linux":
            return "host_or_runtime_namespace"
        try:
            cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return "host_or_runtime_namespace"
        return "container_namespace" if Path('/.dockerenv').exists() or any(name in cgroup for name in ("docker", "containerd", "kubepods", "podman")) else "host_or_runtime_namespace"

    def snapshot(self, db: Database, data_dir: Path) -> dict[str, Any]:
        now = time.monotonic()
        net = psutil.net_io_counters()
        rates: dict[str, float | None] = {"send_rate_bps": None, "recv_rate_bps": None}
        if self._previous_network:
            previous_at, previous_sent, previous_recv = self._previous_network
            elapsed = now - previous_at
            if elapsed > 0:
                rates = {"send_rate_bps": max(0.0, (net.bytes_sent - previous_sent) / elapsed), "recv_rate_bps": max(0.0, (net.bytes_recv - previous_recv) / elapsed)}
        self._previous_network = (now, net.bytes_sent, net.bytes_recv)
        disk = shutil.disk_usage(data_dir)
        memory = psutil.virtual_memory()
        process = psutil.Process()
        with db.connect() as conn:
            counts = {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in _TABLES}
            active_jobs = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','downloading','preparing')").fetchone()[0])
            worker_row = conn.execute("SELECT value FROM settings WHERE key='worker_cpu'").fetchone()
            heartbeat = conn.execute("SELECT value FROM settings WHERE key='worker_heartbeat'").fetchone()
        database = db.path
        wal = Path(f"{database}-wal")
        scope = self._scope()
        worker_alive = False
        try:
            worker_alive = bool(heartbeat and float(heartbeat[0]) > time.time() - 30)
            worker_cpu = json.loads(worker_row[0]) if worker_row and worker_alive else {}
        except (ValueError, TypeError, json.JSONDecodeError):
            worker_cpu = {}
            worker_alive = False
        effective = worker_cpu.get("effective_cpus")
        if not isinstance(effective, (int, float)) or isinstance(effective, bool) or effective <= 0:
            effective = None
        network_scope = "container_or_runtime_namespace" if scope == "container_namespace" else "visible_system"
        return {
            "scope": scope,
            "cpu": {"percent": psutil.cpu_percent(interval=None), "cores": psutil.cpu_count(logical=True), "scope": "visible_system"},
            "memory": {"total_bytes": memory.total, "used_bytes": memory.used, "scope": "visible_system"},
            "disk": {"path": str(data_dir), "total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
            "network": {"bytes_sent": net.bytes_sent, "bytes_recv": net.bytes_recv, **rates, "scope": network_scope},
            "uptime_seconds": max(0.0, time.time() - psutil.boot_time()),
            "process": {"pid": process.pid, "uptime_seconds": max(0.0, time.time() - process.create_time()), "rss_bytes": process.memory_info().rss},
            "database": {"sqlite_bytes": database.stat().st_size if database.exists() else 0, "wal_bytes": wal.stat().st_size if wal.exists() else 0},
            "counts": counts,
            "active_jobs": active_jobs,
            "downloaders": downloader_capabilities(),
            "media_cpu": {
                "effective_cpus": effective,
                "worker_alive": worker_alive,
                "scope_note": "CPU and RAM are visible-system counters; network counters are scoped to the reported runtime namespace. Effective media CPUs are only reported by a live worker.",
            },
        }


def downloader_capabilities() -> dict[str, dict[str, Any]]:
    def executable(name: str) -> dict[str, Any]:
        path = shutil.which(name)
        return {"available": bool(path), "detail": path or "not installed"}

    def installed_package(name: str, detail: str) -> dict[str, Any]:
        return {"available": importlib.util.find_spec(name) is not None, "detail": detail}

    return {
        "direct": {"available": True, "detail": "built-in HTTP client"},
        "yt_dlp": executable("yt-dlp"),
        "mega": executable("megadl"),
        "torrent": executable("aria2c"),
        "gdown": installed_package("gdown", "installed Python package"),
        "gofile": installed_package("gallery_dl", "installed Python package"),
    }
