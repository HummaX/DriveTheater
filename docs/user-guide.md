# DriveTheater user guide

Use only media you are authorized to access. A library record is metadata, not a downloaded file.

## Sign in

A real installation generates new admin credentials. Retrieve the original bootstrap login on your own server:

```bash
sudo bash /opt/drivetheater/scripts/manage.sh login
```

Change the password in Settings. This command does not reset a later password change. The synthetic local preview has separate, disposable demo credentials and no real user data.

## Drive library

1. Connect a folder in **Sources**, then **Sync** it. All indexed videos appear as metadata; sync, search and hover do not download source videos.
2. Movies and series share one grid. A card with a **Series** badge opens its seasons and episodes. Opening/closing the series is navigation only.
3. Choose **Play** on one title/episode to start or reuse its download/preparation job and open the player. Play bypasses a scheduled download window.
4. The download-arrow action **saves to the server** for later viewing. Queued saves wait for the configured download window before starting; an ongoing transfer does not stop when that window closes. The Mbps limit currently applies to the built-in HTTP downloader, not every external provider/torrent tool.
5. **Download to device** appears when the complete original file exists in the server cache. It downloads an attachment to your PC/phone, supports byte-range resume, and does not start a second source download. Browser/download-manager capabilities and network capacity determine speed.
6. **Preview** uses an already generated cached clip. **Remove cached media** keeps the source file and library record.
7. Search, status filters and Continue watching operate on catalog metadata. Filename patterns such as `Show S01E02` and `Show 1x02` drive series grouping; this is not universal episode recognition.

There are no profiles or pins. Icon-only and selected advanced actions have helpful tooltips after a 500 ms hover/keyboard-focus delay. Ordinary navigation and labeled buttons do not display constant tooltips.

## Discover: a separate movie catalog

Open **Discover**, then **Browse movies** or search a title. The configured metadata add-on (Cinemeta by default) receives the query only when you load/search the catalog. IMDb IDs identify movies; this is not an IMDb service.

Set your authorized Torrentio/Stremio-compatible **stream manifest URL** under **Add-on connection**. Its private path may contain tokens. The server stores it privately and returns only connection status. A compatible metadata manifest can also be configured.

Click a movie to look up compatible streams. This does not start a torrent. Choose the actual source using **Download to server** or **Play**. Keeping the source choice explicit prevents accidentally selecting an unwanted file. The server does not seed after completion. Add-on availability is external and live integration is not verified by mock tests.

The Sources page also retains advanced series episode lookup. Adding a lookup result to the library is metadata-only until Play/Download.

## Original video and player controls

The source video is not resized, re-encoded, or converted into quality variants. There is no quality selector or quality badge. Supported video is stream-copied into browser-delivery segments; browser codec support still matters. An incompatible codec produces a clear error rather than a heavy video conversion. You can still download a complete cached original file.

- English, Hindi and explicitly labeled original audio are preferred; missing language tracks cannot be invented. A fallback can be retained when preferred tracks are absent.
- English captions can be extracted from supported text tracks or uploaded as SRT/VTT. Select an available track to search its text and adjust timing in half-second steps.
- Automatic English caption/translation work is optional and happens after playback is made available. The external model/dependency path remains unverified; output may be inaccurate.
- The loaded-range strip reports real buffered media. Seeking previews use cached sprites when available.
- TV mode enlarges controls. Space toggles playback, Left/Right seek ten seconds, and Up/Down change volume outside form controls.
- Watch-position updates feed Continue watching. Audio changes retain playback time and paused state in the tested synthetic HLS case.

AAC audio preparation, short previews, thumbnails and captions still consume CPU. Preserving the original video does not mean zero server work.

## Live state, downloads and server health

The authenticated app keeps a live event connection, with polling fallback. Jobs, catalog state, source sync and server readouts update without a full-page reload. Connection loss is visible; a missing provider or worker never becomes a false success.

Downloads shows the current phase. A known byte/media/item total gets a percentage; an unmeasurable stage remains indeterminate. Cancel stops pending work/subprocesses; Retry may reuse verified partial source bytes. Only one live job per title is admitted.

Server displays runtime CPU/RAM, disk/DB/WAL, network, worker/downloader status, alerts and retained local activity. Read the scope note: container-visible values may not equal the entire host. **Clear history** affects your own local activity/watch records, not cached files or another user's history.

## Settings and replacement servers

Set cache/free-space limits, automatic cleanup, download concurrency, media preparation slots, an optional daily download window, ingest bandwidth and history retention. Active playback and active original-file transfers are protected from eligible cache cleanup. Hardware acceleration is excluded.

A private monthly setup bundle preserves Drive/add-on connections and settings, not media, users, sessions or history. It contains secrets: keep it outside Git and transfer it securely. The next server generates fresh admin credentials. See [operations](operations.md) and [installation/recovery](installation.md).

## Troubleshooting

| Symptom | Check |
|---|---|
| Empty Drive library | Connect and sync a permitted folder in Sources. Private Drive requires legitimate authorization. |
| Playback is preparing | Check the live job phase in Downloads; incompatible/growing containers may require the complete source first. |
| Original codec cannot play | Use a compatible browser/player or download the original. Server video conversion is intentionally disabled. |
| Device-download action missing | The full original source is not yet cached. First use Play or Save to server. |
| No audio/caption language | The source may not contain a supported track. Generated captions are a separate optional process. |
| No discovered streams | Check the configured manifest, authorized provider availability and selected title. Preview fixtures deliberately disable real torrents. |
| Host problem | Preserve data/configuration, read logs, and run the read-only doctor before recovery. |
