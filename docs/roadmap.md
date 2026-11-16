# Delivery and verification roadmap

## Local feature scope

The expansion implements the requested recommendations **2–14** in this preview source. Recommendation **1**, a real Ubuntu deployment/update/restore rehearsal, remains deliberately excluded. Publication does not complete the frontend or deployment verification gates listed below.

| Recommendation | Local implementation |
|---|---|
| 2. HTTPS | Optional, non-mutating preflight and Caddy render/exclusive-write helper; manual operator apply/rollback guide. No proxy install, certificates, DNS or firewall changes. |
| 3. Notifications | Persistent account-scoped inbox plus encrypted opt-in Telegram/Discord/SMTP channels, transactional outbox, leases and bounded retries. Disabled by default; ambiguous delivery can duplicate. |
| 4. Source health | Separate test/sync/last-success facts, safe error categories/retry hints, bounded history and failure/recovery notices. No automatic media job. |
| 5. Library | Collections, smart views, stable sort/filter, recent/saved searches and local override precedence. |
| 6. Series | Durable/manual watched state, observed-playback threshold, S00 specials, season progress, cached-playable-only next episode and off-by-default autoplay. Source-bound preview/confirm for selected season saves. |
| 7. Storage | Measured artifact/inode/shared-byte accounting, owner-bound stale-safe cleanup previews, explicit opt-in title/source expiry policies and active-transfer guards. |
| 8. Integrity | Explicit local SHA256 baseline/match versus real available provider checksum, stored safe probe facts and observed job stages. |
| 9. Compatibility | Best-effort browser declarations, bounded terminal-error recovery and original-device-download guidance. No video conversion. |
| 10. Administration | Pending/confirmed TOTP, replay protection, one-use recovery, session revoke, optional network/proxy gate with root-only recovery, independent encrypted-backup verification. |
| 11. Metadata | Explicit remote preview/save, local corrections, bounded canonical private PNG artwork, local related titles and known episode inventory. |
| 12. Search | Local title/metadata/episode/English-caption matching, parameterized filters, account-scoped recent/saved searches and separate explicit Discover search. |
| 13. Mobile/PWA | Install manifest/local icons, safe public hashed-asset precache policy, responsive/safe-area styles and native original-file device downloads. No private/offline-media caching or navigation fallback. |
| 14. Monitoring | Bounded real health samples/transitions, storage/runtime observations, backup records, stage timing and allowlisted support export. Unknown host/container facts stay unknown; no Docker socket mount. |

The original app retains mixed movie/series cards, metadata-first browsing, shared explicit Play/Download admission, resumable authenticated original downloads, unchanged-video HLS stream copy, English/Hindi/original audio policy, English captions, live SSE/polling and private monthly setup bundles.

No profiles, pins, hardware acceleration, video-quality selector/ladder, or whole-video re-encoding are included. Missing languages, provider results, physical reclaim, and browser codec support cannot be fabricated.

## Verification gates

| Gate | Status |
|---|---|
| Current Python/backend/tooling tests | 251 tests run; OK with 8 platform-specific skips |
| Current synthetic real-HTTP feature smoke | Passed; no live integrations |
| Python compilation, management shell syntax and diff whitespace | Passed |
| Current TypeScript/Vite build and frontend tests | **Blocked: approved Node runtime unavailable** |
| Browser/mobile and service-worker lifecycle checks of the changed UI | **Pending a fresh frontend build** |
| Real Ubuntu/Docker install/update/restore | Excluded by request; not run |
| Live providers, torrents, notification delivery, caption models | Not run; mocks/synthetic data only |
| Public HTTPS/certificates/DNS/firewall | Not applied or verified |
| Broad original-codec/device compatibility | Not certified |

See [release verification](release-review.md) for exact evidence and historical-preview boundaries. Do not call the expansion release-verified until the frontend gates pass.

## Operator documentation

- [Using library, series, notifications, storage and PWA features](user-guide.md)
- [MFA, sessions, optional network trust and local recovery](security.md)
- [Maintenance and monthly server replacement](operations.md)
- [Encrypted offline backups](encrypted-backups.md)
- [Optional HTTPS preflight and manual apply](https.md)

## Monthly replacement

A private setup bundle carries supported connection credentials/settings but excludes media, accounts, sessions, MFA/recovery state, personal history and host-specific network trust. A fresh server generates new administrator credentials. Treat the bundle as a secret. A full private backup is a different, credential-bearing recovery artifact; neither export nor verification automatically restores it.

## Not committed for this delivery

Bookmarks, picture-in-picture, casting and offline queues remain ideas, not delivery promises. No additional implementation is implied.

A public bootstrap must reference one immutable published revision in both the raw installer URL and `--version`. Do not turn a local working-tree verification into a production/deployment claim or change remote `main` without explicit approval.
