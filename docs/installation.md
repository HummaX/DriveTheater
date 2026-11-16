# Installation, operations and recovery

## Fresh bootstrap

Target: Ubuntu 24.04, x86-64 or ARM64, with root access or a sudo-capable account. No separate disk is required. The installer provisions missing Git/Python/Docker/Compose prerequisites using Ubuntu packages, starts Docker, creates a dedicated non-login runtime identity, and builds the application. The data directory is outside the source checkout and never part of the Docker build context.

The final delivery gives a commit-pinned raw GitHub script invocation. After downloading a reviewed copy, the equivalent local command is:

```bash
sudo bash scripts/install.sh --version REVIEWED_COMMIT
```

Optional parameters:

- `--port 8080` when port80 is used by another service; unrelated processes are never killed.
- `--data-dir /srv/drivetheater-data` for another persistent directory. It need not be a mountpoint.
- `--install-dir /opt/drivetheater` for source location.
- `--public-url https://media.example.com` for the browser OAuth callback/base URL behind your configured TLS proxy.

Configuration is generated at `/etc/drivetheater/install.env` mode600; no manual dotenv editing is required. A fresh data directory receives owner-controlled `db`, `config`, `cache`, and `backups` subdirectories. Existing nonempty data without its original configuration is refused, not wiped or silently adopted.

Run the read-only post-installation check after the application has started:

```bash
sudo python3 -I /opt/drivetheater/scripts/doctor.py --env /etc/drivetheater/install.env
```

It checks the installation identity, private paths, free space, a read-only SQLite `quick_check`, the worker heartbeat, local FFmpeg tools, and installed Docker/Compose version commands. It never displays secret values, installs packages, starts services, or repairs/deletes data. A warning or error produces a nonzero exit status; use `--json` for automation.

## Repeating setup

Run the same installer again after correcting a failure. If the original configuration exists, credentials and runtime identity are preserved. A partial initialization between writing the private configuration and its marker is recovered only while the runtime subdirectories remain empty. Populated data with a missing marker is treated as a recovery case.

Once installed, the installer does not silently advance the source version. Use the separate update command to change versions. Configuration and database passwords are not regenerated on restarts; changing the app password updates SQLite, not the stored original bootstrap password.

## Login

The installer displays initial credentials only on fresh setup. Retrieve them later locally using:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh login
```

Use Settings to change the app password. The `login` command retrieves the original generated password, not a reset/bypass of a changed password. Keep private configuration and backups private.

## Update and maintenance

An update checks that source is clean and belongs to the intended repository, fetches a reviewed commit/tag, stops only DriveTheater, makes a consistent SQLite backup and private configuration archive, changes the source version, rebuilds, and waits for database/worker readiness.

```bash
sudo bash /opt/drivetheater/scripts/manage.sh update REVIEWED_COMMIT
```

Updates require downtime. A backup failure attempts to restart the old application without changing source. A build/start/readiness failure after the source switch leaves the backup and logs for manual recovery; **no automatic database rollback** occurs.

## Backup scope

```bash
sudo bash /opt/drivetheater/scripts/manage.sh backup
```

Uses SQLite's backup API and verifies database integrity. Backups include the installer configuration, SQLite contents, private provider configuration, and a checksum manifest under the data directory's `backups/`. Cached videos/derived assets are **not** copied. The live database backup API is consistent; coordinate a maintenance stop if a fully aligned snapshot of changing external credential files is required.

Copy backups to another device/location for disaster protection. Backups on the server's root disk do not survive that disk being lost. Restore must be rehearsed separately; checksum/integrity checks are not proof of a successful disaster restore.

## Manual recovery

Keep the backup directory and inspect `manage.sh logs` and `manage.sh status`. Do not remove volumes, delete data, format storage, force-reset the source, or overwrite the original installer configuration merely to make checks pass.

A recorded backup manifest identifies the previous source revision. For rollback, first stop only DriveTheater, preserve the failed state to another directory, review database/schema compatibility, restore the matching database/configuration deliberately, and select the corresponding reviewed source version. Use an SQLite-aware restore with no open writers. This is an operator-reviewed procedure, not an automatic command that can discard newer user data.

## Ports and exposure

Only the web port is published. The initial app serves HTTP; use a trusted network/VPN, or independently configure a TLS reverse proxy for internet access. Google OAuth for a remote host generally requires a valid HTTPS callback; an installer cannot authorize a Google account or configure your DNS by itself.

## Validation limits

The development workstation is Windows and does not provide Docker or a disposable Ubuntu host. Python configuration/backup tests, shell syntax, local HTTP fixtures and actual FFmpeg media generation are separate from a real Ubuntu/apt/Compose startup. See the actual verification report; never describe those checks as a verified fresh-host deployment.
