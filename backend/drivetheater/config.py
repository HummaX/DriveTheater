from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    admin_user: str
    admin_password: str | None
    secret: str
    cookie_secure: bool
    allow_private_sources: bool
    public_url: str | None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "db" / "drivetheater.sqlite3"

    @property
    def credentials_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "cache"


def load_config() -> Config:
    data_dir = Path(os.environ.get("DT_DATA_DIR", "/data")).expanduser().resolve()
    secret = os.environ.get("DT_SECRET", "")
    # Development may run without a configured secret; sessions remain process-local.
    if secret and len(secret) < 32:
        raise RuntimeError("DT_SECRET must contain at least 32 characters")
    return Config(
        data_dir=data_dir,
        admin_user=os.environ.get("DT_ADMIN_USER", "admin"),
        admin_password=os.environ.get("DT_ADMIN_PASSWORD"),
        secret=secret or "development-only-not-for-production-0000",
        cookie_secure=os.environ.get("DT_COOKIE_SECURE", "false").lower() == "true",
        allow_private_sources=os.environ.get("DT_ALLOW_PRIVATE_SOURCES", "false").lower() == "true",
        public_url=os.environ.get("DT_PUBLIC_URL"),
    )
