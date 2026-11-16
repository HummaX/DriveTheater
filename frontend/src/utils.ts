import type { SpriteCue, VideoState } from './types';

export const formatBytes = (bytes: number | null) => {
  if (bytes === null || !Number.isFinite(bytes)) return 'Size unknown';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  const exp = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length);
  return `${(bytes / 1024 ** exp).toFixed(exp > 1 ? 1 : 0)} ${units[exp - 1]}`;
};
export const formatTime = (seconds: number) => {
  if (!Number.isFinite(seconds)) return '0:00';
  const s = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(s / 3600);
  return hours ? `${hours}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};
export const stateLabel = (state: VideoState) => ({ cloud: 'In cloud', queued: 'Queued', downloading: 'Downloading', preparing: 'Preparing', playable: 'Ready to play', cached: 'Cached', cancelled: 'Cancelled', failed: 'Needs attention' })[state];
export const canDeleteCache = (state: VideoState) => state === 'cached' || state === 'playable';
export const startsMediaWork = (action: 'hover' | 'play' | 'download') => action === 'play' || action === 'download';

const timestamp = (value: string) => {
  const [clock, fraction = ''] = value.trim().split('.');
  const parts = clock.split(':').map(Number);
  const seconds = parts.length === 3 ? parts[0] * 3600 + parts[1] * 60 + parts[2] : parts[0] * 60 + parts[1];
  return seconds + Number(`0.${fraction.replace(/\D/g, '') || '0'}`);
};

export function parseSpriteVtt(vtt: string): SpriteCue[] {
  return vtt.replace(/^﻿?WEBVTT[^\n]*\n?/i, '').split(/\n\s*\n/).flatMap((block) => {
    const lines = block.trim().split(/\r?\n/).filter(Boolean);
    const timing = lines.find((line) => line.includes('-->'));
    const asset = lines.find((line) => !line.includes('-->') && !/^\d+$/.test(line.trim()));
    if (!timing || !asset) return [];
    const [start, end] = timing.split('-->').map(timestamp);
    const [image, fragment] = asset.trim().split('#xywh=');
    const [x, y, width, height] = (fragment || '0,0').split(',').map(Number);
    return Number.isFinite(start) && Number.isFinite(end) ? [{ start, end, image, x, y, width, height }] : [];
  });
}

export const cueAt = (cues: SpriteCue[], time: number) => cues.find((cue) => time >= cue.start && time < cue.end) || null;
