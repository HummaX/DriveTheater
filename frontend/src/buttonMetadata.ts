import type { Video } from './types';
import { formatTime } from './utils';

/** Only public catalog fields belong in button hints, never private source URLs. */
export function mediaActionHint(action: 'play' | 'download' | 'preview' | 'delete', video: Video): string {
  const title = `“${video.title}”`;
  if (action === 'preview') return `Show a muted preview of ${title} from cached media. No source download starts.`;
  if (action === 'delete') return `Remove the cached copy of ${title} from this server. The source file and library entry are kept.`;
  if (action === 'download') {
    if (video.state === 'cached') return `${title} is already cached. Reuse the existing copy; no new download is needed.`;
    if (['queued', 'downloading', 'preparing', 'playable'].includes(video.state)) return `Reuse the current preparation job for ${title}; do not start a duplicate download.`;
    return `Queue ${title} for this server's cache without opening the player. The download schedule and bandwidth limit apply.`;
  }
  const resume = typeof video.watch_position === 'number' && Number.isFinite(video.watch_position) && video.watch_position > 0
    ? ` Resume from ${formatTime(video.watch_position)} when playback is ready.` : '';
  if (video.playback.url) return `Open ${title} in the player using available cached media.${resume}`;
  return `Prepare ${title} and open the player. Download only if needed; an explicit Play request bypasses the download schedule.${resume}`;
}

const descriptions: Record<string, string> = {
  Library: 'Browse catalog metadata and cached titles. Browsing does not download source media.',
  Sources: 'Connect Drive folders or look up compatible streams. Downloads require a separate explicit request.',
  Downloads: 'View live download and processing progress, cancel work, or retry a failed job.',
  Server: 'View live server health, downloader availability, alerts, and local activity.',
  Settings: 'Manage cache, download scheduling, bandwidth, activity retention, and your password.',
  'Sign out': 'End your session on this browser. Server downloads continue independently.',
  'Sign in': 'Sign in to this installation. Local sign-in activity is recorded on this server.',
  'Signing in…': 'Signing in to this installation. Wait for the server response.',
  'Add URL': 'Register a media link, magnet, or torrent source. Downloading is optional and must be explicitly selected.',
  'Add a URL': 'Register a media link, magnet, or torrent source. Downloading is optional and must be explicitly selected.',
  'Add to library': 'Register the selected source as metadata only. No media download starts.',
  'Adding…': 'Registering the selected source. Wait for the server response.',
  'Connect source': 'Store this Drive connection privately on your server. Connecting does not download videos.',
  'Connecting…': 'Saving the Drive connection. Wait for the server response.',
  'Load episodes': 'Send the series IMDb ID to the selected metadata add-on and list its episodes. No media download starts.',
  'Loading episodes…': 'Looking up episode metadata only. No media download starts.',
  'Find streams': 'Request stream listings from the add-on you supplied. Results are metadata only; availability is not guaranteed.',
  'Finding streams…': 'Looking up stream listings. No media download starts.',
  Test: 'Check this source connection and folder access without downloading source media.',
  'Testing…': 'Checking source access. Wait for the server response.',
  Sync: 'Refresh this folder’s catalog metadata. Cached copies are kept and source videos are not downloaded.',
  'Syncing…': 'Refreshing folder metadata, not downloading videos.',
  Reconnect: 'Open Google authorization to reconnect this source using the configured callback.',
  'Opening…': 'Opening the configured source authorization flow.',
  Cancel: 'Request cancellation of this job. Useful partial data may be kept for an explicit retry.',
  Retry: 'Queue this job again and reuse verified partial data when the source supports safe resume.',
  'Clear history': 'Clear your local activity history on this server. Sources and cached media are kept.',
  'Clearing…': 'Clearing local activity history. Wait for the server response.',
  'Clean eligible cache': 'Remove eligible cached copies according to storage policy. Active playback and pending work are protected.',
  'Save settings': 'Save the displayed cache, scheduling, bandwidth, and retention settings to this server.',
  'Change password': 'Change this account’s password using the current password and the new value entered.',
  'Close player': 'Return to the library view without cancelling server downloads.',
  Play: 'Start or resume this title from its current position.',
  Pause: 'Pause playback without cancelling server processing.',
  'Enable TV mode': 'Enlarge player controls and enable Space to play/pause, Left/Right to seek, and Up/Down to adjust volume.',
  'Exit TV mode': 'Return to standard player controls and disable TV keyboard shortcuts.',
  Fullscreen: 'Open the video fullscreen using your browser. Press Escape to leave fullscreen.',
  '−0.5s': 'Show the selected English captions half a second earlier, down to a −10 second offset.',
  '+0.5s': 'Show the selected English captions half a second later, up to a +10 second offset.',
  Reset: 'Restore the selected captions’ original timing.'
};

export function buttonMetadata(label: string): { action: string; hint: string } {
  const clean = label.trim().replace(/\s+/g, ' ');
  return { action: clean.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''), hint: descriptions[clean] ?? clean };
}
