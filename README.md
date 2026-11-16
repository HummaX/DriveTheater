# DriveTheater

Watch videos from your Google Drive and other sources you are allowed to use, through a website running on your own server.

**New to servers? Start with the [step-by-step Ubuntu setup guide](docs/installation.md).** It explains where to type commands, how to open the website, and what to do if something fails.

> **Preview software:** local tests and synthetic playback checks have passed, but a complete Ubuntu 24.04 installation, Docker startup, and live external services have not yet been verified. Try it on a fresh server before relying on it. DriveTheater is a separate project, not an upgrade for DriveConnector.
>
> **Already saw `fatal: invalid refspec '24.04.4 LTS (Noble Numbat)'`?** That is an installer bug, not a broken Ubuntu installation. Read [how to recover from this exact error](docs/installation.md#invalid-refspec-after-cloning). Do not delete your data or edit the server checkout to work around it.
>
> **Containers restart with `PermissionError` reading `/app/drivetheater/__init__.py`?** Use the [container-permission recovery guide](docs/container-permission-recovery.md). The fix changes immutable image assets, not your stored media, credentials or database.

## What you need

- A server running **Ubuntu 24.04 LTS**, including 24.04.x releases.
- A **64-bit Intel/AMD or ARM64** server.
- SSH access: your hosting provider gives you a server IP address and a login account.
- A `root` account, or an account allowed to run `sudo` commands.
- An internet connection and free disk space. Setup requires at least **2 GiB free**; allow much more for Docker images, original videos, and temporary playback files. Two GiB is a setup check, not a realistic media-storage allowance.
- Videos you are authorized to access. A private Google Drive needs legitimate Google authorization.

You do **not** need to install Python, Node.js, or a database by hand. The installer sets up the required host packages and builds the app using Docker. You do not need an AI API key, a second disk, or manual configuration-file editing.

## Quick setup

**Run server commands on the Ubuntu server, not in your PC's terminal before connecting.**

### 1. Connect to your server

Open Terminal or PowerShell on your own computer. Replace `YOUR_USER` and `SERVER_IP` with the details from your hosting provider:

```bash
ssh YOUR_USER@SERVER_IP
```

For example, if the provider supplied a root login, use `root` in place of `YOUR_USER`. A password may show no characters while you type; that is normal.

### 2. Install DriveTheater

For the beginner setup, keep **port 8080 closed to public inbound traffic in your provider's firewall**. You will open the website through SSH in the next step. Keep SSH access allowed.

The command below installs published preview revision [`1dcbddb`](https://github.com/HummaX/DriveTheater/commit/1dcbddbaee34bedbd13cc2c75c590679af839e7d), using port **8080**. Both the installer download and the application revision are pinned to that same immutable commit. It includes the Ubuntu release-name correction, container application-permission repair, and expanded preview features. Read [the full guide](docs/installation.md#install-drivetheater) for a download-and-review option.

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/HummaX/DriveTheater/1dcbddbaee34bedbd13cc2c75c590679af839e7d/scripts/install.sh | sudo bash -s -- --version 1dcbddbaee34bedbd13cc2c75c590679af839e7d --port 8080
```

This command installs missing packages, starts Docker, downloads the code, builds the app, and creates a new private login. Let it finish; downloading and building can take several minutes. Success ends with **`DriveTheater is ready`** and your initial login details.

If you already have a configured installation, the installer preserves its original port, credentials, data, and app revision. It is not an update command.

### 3. Open the website safely

Keep the server terminal open. Open a **second terminal on your own computer** and run:

```bash
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 YOUR_USER@SERVER_IP
```

Leave this second terminal running. Open **http://127.0.0.1:8080** in your computer's browser. This SSH connection encrypts traffic to the server; closing it closes this browser access.

The app itself listens on all server interfaces by default. The tunnel does **not** make that public listener private: keep port 8080 blocked in your hosting firewall. For a public website, configure HTTPS with a domain and a reverse proxy instead. [Connection options](docs/installation.md#open-the-website).

### 4. Sign in

Use the unique credentials printed by your installer. To display the original login again, run this **on the server**:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh login
```

Change the password in **Settings** after signing in. The command above displays the original generated password; it does not reset a password you changed. Local-preview demo credentials are not server credentials.

## Add your first video

### From Google Drive

1. Open **Sources**.
2. Expand **Connect a Google Drive folder**.
3. Enter a source name and the folder link or ID.
4. Select an authorization method and follow its **Connection guide**. Do not make a private folder public just to simplify setup.
5. Choose **Connect source**, complete authorization if needed, and then **Sync** the source.
6. Open **Drive library**. Sync lists videos; it does not download them.
7. Choose **Play** for one video. Follow its actual download/preparation progress in **Downloads**.

The first release lists videos directly inside each connected folder; it does not scan every subfolder or your entire Drive automatically. Movies and series share the same library. A card marked **Series** opens its seasons and episodes. Public-link support depends on the installed provider tools and Google availability; live provider access is not established by the local tests. See the [Google Drive connection guide](docs/google-drive.md).

### From a direct video link

Open **Drive library → Add URL**, enter a supported link you are allowed to access, then choose **Play** or save it to the server. Adding the link alone does not download the video.

### From Discover

Open **Discover → Add-on connection** and save your authorized Torrentio/Stremio-compatible stream manifest URL. This URL may contain a private token: do not share it.

Choose **Browse movies** or search for a title, open a movie, then explicitly select a stream using **Play** or **Download to server**. Browsing does not silently download torrents. Movie information comes from Cinemeta by default; DriveTheater is not an IMDb service. External add-ons and torrents still require live validation.

## Understand the three actions

| Action | What happens |
|---|---|
| **Play** | Downloads/prepares a title on the server if needed, then opens the player. It bypasses a scheduled download window. Some files must finish downloading before playback can start. |
| **Save to server / Download to server** | Keeps a server-side copy for later. A queued save waits for the configured download window before starting. |
| **Download to device** | Saves the complete cached original to your PC or phone. This appears only after the server has the full original file. Resume is supported; speed depends on your server, connection, and download client. |

A schedule controls when queued saves start; it does not stop an ongoing transfer when the window closes. The incoming Mbps limit currently applies to the built-in HTTP downloader, not every external provider/torrent tool.

The app keeps the **original video quality**: no video resizing, quality menu, or quality ladder. Supported video is repackaged for playback without re-encoding. Audio, previews, thumbnails, and optional captions can still use CPU. If your browser cannot play the original codec, download the original or use a compatible player.

Audio options prefer English, Hindi, and explicitly labeled original tracks. Captions are English only. Missing tracks are not invented. Automatic captions require a separate model/dependency path that has not been live-verified.

## Optional feature expansion

This preview source includes collections and saved filters, durable watched/series state, explicit season-save confirmations, metadata corrections and private PNG posters, notifications/source health, storage previews/integrity/diagnostics, MFA/session/network controls, encrypted-backup and HTTPS helpers, and a public-assets-only PWA shell.

**The expansion is preview code, not deployment certification.** Current Python/API checks pass, but the changed frontend has not yet been TypeScript/Vite-built or browser-verified. Read the [current verification status](docs/release-review.md) rather than treating the earlier preview's frontend checks as approval of this expansion. [Using the features](docs/user-guide.md).

## Everyday server commands

Run these **on the Ubuntu server**. You do not need to be in a particular folder.

**Check the app's containers:**

```bash
sudo bash /opt/drivetheater/scripts/manage.sh status
```

**Show recent logs:**

```bash
sudo bash /opt/drivetheater/scripts/manage.sh logs
```

**Start the app after stopping it:**

```bash
sudo bash /opt/drivetheater/scripts/manage.sh start
```

**Stop DriveTheater without deleting its data:**

```bash
sudo bash /opt/drivetheater/scripts/manage.sh stop
```

**Create a private backup of configuration and database, not cached videos:**

```bash
sudo bash /opt/drivetheater/scripts/manage.sh backup
```

See [maintenance and monthly server replacement](docs/operations.md) for backup storage, updates, and moving your connections to a new server. Never paste private configuration, backups, passwords, or manifest tokens into a public issue.

## Where things are stored

| Server path | Contents |
|---|---|
| `/opt/drivetheater` | Application code. Do not put videos or private credentials here. |
| `/var/lib/drivetheater` | Database, provider configuration, cached media, and backups. |
| `/etc/drivetheater/install.env` | Private installer configuration and initial login credentials. |

Do not delete these folders to fix an error. If setup stops, keep the error output and follow [troubleshooting](docs/installation.md#troubleshooting).

## More help

- [Beginner installation and troubleshooting](docs/installation.md)
- [Using the app](docs/user-guide.md)
- [Maintenance, backups, updates, and replacement servers](docs/operations.md)
- [Optional MFA, session controls and network recovery](docs/security.md)
- [Encrypted offline backups](docs/encrypted-backups.md) and [optional HTTPS](docs/https.md)
- [Google Drive authorization](docs/google-drive.md)
- [Architecture](docs/architecture.md) and [UI design](docs/ui-design.md)
- [Detailed verification and remaining limitations](docs/release-review.md)

Local Python/API/synthetic-media checks cover individual behaviors; they are not proof of a real Ubuntu deployment. The earlier preview passed frontend/browser checks, but the current expansion still needs its own build and browser verification. Optional downloader/transcription tools and models have not been live-validated. The earlier Vite build also warned about bundle size. Use only authorized media; no DRM or access-control bypass is provided.

## License

MIT. Independently implemented for HummaX. DriveConnector is not a runtime dependency.
