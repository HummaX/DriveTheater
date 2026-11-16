# Optional account and network security

These opt-in controls are part of the expanded preview source and are **not enabled by default**. Read the verification and deployment limitations before updating a working server. Preserve its private configuration, credentials and data; do not replace it with an unreviewed local tree just to expose these settings.

Use HTTPS for a public website, or an SSH tunnel with the app port blocked from the public internet. MFA is not a replacement for encrypted transport. Keep a working SSH login before changing account or network settings.

## Add an authenticator

1. Sign in and open **Settings → Security**.
2. Enter your current password to begin MFA enrollment. If MFA is already enabled, sensitive changes also require a current authenticator/recovery proof.
3. Add the displayed provisioning secret/URI to a trusted authenticator. Keep it private; never paste it in a support issue or screenshot.
4. Enter a current six-digit code to confirm enrollment. Starting enrollment alone does not enable MFA.
5. Save the recovery codes immediately, outside this server, in a password manager or protected offline record. They are shown once. Each code works once.
6. Before closing the current session, test signing in from another browser/private window. Password entry should lead to a separate factor step; it must not grant access on its own.

TOTP depends on reasonably accurate device and server time. Codes have a narrow clock window and replay protection; a code that just succeeded may be rejected if immediately reused. Wait for the next code rather than repeatedly submitting the same one. Factor failures are rate-limited.

Recovery codes are not a second password to share. Rotating them invalidates the old set. Disabling MFA or changing other sensitive controls requires current proof. The app does not provide an unauthenticated MFA bypass. The command that displays the original bootstrap login does **not** reset a changed password or MFA.

MFA secrets are encrypted using the installation secret. Preserve the private installation configuration with the database: losing/changing that secret can make stored MFA/channel secrets unusable. A private setup bundle intentionally does not migrate accounts or MFA to a fresh server.

## Review or revoke sessions

The Security panel shows opaque session IDs, browser/device labels and observed session times—not cookies or token hashes. Device labels are best-effort labels derived from the user agent, not trusted device attestations.

Use the revoke action for a selected session, other sessions, or all sessions. Revocation takes effect on the next authenticated request. Revoking the current/all sessions signs you out. A password change invalidates existing sessions and pending MFA/network confirmations.

New-network alerts use keyed network markers rather than exposing raw addresses in notices. They are an operational hint, not evidence of an intrusion or proof that a device is yours. Ordinary **Clear history** does not erase the separate security audit, durable watched state, or notifications. Security events have their own bounded retention.

## Network allowlist and trusted proxy gate

Do not enable these unless you know which address the app actually observes. NAT, VPNs, Docker networking, mobile address changes and a reverse proxy can change it.

- An IP allowlist accepts explicitly configured IPv4/IPv6 CIDRs. An empty enabled list is refused.
- Forwarded addresses are ignored unless the **immediate socket peer** is inside the explicitly trusted proxy CIDRs. Only the trusted right-hand part of a bounded `X-Forwarded-For` chain is processed.
- The optional proxy gate requires a trusted proxy to supply `X-DriveTheater-User` matching the existing administrator. Configure that proxy to discard client-supplied identity headers and set its own verified assertion.
- A proxy assertion is an **additional gate**. It never creates a session and never bypasses the app password or MFA.
- Trust only the specific proxy network you control. Do not trust `0.0.0.0/0` or `::/0` as a convenience. Firewall direct access to the backend when using a public proxy.
- The Compose web command disables Uvicorn proxy rewriting so the app can inspect the socket peer itself. A custom launch must also use `--no-proxy-headers`; do not layer a second, permissive forwarded-header trust policy in front of these checks.

Preview the change first. The app checks whether the proposed policy would admit the current request, then issues an account-bound, expiring, one-use confirmation. Apply requires password/factor proof and rechecks the current request. A new proxy gate must already be correctly supplying its verified header during preview; the preview does not configure the proxy for you.

Preview protects against obvious current-request lockout, not every future network change. Keep SSH available and test a second browser before closing your working session. Malformed stored policy fails closed; there is no silent permissive fallback. Only loopback liveness/readiness probes have a narrow exemption.

## Recover from a network-policy lockout

Use this only for the network policy, from a trusted **SSH terminal on your Ubuntu server**. It does not recover a lost password, authenticator, recovery codes, or installation encryption secret.

The helper requires local root access and an explicit confirmation. It first makes a private pre-change SQLite snapshot, then removes only the network policy and pending network confirmations. It records a security event and preserves users, passwords, MFA, sessions, media, and other settings.

```bash
sudo bash /opt/drivetheater/scripts/manage.sh recover-network --confirm-network-reset
```

Keep the printed snapshot path private. Reopen the app through a safe tunnel/HTTPS path, inspect the policy, and deliberately reconfigure it. Do not delete the database, replace the installation secret, remove Docker volumes, or force-reset the repository to fix this lockout.

The local helper and HTTP/security tests are synthetic. A real Ubuntu lockout/recovery or deployment rehearsal was not performed.

## Private state and least privilege

Keep runtime credentials outside Git. The installer uses private `/etc/drivetheater/install.env` and persistent state under the selected data directory. Database/configuration backups contain account/provider secrets even when cached media is excluded. Keep them private and deliberately encrypt copies for transport/storage; a checksum alone does not prove recoverability.

Run the service with its dedicated non-root identity, give it only the data directories it needs, and keep the checkout separate from persistent state. Do not mount unrelated host paths or the Docker socket, run the service as root, or relax private data permissions to make a warning disappear. Preserve tested SSH access before any host/cloud firewall change. The app does not configure that firewall or SSH policy for you.

The read-only doctor is a useful first diagnostic, not an automatic repair or deployment certification:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh doctor
```

Use the private-backup and fresh-server procedures deliberately. A replacement server gets new account credentials; never reuse credentials from a demo or an earlier conversation.

## Safe support information

Use the allowlisted support export for aggregate diagnostics when appropriate. Review any information before sharing it. Never attach a private backup, setup bundle, raw database, authenticator secret, recovery code, cookie, signed provider URL, notification configuration, or `/etc/drivetheater/install.env` to a public issue.

See [HTTPS](https.md), [encrypted offline backups](encrypted-backups.md), and [maintenance](operations.md) for the separate operator workflows.
