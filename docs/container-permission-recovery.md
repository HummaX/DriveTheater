# Recover from the container application-permission error

This guide addresses this **specific** error in both the web and worker logs:

```text
PermissionError: [Errno 13] Permission denied: '/app/drivetheater/__init__.py'
```

It does not diagnose unrelated permission errors in `/data` or provider configuration. Run server commands on your Ubuntu server, not on your computer before connecting over SSH.

## What went wrong

The installer deliberately uses `umask 077` to keep private configuration and runtime files private. A Git checkout created under that mask can have directories readable only by their owner and source files with mode `0600`. The previous Dockerfile copied those modes into the image as root-owned files, then ran the app as a different, non-root user. Both processes failed while reading the application package, before the web listener could stay running.

The image fix normalizes **only immutable application code and built UI assets** under `/app/drivetheater` and `/app/ui`. It makes those directories traversable and files readable, but not writable by the runtime user. It also makes `/app` traversable. The `/data` mount, host data directory, provider configuration, credentials, and administrator password are not changed.

A build-time check runs after `USER 10001:10001`: it imports the package and reads the code/UI assets, rejecting unreadable assets or writable application files/directories. This prevents the same failure from being silently shipped in a successful build. It is not a full application-startup or provider test.

## Before retrying

**Publication is required first.** A local Dockerfile edit is not available from GitHub. Use the full published commit containing the permission fix. The older `027ea152a3e4ed2fdc6200b490a34d7520099bee` revision fixes the Ubuntu release-name bug, but does **not** contain this image-permission fix.

Do not:

- Delete `/etc/drivetheater`, `/var/lib/drivetheater`, cached media, databases, or Docker volumes.
- Use `chmod 777`, recursively change private-storage ownership, or run the app as root.
- Edit the tracked Dockerfile in `/opt/drivetheater` as a quick workaround; the updater intentionally refuses local source changes.
- Re-run an old pinned installer expecting it to select a newer revision. Existing configured installations retain their original app revision on installer reruns.

The accidentally combined command ending in `--port 8080sudo apt-get update` was rejected during argument parsing. It is not the cause of this container error.

## Recovery for an initialized installation

This path applies when the installer created the private configuration and built `drivetheater:local`, as in the reported failure. The current update command can back up initialized configuration even when no application database has been created yet. It requires the original private installation marker/directories, a clean source checkout, and the previous local image.

### 1. Stop the restart loops without deleting data

```bash
sudo bash /opt/drivetheater/scripts/manage.sh stop
```

### 2. Check that the checkout is clean

```bash
sudo git -C /opt/drivetheater status --porcelain --untracked-files=all
```

No output means Git sees no source changes. If files are listed, stop and preserve/review them. Do not use `git reset --hard`, `git clean`, or delete files to pass this check.

### 3. Verify the existing installation and make a private backup

```bash
sudo python3 -I /opt/drivetheater/scripts/bootstrap_config.py verify --env-file /etc/drivetheater/install.env
```

This prints an installation-identity confirmation without showing passwords. If verification fails, stop and preserve the existing files rather than reinitializing storage.

```bash
sudo bash /opt/drivetheater/scripts/manage.sh backup
```

Keep the printed backup directory. Backups include private configuration and any existing database, but not cached videos. Copy them securely off the server if possible; never publish them.

### 4. Update to the published permission-fix revision

**Template only:** replace `PUBLISHED_PERMISSION_FIX_COMMIT` with the actual full Git commit supplied after the fix is published. Do not run it with the placeholder, the old `027ea15` revision, or an unspecified default branch.

```bash
sudo bash /opt/drivetheater/scripts/manage.sh update PUBLISHED_PERMISSION_FIX_COMMIT
```

The updater preserves configuration and credentials, creates its own backup, builds the replacement image, recreates the app containers, and checks readiness. Building can take several minutes. It may briefly start the old image while building; the same old restart error during that interval does not establish whether the replacement failed. Read the command's final result.

If the update fails, keep the output and backup. The updater attempts prior-image/source recovery, **not a database rollback**. In this case the prior image is already broken, so reverting to it cannot restore playback. Do not keep rerunning old installer commands or delete private data.

### 5. Confirm the services and readiness

```bash
sudo bash /opt/drivetheater/scripts/manage.sh status
```

Both services should stay running rather than repeatedly show `Restarting`. For this installation's port 8080:

```bash
curl --noproxy '*' --fail --show-error http://127.0.0.1:8080/api/ready
```

Check that the JSON contains `"status":"ok"`; HTTP success alone does not prove worker readiness. Use the actual configured port if different. If either service still restarts or readiness is not OK, inspect its **new** error:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh logs web
```

```bash
sudo bash /opt/drivetheater/scripts/manage.sh logs worker
```

Review logs for private values before sharing them. A later, different error needs its own diagnosis; this fix only addresses image asset permissions.

### 6. Retrieve your original login

```bash
sudo bash /opt/drivetheater/scripts/manage.sh login
```

This prints the original generated password, not a reset of a later password change. Use the existing SSH-tunnel/HTTPS setup to sign in. The public HTTP listener is not encrypted.

## Verification boundaries

The regression suite checks Dockerfile ordering/scope and executes the actual build-gate Python with synthetic readable, unreadable, missing, and writable fixtures. A separate POSIX-only regression runs the real normalization commands against restrictive temporary code/UI permissions, verifies a non-root read, and checks that a private-data sentinel remains unchanged. Windows cannot verify Linux ownership enforcement and skips that POSIX test.

The Dockerfile's gate will execute on a real image build. Adding the gate or passing local tests does **not** mean a real Docker build, server startup, update, or complete recovery was performed here. No live provider, torrent, model, credential migration, or user-server command is part of these local checks.
