# Delivery and verification roadmap

## Implemented interface

Drive library and a separate movie Discover workspace; mixed movie/series cards; series seasons/episodes on opening; dark-gradient system-font UI; delayed selective tooltips; metadata-first browsing; shared Play/Download jobs; resumable original-file downloads to device; unchanged-video HLS stream copy; English/Hindi/explicit-original audio policy; English caption tools; continue watching; local server/activity/alerts; live SSE invalidation and polling fallback; cache cleanup, schedules, bandwidth and portable private setup bundles.

No video-quality selector, quality badges, quality ladder, profiles, pins or hardware acceleration are planned in this delivery. Missing source languages and browser codec support cannot be fabricated.

## Remaining verification gates

| Item | Status |
|---|---|
| Local Python/API/frontend suites, synthetic FFmpeg and desktop/375px browser checks | Passing in the current Windows environment; see release-review.md |
| Fresh/repeat Ubuntu 24.04 Docker install | Not run; requires a disposable authorized host |
| Actual update success/failure recovery and restore rehearsal | Not run in Docker; local helper tests are not deployment proof |
| Live public/private Google Drive and provider credentials | Not run against user media |
| Real authorized torrents and configured Torrentio/Cinemeta services | Not run; fixture and API tests use bounded mocks |
| HEVC/AV1/VP9 on representative browsers | Pass-through packaging implemented; broad runtime/browser verification pending |
| Optional local caption model and external downloader/transcription dependencies | Installation/execution requires approval and authorized test fixtures |
| CI that installs or executes optional dependencies | Not enabled |

## Monthly replacement

The private setup bundle intentionally carries connection credentials and settings, including private add-on URLs. It excludes cached videos, users, sessions and history. A fresh server creates new admin credentials. Treat the bundle as a secret; no public credential-distribution workflow is provided.

## Ideas not included

Bookmarks, picture-in-picture, casting and offline queues are not delivery commitments. No additional implementation was started for these ideas.

## Public bootstrap

A usable one-line install command must reference one immutable, published source revision in both the raw script URL and the installer's `--version` argument. Do not run a `PIN_PENDING` placeholder or describe local/mock tests as production deployment certification.
