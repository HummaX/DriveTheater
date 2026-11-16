# Security and deployment boundaries

## Secrets and private state

Runtime credentials belong outside Git. The installer creates `/etc/drivetheater/install.env` as a private file and stores persistent runtime state below the selected data directory. Do not commit, publish, paste into tickets, or place in public backups any installation environment file, database, provider credential, access token, generated password, or configuration archive.

Keep database/configuration backups private and encrypt them in transit and at rest according to the deployment environment. Cached media is deliberately excluded from the built-in backup scope. A backup's presence and checksum do not prove that a recovery procedure works: test restoration in an isolated environment with a new, authorized login.

## Network exposure

Choose a stable domain name before exposing the service beyond a trusted network. For internet access, put a maintained HTTPS reverse proxy in front of the application, obtain and renew certificates through the chosen proxy or platform, and set the public URL to the HTTPS origin used by browsers and OAuth redirects. Restrict direct application-port access so clients reach the proxy rather than an unencrypted backend.

Apply host or cloud firewall policy narrowly: allow only the web ports required by the chosen proxy, and preserve a tested administrative SSH path before changing inbound rules. Restrict SSH by source network, strong authentication, and operational access policy appropriate to the organization. Do not make firewall changes until an alternate administrative route is confirmed.

DriveTheater does not automatically configure DNS, TLS certificates, reverse proxies, firewalls, or SSH policy. These controls require operator review and environment-specific implementation.

## Least privilege and recovery

The runtime service uses a dedicated non-login identity and should receive only the data directories it needs. Keep the source checkout separate from persistent data. Do not mount unrelated host paths into the application, run the service as root, or loosen private directory permissions to work around a diagnostic warning.

The doctor command is intentionally read-only:

```bash
sudo python3 -I /opt/drivetheater/scripts/doctor.py --env /etc/drivetheater/install.env
```

It reports configuration/path safety, storage, SQLite integrity, worker heartbeat age, and local tool availability without printing secret values or changing the host. Investigate failures with logs and backups; avoid destructive "repair" actions such as deleting database files, credentials, cache directories, or Docker volumes.

## Fresh installations

Use a new Ubuntu 24.04 host or isolated target for fresh-install rehearsals. Each installation needs new runtime credentials and a separate private backup location. Do not carry cached media into a new installation. A full Docker/Ubuntu deployment verification is still required; local tests and the doctor command cannot substitute for it.
