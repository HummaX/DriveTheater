# Maintain DriveTheater and move to a new server

[Back to the README](../README.md) · [First-time setup and troubleshooting](installation.md)

Unless a step explicitly says **your computer**, run its commands in an SSH terminal connected to your **Ubuntu server**. These examples use the default application folder, `/opt/drivetheater`.

## Everyday commands

### Check whether the app is running

```bash
sudo bash /opt/drivetheater/scripts/manage.sh status
```

Look for the **web** and **worker** services. To see recent output:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh logs
```

For only the worker's recent output:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh logs worker
```

Review logs before sharing them; remove private URLs, account details, and tokens.

### Stop or start the app

Stop only DriveTheater, without deleting its database or cached videos:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh stop
```

Start it again:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh start
```

A deliberate stop means playback and downloads stop until you start the app again.

### Display the initial login

```bash
sudo bash /opt/drivetheater/scripts/manage.sh login
```

This displays the original generated login. It is **not** a password reset, and the original password no longer works if you changed it in the app.

### Run a read-only check

```bash
sudo bash /opt/drivetheater/scripts/manage.sh doctor
```

This checks the private installation paths, free disk space, database integrity, worker heartbeat, and available tools. It does not install packages, contact media providers, repair files, or delete data. A warning or error returns a nonzero exit code.

A stopped worker is expected to cause a heartbeat warning. Some tool checks run on the host, while Docker keeps FFmpeg inside the app image; do not install host tools just to silence a warning without checking the actual service logs.

## Backups versus replacement-server setup bundles

They solve different problems:

| Option | Use it for | Includes | Does not include |
|---|---|---|---|
| **Backup** | Keeping database/configuration for a carefully planned recovery. | Database (including accounts/MFA/session state), private provider configuration, custom artwork, original installer settings, and a checksum manifest. | Cached videos and generated media assets. |
| **Private setup bundle** | Moving connections/settings to a fresh monthly server without carrying over user data. | Drive connections, private provider/add-on configuration, and supported app settings. | Library/video records, cached videos, user accounts, sessions, watch history, and the old app administrator login. |

**Both can contain secrets.** Keep them off GitHub and away from public web folders. A file saved only on your server is lost if that server or its disk is lost; keep a secure copy elsewhere.

## Create a backup

Run:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh backup
```

The command prints the created backup directory under `/var/lib/drivetheater/backups/`. Keep the **whole directory**, not only the database file. Copy it securely to storage you control outside this server.

The database copy uses SQLite's backup API and checks its integrity. Cached media is not backed up. If you need an aligned snapshot of both the database and changing provider credential files, pause changes and plan a maintenance stop before creating it.

**Restoring is not automatic.** A recovery must match the app version and database format, with no app processes writing to the database. Do not overwrite a live database or delete a failed installation just because a backup exists. A successful backup check is not proof that a disaster restore has been rehearsed.

## Encrypt and verify a backup

The normal `backup` directory is private **plaintext**, not an encrypted archive. It now includes validated custom poster artwork; disposable cached video/previews/captions are still excluded. The database snapshot is materialized as a standalone SQLite file, so it must not need live WAL/SHM sidecars.

Use this only on a reviewed version containing the optional helper, with its reviewed host `cryptography` dependency already available. These example paths are templates: replace `SNAPSHOT` with the directory printed by `backup`, and choose a new archive filename in an existing private directory outside that snapshot.

```bash
sudo bash /opt/drivetheater/scripts/manage.sh encrypt-backup /var/lib/drivetheater/backups/SNAPSHOT /private/offline/SNAPSHOT.dtb
```

Enter an independent strong backup passphrase at the hidden prompt. Do not put it in the command, environment, setup bundle, or shell history. Store it separately from the archive.

```bash
sudo bash /opt/drivetheater/scripts/manage.sh verify-backup /private/offline/SNAPSHOT.dtb
```

Verification authenticates the encrypted archive, checks its bounded/allowlisted members and hashes, and checks SQLite integrity in private temporary storage. It **does not restore**, prove provider credentials still work, or certify a disaster recovery. It never deletes the original plaintext backup. Keep that original private and retain it until your own verified retention process permits removal. See [encrypted backup details and limits](encrypted-backups.md).

## Security and HTTPS maintenance

[Optional MFA, session controls and network-policy recovery](security.md) use their own deliberate confirmation steps. If an IP/proxy policy locks you out, use the root-only network recovery helper over SSH; it preserves password, MFA, sessions, and media. Do not reset the entire installation.

[HTTPS preflight/configuration](https.md) is optional. The helper never installs/reloads a proxy, requests ACME certificates, takes over ports, or changes DNS/firewalls. Apply only through your own reviewed server change process.

## Update an existing installation

Do this only after setup has completed successfully. `update` is not a repair command for an installer that stopped at the clone/fetch stage.

1. Choose a reviewed full Git commit or release tag that you want to install.
2. Read the release information, including any database compatibility changes.
3. Create a backup and copy it off the server.
4. Expect a brief interruption while services are backed up/replaced and checked.
5. Run the update with that exact revision.

**Example template — replace `REVIEWED_COMMIT` before running:**

```bash
sudo bash /opt/drivetheater/scripts/manage.sh update REVIEWED_COMMIT
```

Do not type `REVIEWED_COMMIT` literally, and do not run a bare `update` expecting it to pick a safe release. The script's fallback is `main`; this project's published preview is on `claude-code`, so always provide the intended revision explicitly. Updating to a moving branch is less reproducible than using a reviewed commit.

An update checks that the checkout is clean and belongs to DriveTheater, backs up private state, builds the replacement, and checks readiness. It attempts to restore the previous app image/source if the replacement fails. **It does not automatically restore or roll back the database.** Stop and inspect errors rather than repeatedly changing versions against uncertain database state.

After updating, check:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh status
```

```bash
sudo bash /opt/drivetheater/scripts/manage.sh logs
```

Then sign in and try an authorized test video. Real Ubuntu install/update/recovery behavior has not yet been fully verified by the repository's local tests.

## Replace your server each month

This path keeps your supported source/add-on connections and settings, but lets the new server create fresh users/secrets. Videos are downloaded again only when requested. You do not need to copy cached media.

### 1. Export settings on the old server

Connect to the **old server**. Export a new private setup file into your login account's home directory:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh export-setup "$HOME/drivetheater-private-setup.json"
```

The export creates a private file. It may be owned by root because the management command uses `sudo`. Give **only this exported file** to your SSH login account so that you can copy it; do not change ownership of the application data tree:

```bash
sudo chown "$(id -u):$(id -g)" "$HOME/drivetheater-private-setup.json"
```

Keep it readable only by its owner:

```bash
chmod 600 "$HOME/drivetheater-private-setup.json"
```

Use a fresh filename if an export already exists. Do not overwrite or delete your only good setup copy as a generic troubleshooting step.

### 2. Copy the file through your computer

On **your own computer**, open a terminal. Replace the login names and IP addresses; you may have different users on the old and new servers.

Download the setup file from the old account's home directory:

```bash
scp OLD_USER@OLD_SERVER_IP:drivetheater-private-setup.json ./drivetheater-private-setup.json
```

Upload it to the new account's home directory:

```bash
scp ./drivetheater-private-setup.json NEW_USER@NEW_SERVER_IP:drivetheater-private-setup.json
```

SSH/SCP encrypts this transfer. Your local copy is still a sensitive credential-bearing file: store it privately, not in a public/shared folder or repository. Keep the old server available until the new one works.

### 3. Install on the fresh new server with the bundle

Connect to the **new Ubuntu 24.04 server**. Keep SSH allowed and the app port closed in its provider firewall, as explained in [first-time setup](installation.md).

Restrict the uploaded setup file:

```bash
chmod 600 "$HOME/drivetheater-private-setup.json"
```

Review the selected installer revision. The following pins the installer and app to published preview commit [`1dcbddb`](https://github.com/HummaX/DriveTheater/commit/1dcbddbaee34bedbd13cc2c75c590679af839e7d), including the Ubuntu release-name correction, container permission repair, and expanded preview features. It is not a moving branch.

**If your bundle contains Google OAuth connections, do not run the command unchanged.** Configure the new DNS/HTTPS proxy first and add `--public-url https://YOUR_ACTUAL_DOMAIN` to this fresh-install command, even when reusing the old domain. Import builds the stored OAuth callback from that option; omitting it leaves the callback unavailable. Changing the flag on a later installer rerun does not rewrite the existing configuration. Use your actual HTTPS website to sign in, rather than the default plain-HTTP tunnel address.

For connections that do not need a public OAuth callback:

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/HummaX/DriveTheater/1dcbddbaee34bedbd13cc2c75c590679af839e7d/scripts/install.sh | sudo bash -s -- --version 1dcbddbaee34bedbd13cc2c75c590679af839e7d --port 8080 --setup-bundle "$HOME/drivetheater-private-setup.json"
```

Use an absolute bundle path; `$HOME` expands to one before `sudo` runs. Import is for a **fresh installation only**. It is intentionally refused if the new server already has a configured DriveTheater installation. Do not delete an existing configuration to force an import. The installer does not configure HTTPS itself; some providers may require authorization again after migration.

### 4. Check before retiring the old server

- Installation finished with `DriveTheater is ready`.
- Both **web** and **worker** are running.
- You can open the app through the SSH tunnel or properly configured HTTPS.
- You can sign in using the **new server's generated credentials**.
- Sources and supported settings appear; reconnect any provider that requires authorization.
- In **Sources**, **Sync** each Drive folder to rebuild the video list; setup bundles do not include library/video records.
- An authorized test video can be downloaded/prepared and played.
- A new backup has been created and copied somewhere safe.

Old users, sessions, watch history, and media are not migrated by a setup bundle. The initial password is new. Do not cancel the old server based solely on a successful import message.

## If something fails

Preserve the error output, source checkout, configuration, data, backups, and setup bundle. Read [installation troubleshooting](installation.md#troubleshooting), especially if you see the Ubuntu `invalid refspec` error.

Do not format storage, remove Docker volumes, force-reset source, or delete `/etc/drivetheater` or `/var/lib/drivetheater` to make a check pass. Restoring database/configuration is a separate, deliberate recovery operation, not a normal monthly setup step.
