"""Pure scheduling and bandwidth rules used by the worker."""
from __future__ import annotations

import time


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def allowed_now(settings: dict[str, object], interactive: bool = False, now: float | None = None) -> bool:
    """Return whether a noninteractive transfer may run in local server time."""
    if interactive or not _truthy(settings.get("download_schedule_enabled")):
        return True
    try:
        start = int(float(settings.get("download_start_hour", 0))) % 24
        end = int(float(settings.get("download_end_hour", 0))) % 24
    except (TypeError, ValueError):
        return False
    if start == end:
        return True
    hour = time.localtime(time.time() if now is None else now).tm_hour
    return start <= hour < end if start < end else hour >= start or hour < end


def limit_bytes(value: object) -> int:
    """Convert a Mbps setting to bytes/second; zero means no rate limit."""
    try:
        mbps = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, int(mbps * 1_000_000 / 8))


class TransferThrottle:
    """Monotonic token pacing with short sleeps so cancellation stays responsive."""
    def __init__(self, rate_bytes: int, clock=time.monotonic, sleeper=time.sleep):
        self.rate = max(0, int(rate_bytes))
        self.clock, self.sleeper = clock, sleeper
        self.next_at = clock()

    def consume(self, count: int, check) -> None:
        if not self.rate or count <= 0:
            return
        self.next_at = max(self.next_at, self.clock()) + count / self.rate
        while True:
            check()
            remaining = self.next_at - self.clock()
            if remaining <= 0:
                return
            self.sleeper(min(.05, remaining))
