# Release verification status

This is the independent DriveTheater project, not the legacy DriveConnector checkout. It is a preview release, not a production deployment certification.

## Completed local checks

- Core Python suite: **63 tests, 2 skipped** because Windows symlink privileges were unavailable.
- Backend/API suite: **20 passing**.
- Frontend suite: **22 passing**; TypeScript check and Vite production build pass.
- Shell syntax and Python compile checks pass.
- Synthetic FFmpeg tests cover preview/sprite assets, alternate English/Hindi audio, preserved source resolution, one video rendition, no video encoder/filter flags, and identical decoded frame hashes after HLS stream-copy packaging.
- Original-file attachment tests cover auth, HEAD, Range/If-Range, ETag, incomplete-file refusal, no automatic provider queueing and active-cache protection.
- Mocked Discover tests cover saved private add-on status, bounded catalog/search metadata, stream lookup without queueing, and portable add-on settings in the monthly private bundle.
- Browser checks cover the dark mixed movie/series grid, delayed selective tooltips, original-file 206 responses, desktop/375 px phone layout, movie dialog focus/overflow, English/Hindi switching while paused at the same time, and source download-to-playable updates in the same document without reloading.

The synthetic preview disables real add-on/torrent/model execution. Its catalog/posters and login are disposable fixtures, not real user content. No deployment or user-server modification was performed.

## Corrections included

- Growing-file Range responses never advertise future bytes.
- Language filtering excludes arbitrary nonpreferred tracks when preferred tracks exist.
- Invalid Google source creation does not leave private credential files behind.
- Installer refuses to deploy an arbitrary branch against preserved runtime data with a missing checkout.
- Updater builds before replacing the running image and attempts prior-app recovery on failure, never automatic database rollback.
- Setup bundles translate host/container credential paths and preserve connection settings, but never carry users/sessions/media or reuse the old admin password.
- Quality controls/ladder are removed. Main video is stream-copied; unsupported codecs never trigger video re-encoding.
- Runtime data, build artifacts, private setup bundles and local tooling are excluded from public source/build context. A candidate-file scan found no known GitHub/Google token or private-key patterns; this is not an exhaustive secrets/security audit.

## Remaining boundaries

1. Real Ubuntu 24.04 Docker build/start, fresh and repeat installs, monthly restore, update success/failure and restore rehearsal have not been run on this Windows workstation.
2. Live public/private Drive, MEGA/GoFile/extractor sources, permitted torrents and configured Torrentio/Cinemeta services are not verified by mocks.
3. Optional downloader/transcription dependency installation and local caption-model execution require authorization and approved fixtures; they were not run here.
4. Broad HEVC/AV1/VP9 browser playback remains unverified. Original-codec compatibility is a client requirement, not a video-conversion promise.
5. Vite reports a large frontend bundle. No benchmark or hardware-acceleration work was performed.
6. Internet-facing deployments require HTTPS or a trusted VPN and an SSH-preserving firewall. Direct HTTP is not an encrypted production login path.
