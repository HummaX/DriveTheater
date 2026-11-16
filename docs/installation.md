# Set up DriveTheater on Ubuntu 24.04

[Back to the README](../README.md) · [Using the app](user-guide.md) · [Maintenance and moving servers](operations.md)

This guide is for a fresh **Ubuntu 24.04 LTS** server. You do not need programming experience. Run one command at a time, wait for it to finish, and keep any error message if a step fails.

> **Preview status:** a complete real Ubuntu/Docker installation has not yet been verified. The installer has local regression tests, including the Ubuntu release-name bug described below. Do not treat those tests as proof that a real server installation has finished successfully.

## Before you start

Have these details ready from your hosting provider:

| What you need | Example or explanation |
|---|---|
| Server operating system | Ubuntu 24.04 LTS, including releases such as 24.04.4. |
| Server address | The public IP address shown in your hosting dashboard. |
| SSH login | Usually `root`, `ubuntu`, or another account chosen by your provider. Use the actual supplied account. |
| SSH authentication | Your SSH key or server password. |
| Administrator access | Either root, or permission to use `sudo`. |
| Disk space | At least 2 GiB free to pass the initial check, plus substantially more for Docker images, original videos, and playback files. |

Supported server processors are 64-bit Intel/AMD (`x86_64`) and ARM64. A second disk is not required. No formatting or mounting is part of the normal setup.

**For a safe first login:** keep incoming port **8080 blocked from the public internet in your hosting provider's firewall**, but leave SSH allowed. You will use an encrypted SSH tunnel to reach the app. Outgoing internet access is needed to download packages and images.

Do not disable the firewall or change SSH rules blindly. Docker-published ports may bypass assumptions you make about local UFW rules; use the hosting provider's network firewall for this beginner path.

## Connect to the server

On your **own PC or Mac**, open Terminal or PowerShell. Replace the capitalized placeholders with your actual details; do not type the placeholder words literally.

```bash
ssh YOUR_USER@SERVER_IP
```

For a provider-supplied root account, replace `YOUR_USER` with `root`. Verify the server fingerprint using your provider's information before trusting a first-time SSH connection. Password input may remain invisible while you type.

After connecting, commands in that terminal run **on the server**. Leave it open for installation and maintenance.

To check the operating system:

```bash
cat /etc/os-release
```

Look for `ID=ubuntu` and `VERSION_ID="24.04"`. A longer display name such as `24.04.4 LTS (Noble Numbat)` is normal.

To check available storage:

```bash
df -h /
```

Use a fresh server if possible. The installer refuses some ambiguous existing-data situations rather than replacing someone else's files.

## Install DriveTheater

### What the installer does

It installs missing Git, Python, Docker, and Docker Compose host prerequisites, starts Docker, creates a dedicated non-login account if the `drivetheater` account does not already exist, downloads the application, and builds two app containers: **web** and **worker**. Containers are the background app processes managed by Docker; you do not need to start them individually.

It uses these default locations:

| Location | Purpose |
|---|---|
| `/opt/drivetheater` | Application source code. |
| `/var/lib/drivetheater` | Private database, provider configuration, cached media, and backups. |
| `/etc/drivetheater/install.env` | Private installer settings, secrets, and the original generated login. |

The normal setup does not reuse DriveConnector credentials or storage. It does not require manually editing an `.env` file.

### Choose the preview revision

These commands pin both the installer download and the application to published preview commit [`1dcbddb`](https://github.com/HummaX/DriveTheater/commit/1dcbddbaee34bedbd13cc2c75c590679af839e7d). The full revision is `1dcbddbaee34bedbd13cc2c75c590679af839e7d`; it includes the Ubuntu release-name correction, the container application-permission repair, and the expanded preview features. Later changes to the `claude-code` branch cannot silently change this installation. To use another reviewed revision, replace the commit in both the installer URL and the `--version` argument.

**Do not use the older `42d2f9f9e1fcc7397957ce7d9482f0ce3e191742` bootstrap for a new attempt.** That immutable script contains the Ubuntu release-name bug. Updating a branch does not change an old commit URL; use the corrected commit pinned in this guide.

### Quick install

Run this **on the server**. It uses port **8080** to avoid taking over port 80 from another website:

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/HummaX/DriveTheater/1dcbddbaee34bedbd13cc2c75c590679af839e7d/scripts/install.sh | sudo bash -s -- --version 1dcbddbaee34bedbd13cc2c75c590679af839e7d --port 8080
```

This downloads a script and executes it with administrator privileges. Only run it after reviewing the source and trusting the selected revision. Use the download-first option below if you prefer to inspect the exact file before running it.

If you are already logged in as root and `sudo` is unavailable, remove only the word `sudo` from the command. If `curl` or certificate support is missing, install them first:

```bash
sudo apt-get update
```

```bash
sudo apt-get install -y curl ca-certificates
```

Then retry the install command. These commands change the server; they are not checks to run on your PC.

### Download first and review instead

Use this **instead of** the quick install, not as an additional installation. Run all these commands in the same server terminal so the temporary path stays available.

Create a private temporary folder:

```bash
INSTALL_TMP="$(mktemp -d)"
```

Download the installer:

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/HummaX/DriveTheater/1dcbddbaee34bedbd13cc2c75c590679af839e7d/scripts/install.sh -o "$INSTALL_TMP/install.sh"
```

Read it in the terminal, or review [the installer source on GitHub](https://github.com/HummaX/DriveTheater/blob/1dcbddbaee34bedbd13cc2c75c590679af839e7d/scripts/install.sh) in your browser:

```bash
less "$INSTALL_TMP/install.sh"
```

Press `q` to leave `less`. If that viewer is not installed, use the GitHub source link; do not install a new tool just to read this file. When you are ready:

```bash
sudo bash "$INSTALL_TMP/install.sh" --version 1dcbddbaee34bedbd13cc2c75c590679af839e7d --port 8080
```

### Know when setup has succeeded

Package downloads and image builds can take several minutes. Do not close the terminal while they are running.

A completed install prints:

- **`DriveTheater is ready`**, followed by an address.
- The data directory.
- Your newly generated admin username and password on a fresh install.

Messages such as `No services need to be restarted` or `Running kernel seems to be up-to-date` are normal Ubuntu package messages. They do **not** mean DriveTheater itself is ready. If the command ends in an error, resolve that error before continuing.

## Open the website

### Recommended first connection: SSH tunnel

Keep port 8080 closed in your provider's public firewall. On your **own computer**, open a second terminal and run:

```bash
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 YOUR_USER@SERVER_IP
```

A blank terminal after authentication is normal: the tunnel is running. Leave it open, then visit **http://127.0.0.1:8080** in your computer's browser.

The local `127.0.0.1` address means your own computer; the SSH tunnel carries the request to the server. Closing that terminal disconnects this route to the app but does not stop DriveTheater.

**Important:** the app's default server listener is `0.0.0.0`, meaning all server interfaces. The tunnel encrypts your browser connection but does not restrict the listener. Keep the app port blocked from public access.

If local port 8080 is occupied on your computer, forward local port 8081 instead:

```bash
ssh -N -L 127.0.0.1:8081:127.0.0.1:8080 YOUR_USER@SERVER_IP
```

Then browse to **http://127.0.0.1:8081**. The server still uses 8080.

### Other connection options

On a trusted private network or VPN, the app is reachable at `http://SERVER_IP:8080` if your network rules allow it. Plain HTTP is unencrypted; do not use it for passwords over an untrusted network.

For a public internet website, configure a domain, HTTPS certificate, and TLS reverse proxy separately. The installer does not create DNS records or configure that proxy. Restrict direct access to the underlying HTTP port.

For a **fresh installation** behind an already configured HTTPS proxy, the installer supports `--public-url https://media.example.com`. This sets the app's public origin and secure-cookie behavior; it does not enable HTTPS by itself. Remote Google OAuth generally needs an exact valid HTTPS callback. Use the Google connection guide rather than guessing the callback URL.

If you omit `--port`, the installer's default is **80**, not 8080. An already configured installation keeps its original port even when you rerun setup with different flags; use the original port in your tunnel and checks.

## Sign in and add a video

Use the credentials printed at the end of installation. To display the original generated login again, run this **on the server**:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh login
```

After signing in, change your password in **Settings**. The `login` command does not reset or reveal a later changed password. Do not use the disposable local-preview login on a real server.

For your first Google Drive source:

1. Open **Sources → Connect a Google Drive folder**.
2. Enter a name and a folder link or ID.
3. Choose the appropriate authorization method and read its **Connection guide**. Private folders require Google authorization; do not change sharing to public just for convenience.
4. Choose **Connect source**, complete any authorization, then **Sync** the source.
5. Open **Drive library**, select a title or series episode, and choose **Play**.
6. Check **Downloads** for progress. Browsing/syncing alone does not download media.

For an authorized direct video URL, use **Drive library → Add URL**. For movie discovery and explicit Torrentio stream selection, use **Discover**. See [the user guide](user-guide.md) and [Google Drive connection guide](google-drive.md) for details and provider limitations.

## Check that the app is running

On the server:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh status
```

Look for the **web** and **worker** services running. Container status alone is not a complete end-to-end playback test. Check application readiness too:

```bash
curl --fail --show-error http://127.0.0.1:8080/api/ready
```

A successful response includes `"status":"ok"`. Use your actual configured port if it is not 8080.

For recent app output:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh logs
```

The optional read-only diagnostic is:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh doctor
```

It does not repair or delete anything. Some checks look for FFmpeg on the host, while normal Docker installation places FFmpeg inside the app image; a host-tool warning alone is not proof that container playback is broken. A deliberately stopped worker also causes a heartbeat warning. Investigate warnings rather than deleting files to clear them.

## Troubleshooting

### Invalid refspec after cloning

Example:

```text
Cloning into '/opt/drivetheater'...
fatal: invalid refspec '24.04.4 LTS (Noble Numbat)'
```

**Cause:** the old installer stored the requested app revision in a variable called `VERSION`. Reading `/etc/os-release` replaced it with Ubuntu's human-readable version. Git then tried to fetch that display name instead of the requested commit or branch.

**The fix:** newer installer source uses `APP_VERSION` and reads OS information in an isolated subshell. Regression tests use the exact point-release label shown above.

To recover with the corrected installer pinned in this guide:

1. Keep `/opt/drivetheater`, `/var/lib/drivetheater`, and `/etc/drivetheater` intact. Do not reset the server, delete files, or reinstall Ubuntu.
2. Fetch the **corrected bootstrap**, using the install instructions earlier in this guide. Do not rerun the old immutable commit URL.
3. Run the downloaded script from its temporary folder, not by editing the tracked installer inside `/opt/drivetheater`. The installer checks that its checkout is clean and will refuse local source modifications.
4. For a fresh attempt stopped at this exact Git failure, the installer can reuse the clean clone. This failure occurs before the script creates private app configuration or starts app containers; prerequisite packages and Docker may already have been installed/started.
5. If private app configuration already exists from another attempt, preserve it. Rerunning setup preserves that installation's settings and revision rather than treating it as fresh.

Do not use `manage.sh update` to recover this aborted fresh install. Updating requires an initialized installation and a previous app image.

To inspect the source checkout without changing it:

```bash
sudo git -C /opt/drivetheater status --short
```

No output means Git sees no local changes. If it lists files, preserve and review them before retrying. Do not use `git reset --hard` or `git clean` as a generic fix.

### The website will not open

- Check that installation actually printed `DriveTheater is ready`.
- Check **web** and **worker** with `manage.sh status`.
- Run the local `/api/ready` request on the server. If it fails, inspect logs before changing firewall settings.
- If readiness works on the server, check the SSH tunnel, local browser address, and port numbers.
- `127.0.0.1:8080` on your computer needs the tunnel; it is not your remote server without it.
- A public HTTP address will be blocked if you followed the recommended provider-firewall setup. Use the tunnel, not a public HTTP login.

### Port is already in use

The installer does not stop other websites or services. On a fresh install, use `--port 8081` and update the server-side tunnel destination and readiness URL accordingly. Changing `--port` on an existing configured installation does not rewrite its port.

### Readiness failed

Keep the private configuration and data. Run `manage.sh logs` and `manage.sh status` if configuration has been created. A newer installer reports the preservation and logs instructions explicitly, including when the readiness wait times out. Do not assume a failed readiness check means the database should be deleted.

### The management command says the app is not initialized

Setup may have stopped before `/etc/drivetheater/install.env` was created. Resolve the installer failure and retry with the corrected bootstrap. Login, backup, and update commands are for an initialized installation, not a substitute for finishing setup.

### A video will not play or download

Use **Downloads** to read the actual job error. Sources may need authorization, an installed provider tool, or a supported URL. Some videos must download completely before playback. A browser that does not support the original codec may need a compatible player or **Download to device** after the complete original is cached; video conversion is intentionally disabled.

### Sharing an error safely

Share the last relevant error lines after removing private URLs, tokens, cookies, passwords, and account data. Do not upload `install.env`, provider JSON, private setup bundles, or backups to GitHub. Logs can contain sensitive details too; review them first.

## Next time

You do not reinstall for normal use. Use [the maintenance guide](operations.md) to start/stop the app, back up data, update to a reviewed revision, or move connections to a replacement server. An update or restore is a separate operation; a successful fresh setup does not prove those recovery paths have been tested.
