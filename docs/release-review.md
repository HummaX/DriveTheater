# Release verification status

DriveTheater is independent of DriveConnector. This is local preview evidence, not production deployment certification.

## Current feature expansion — 2026-11-16

The requested recommendations **2–14** have implementation, API integration, feature components and operator helpers in this preview source. Recommendation **1**, a real Ubuntu deployment/update/restore rehearsal, was deliberately excluded. Publishing this source does not imply a frontend build, successful server deployment, or completed recovery rehearsal.

| Check | Latest observed result |
|---|---|
| Backend/API and feature services | **100 tests run; OK, 3 skipped** |
| Core, media, migrations and operator tooling | **151 tests run; OK, 5 skipped** |
| Synthetic application over a real local HTTP socket | **Passed** |
| Python compilation | **Passed** |
| Management shell syntax | **Passed** |
| Git diff whitespace check | **Passed** |
| TypeScript/Vite build of this expansion | **Not run: no approved available Node runtime** |
| Frontend tests of this expansion | **Not run for the same reason** |
| Browser/mobile checks of the newly integrated feature UI | **Not run against a fresh build** |

The two Python suites total **251 tests**, including **8 platform-specific skips**. Skips cover Windows symlink privilege limitations, real Linux/POSIX permission checks, and a platform-specific unsupported-runtime case. A skipped path is not a passing verification of that platform.

The previous published preview passed 22 frontend tests, a TypeScript/Vite build, and desktop/375px browser checks. That is **historical evidence only**, not verification of the changed React components or PWA build plugin. Static source/API-contract inspection is also not a substitute for compilation or browser behavior.

### Representative verified safeguards

- Additive/repeated/concurrent SQLite initialization preserves existing passwords, sessions and data. Extended tables use named INSERT columns.
- TOTP RFC vectors, clock window/replay prevention, pending enrollment, encrypted secrets, one-use recovery, factor rate limiting and immediate session revocation are tested. Password-only MFA login creates no session; password changes invalidate pending challenges.
- Unified authentication protects old/new API routes. Untrusted forwarded/identity headers do not grant access. Network policy preview rejects current-request lockout; local root-only recovery preserves passwords/MFA/media.
- Notifications/outbox are durable and transactional. Disabled channels do not send; mocked delivery covers leases, deduplication, bounded retry/rate limits, fixed HTTP endpoints, SMTP public-peer/TLS checks and safe error handling. Real delivery was not used.
- Source health distinguishes connection tests from sync success and records deduplicated failure/recovery transitions without provider secrets or automatic media jobs.
- Collections/saved searches are account-scoped. Local metadata overrides take precedence. S00 specials, local caption indexing, watched thresholds, manual precedence, replay-resistant playback observation and cached-only next-episode behavior are tested.
- Season previews preserve exact selection order, count known/unknown sizes, and omit active/already-playable items. Confirmations bind the owner and a server-side source fingerprint. Changed source identity/URL/size rejects admission atomically; a job started after preview is excluded rather than counted as a new download. Successful confirmations are single-use.
- Safe cache reservations recheck jobs, transfer/playback leases, paths, inode identity and shared references. Cleanup plans are owner-bound, expiring and one-use. Synthetic tests cover hardlink accounting, stale previews, partial unlink failures and fail-closed unsupported operations. Windows skips do not certify Linux deletion semantics.
- Integrity distinguishes a local computed baseline/local match from an actual provider-checksum match. Caller-supplied checksums cannot manufacture provider verification.
- Probe/stage/support diagnostics use allowlisted data. Worker failure/cancellation categories are recorded without raw provider/FFmpeg errors, signed URLs or paths. Host/container totals remain unknown rather than inferred.
- Scheduled health observations notify only on enter/recover transitions, deduplicate backup creation notices, prune expired tokens separately, preserve credentials/MFA and record real bounded samples.
- Explicit metadata lookup and canonical PNG poster validation use mocks/local data only. Private custom artwork is included in a real synthetic standalone SQLite backup that encrypts and verifies offline.
- Backup tests cover independent/wrong passwords, authenticated tampering, bounded tar/gzip/SQLite validation, exclusive destinations and no restoration/live database mutation. HTTPS tests exercise validation/render/preflight with stubbed observations, not certificates or host changes.
- PWA policy tests check an inert default precache and refusal of private/API/media/navigation caching. The changed Vite precache-generation plugin and actual service-worker lifecycle still need a fresh frontend build/browser run.
- Existing synthetic FFmpeg tests preserve decoded video frames through HLS stream-copy packaging, original resolution, a single video rendition and no fallback video encoder/quality ladder. Authenticated original downloads retain HEAD/Range/If-Range and active-cache protection.

### Real HTTP smoke

`tests/run_feature_smoke.py` starts an isolated synthetic app and exercises it through HTTP, not just internal function calls. It passed authenticated metadata/S00/collections without media jobs, the durable inbox/support route, a ranged original download, stale cleanup-token replay refusal, and MFA/recovery/session revocation. Its report explicitly records `ui_build_verified: false` and `live_integrations_used: false`.

## Boundaries not tested or performed

1. No user-server deployment, Ubuntu/Docker install/start/update/failure rollback or disaster-restore rehearsal. The Docker permission repair has local tests but no real Linux build/run certification here.
2. No live Drive, MEGA/GoFile/extractor, Cinemeta/Torrentio, permitted torrent, or real provider credential use.
3. No Telegram/Discord/email test messages or live delivery. Retries can duplicate messages after ambiguous remote acceptance; exactly-once delivery is not promised.
4. No optional downloader/transcription package installation or caption-model execution.
5. No DNS/firewall changes, Caddy installation/reload, ACME/certificate issuance or public HTTPS reachability test.
6. No broad HEVC/AV1/VP9 browser/device compatibility or hardware performance certification. Original-codec support remains a client requirement; no video re-encoding was added.
7. No PWA offline-media promise. Only allowlisted public build assets may be cached; a fresh offline navigation is not guaranteed. Device downloads are deliberate separate original files.
8. No comprehensive independent security audit or exhaustive secrets scan is implied by the regression suite. Private backups/setup bundles/configuration must never be posted publicly.

See [the user guide](user-guide.md), [security](security.md), [maintenance](operations.md), [encrypted backups](encrypted-backups.md), and [optional HTTPS](https.md) for operator-facing behavior and limitations.
