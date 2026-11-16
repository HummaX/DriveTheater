#!/usr/bin/env python3
"""Root-only local network-policy recovery. Never changes passwords or MFA."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import time


def recover_network(database: Path, *, confirm: bool = False) -> Path:
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise PermissionError('Network recovery requires a local root shell')
    if not confirm:
        raise ValueError('Explicit --confirm-network-reset is required')
    database = Path(database)
    if not database.is_absolute() or database.is_symlink() or database.resolve() != database:
        raise ValueError('Use the absolute existing database path without symlinks')
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Database must be a regular single-link file')
    # Exclusive private snapshot is retained for rollback. Do not print any DB content.
    backup = database.with_name(database.name + '.network-recovery-' + str(int(time.time())) + '-' + secrets.token_hex(4))
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=30)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_policies'").fetchone():
            raise ValueError('Database has no network policy to recover')
        with closing(sqlite3.connect(backup)) as snapshot:
            connection.backup(snapshot)
            snapshot.commit()
            snapshot.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            snapshot.execute('PRAGMA journal_mode=DELETE')
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Recovery backup integrity check failed; nothing changed')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("DELETE FROM security_policies WHERE key='network'")
            connection.execute("DELETE FROM security_confirmations WHERE kind='network'")
            connection.execute("INSERT INTO security_events(occurred_at,username,event_type,details) VALUES(?,NULL,'local_network_recovery',?)", (int(time.time()), json.dumps({'method': 'local_root'})))
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--confirm-network-reset', action='store_true')
    args = parser.parse_args()
    try:
        backup = recover_network(args.database, confirm=args.confirm_network_reset)
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, 'Recovery refused or failed. Check local root access, the exact database path, and free space. No password or MFA reset was performed.\n')
    print('Network restrictions reset to direct-peer defaults. Passwords, MFA, sessions and media were preserved.')
    print('Private pre-change database snapshot: ' + str(backup))


if __name__ == '__main__':
    main()
