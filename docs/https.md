# HTTPS reverse proxy with Caddy

[Back to first-time setup](installation.md) · [Operations](operations.md)

DriveTheater's standard installer intentionally exposes plain HTTP. Use this **optional** guide only when you operate a public DNS name and understand your server's firewall and provider controls. The helper below generates a new Caddyfile; it never installs Caddy, starts or reloads services, changes a firewall, requests a certificate, contacts DNS, or changes DriveTheater.

> Keep SSH access working throughout. This guide does not replace the SSH-tunnel path.

## What you need

- A public DNS hostname such as `media.example.com` (not an IP address or `localhost`).
- Control of the DNS record and of inbound TCP ports **80** and **443** at both the cloud/provider and server firewall layers.
- An Ubuntu host with DriveTheater reachable only at a loopback backend port, such as `127.0.0.1:8080`.
- An operator-approved Caddy installation. Obtain and maintain Caddy separately according to its official documentation.

The supplied `compose.yaml` defaults to `DT_BIND=0.0.0.0` and `DT_PORT=80`; that is not suitable for a Caddy backend on the same host. Choose a different backend port such as `8080`, bind it to `127.0.0.1`, and keep public traffic at Caddy instead. Do **not** run Caddy when another service owns ports 80 or 443.

## 1. Do a local, read-only preflight

On the server checkout, run this command without `sudo` unless the checkout itself requires it:

```bash
python3 -I /opt/drivetheater/scripts/https_setup.py preflight --domain media.example.com --port 8080
```

The JSON result has these stable fields:

- `domain` and `public_origin` are the validated hostname and required `https://` origin.
- `backend` is always the loopback upstream Caddy will use.
- `checks.caddy_port_80` and `checks.caddy_port_443` report local loopback listener facts: `free`, `occupied`, or `unknown`.
- `checks.backend_loopback` says whether DriveTheater currently accepts a local connection. A missing listener is not repaired by this command.
- `external.dns`, `external.acme`, and `external.firewall` always remain `unknown`. Check them yourself from an independent network; the helper deliberately makes no public network probes.

Stop at an `occupied` Caddy port and identify its owner. Do not stop unrelated services merely to make the check pass. An `unknown` result requires host-level investigation before an apply attempt.

## 2. Make DriveTheater loopback-only and HTTPS-aware

Back up `/etc/drivetheater/install.env` first, then carefully review the current configuration. The desired values are conceptually:

```dotenv
DT_BIND=127.0.0.1
DT_PORT=8080
DT_PUBLIC_URL=https://media.example.com
DT_COOKIE_SECURE=true
```

Use your actual domain and selected backend port. `DT_PUBLIC_URL` must match the browser origin exactly; otherwise OAuth callback URLs can be wrong. `DT_COOKIE_SECURE=true` ensures session cookies are not sent over HTTP. This repository does not provide an automatic migration for an existing install configuration: preserve its secrets and current values, make one reviewed change at a time, and use the normal DriveTheater management commands to check readiness after any operator-applied change.

With the loopback backend, your existing private access remains available. For the example above, from **your computer**:

```bash
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 SERVER_USER@SERVER_IP
```

Then browse `http://127.0.0.1:8080` for a local connectivity check. With secure cookies and a public HTTPS origin configured, a plain-HTTP tunnel is **not** guaranteed to support normal sign-in or mutations. Use the intended trusted HTTPS origin for normal operation; do not disable secure cookies just to silence a test. The tunnel does not validate public HTTPS.

## 3. Generate, inspect, and optionally apply a new Caddyfile

Choose a destination that does not already exist. The generator uses exclusive creation and refuses to replace any existing path:

```bash
sudo install -d -m 700 /etc/caddy/drivetheater
```

```bash
sudo python3 -I /opt/drivetheater/scripts/https_setup.py write \
  --domain media.example.com --port 8080 \
  --destination /etc/caddy/drivetheater/Caddyfile
```

```bash
sudo cat /etc/caddy/drivetheater/Caddyfile
```

The generated configuration does all of the following:

- redirects the specified hostname from HTTP to HTTPS;
- proxies only to `127.0.0.1:PORT`;
- preserves Caddy's normal `Range` forwarding and uses immediate upstream flushing for server-sent events;
- adds `Cache-Control: no-store, private` and `Pragma: no-cache` to prevent caching authenticated responses; and
- does **not** configure `trusted_proxies` or accept broad forwarded identities.

It accepts only a plain public DNS hostname and an integer backend port. Schemes, paths, whitespace, Caddy directives, control characters, IP addresses, and `localhost` are rejected rather than interpolated into the configuration.

### Optional operator apply procedure

This is intentionally manual; adapt locations and service names to your Caddy package and site policy.

1. Validate the generated file with Caddy's own validation command before referencing it from the main Caddy configuration.
2. Review the final assembled Caddy configuration, especially other sites that may own ports 80/443 or add trusted proxy settings.
3. Confirm public DNS, inbound TCP 80/443, and provider/network firewall rules independently.
4. Reload Caddy only through your approved change process.
5. From an external network, visit `https://media.example.com`, sign in, test an authorized range/seek request, and verify an event-driven UI action. Then inspect Caddy and DriveTheater logs for errors.

A local preflight or a syntactically valid Caddyfile is **not** evidence that DNS, ACME, firewall policy, certificate issuance, external reachability, or real playback works.

## Rollback

If the HTTPS change fails, preserve logs and the generated file for review. Remove only the Caddy site reference you added through your normal change process, then reload Caddy through that same process. Restore the reviewed prior DriveTheater environment values only if you changed them and have verified the backup. Do not delete `/etc/drivetheater`, `/var/lib/drivetheater`, Docker volumes, application secrets, or an unrelated port owner as a rollback shortcut.

After rollback, the SSH tunnel described above still works when DriveTheater remains bound to loopback. If you deliberately return to direct HTTP exposure, set `DT_PUBLIC_URL` and `DT_COOKIE_SECURE` consistently and treat that as a separate, security-sensitive configuration change.
