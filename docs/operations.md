# Operations

## Routine health check

After installation, and before a planned update, run the read-only diagnostic command locally on the host:

```bash
sudo python3 -I /opt/drivetheater/scripts/doctor.py --env /etc/drivetheater/install.env
```

The command validates the installation using the existing bootstrap configuration helper. It checks that installation paths are not symlinks and retain appropriate privacy, reports free disk capacity, opens SQLite strictly read-only for `PRAGMA quick_check`, and reports the age of the worker heartbeat stored in the database. It also reports whether `ffmpeg`, `ffprobe`, and already-installed Docker/Compose commands are available.

The default worker-heartbeat warning threshold is 90 seconds. For a deliberately stopped worker, this warning is expected; do not change the database merely to clear it. Use `--heartbeat-max-age SECONDS` to suit a monitored environment, or `--json` for structured output. Any warning or error returns a nonzero exit code. The diagnostic command does not reveal configuration values, start or stop services, install software, contact providers, or modify application data.

## Updates, logs, and backups

Use the management wrapper for normal operations:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh status
sudo bash /opt/drivetheater/scripts/manage.sh logs
sudo bash /opt/drivetheater/scripts/manage.sh backup
sudo bash /opt/drivetheater/scripts/manage.sh update REVIEWED_COMMIT
```

Run a backup before an update and preserve the generated backup independently from the application host. Backups contain private configuration and database content but do not include cached media. Inspect failures before retrying; do not delete configuration, volumes, cached data, or backups as a generic recovery step.

## Fresh-install cadence

For a monthly deployment rehearsal, use a new Ubuntu 24.04 host or a confirmed empty installation target, perform a fresh install, run the doctor command, create a new administrator login, and verify the normal playback and worker flows with approved test content. Treat each fresh host as a new private capsule: it must have its own runtime credentials and backup destination.

Do not migrate cached media between fresh installations. If a database or provider configuration must be restored, plan and rehearse that recovery separately, preserving its private configuration and source-version compatibility. A successful local diagnostic is not proof that a fresh Docker deployment works on an Ubuntu host.

## Required deployment verification

Real Docker/Ubuntu 24.04 deployment verification remains unrun in this repository. Before relying on a release, execute and document a fresh-host verification that covers installation, Docker/Compose startup, readiness, worker heartbeats, backup creation, and controlled update behavior. This documentation does not claim those host-level checks have been automated or completed.
