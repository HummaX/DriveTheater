"""CPU budgeting shared by media workers and diagnostics."""
from __future__ import annotations

import math
import os
from pathlib import Path


def cpu_limit(root: Path = Path('/sys/fs/cgroup')) -> float | None:
    """Read the current cgroup quota; None means no readable CPU quota."""
    try:
        quota, period = (root / 'cpu.max').read_text().split()[:2]
        if quota != 'max' and float(period) > 0:
            return float(quota) / float(period)
    except (OSError, ValueError):
        pass
    for directory in (root / 'cpu', root):
        try:
            quota = float((directory / 'cpu.cfs_quota_us').read_text())
            period = float((directory / 'cpu.cfs_period_us').read_text())
            if quota > 0 and period > 0:
                return quota / period
        except (OSError, ValueError):
            continue
    return None


def media_cpus() -> float:
    available = float(os.cpu_count() or 1)
    if hasattr(os, 'sched_getaffinity'):
        available = min(available, len(os.sched_getaffinity(0)))
    quota = cpu_limit()
    if quota is not None:
        available = min(available, quota)
    try:
        configured = float(os.environ.get('DT_MEDIA_CPUS', str(available * .9 if quota is None else available)))
        if math.isfinite(configured) and configured > 0:
            available = min(available, configured)
    except ValueError:
        pass
    return max(.1, available)


def media_threads() -> int:
    # Split threads across simultaneous encoders; the container quota remains the
    # hard limit even when FFmpeg's audio/filter/decoder threads do extra work.
    try:
        parallel = max(1, min(2, int(os.environ.get('DT_ENCODER_SLOTS', '1'))))
    except ValueError:
        parallel = 1
    return max(1, math.floor(media_cpus() / parallel))
