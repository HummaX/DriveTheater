import { describe, expect, it, vi } from 'vitest';
import { LibraryCardView, resumeProgress, seriesEpisode } from './App';
import type { Video } from './types';
import { canDeleteCache, parseSpriteVtt, startsMediaWork, stateLabel } from './utils';

const cachedVideo: Video = {
  id: 'preview-1', title: 'Cached preview', source_id: 'source-1', source_kind: 'http', remote_id: 'remote-1',
  state: 'cached', mime_type: 'video/mp4', size_bytes: 1, bytes_downloaded: 1, duration: 60,
  width: 1920, height: 1080, thumbnail_url: 'https://assets.example/thumbnail.jpg', poster_url: 'https://assets.example/poster.jpg', preview_url: 'https://assets.example/preview.mp4',
  sprites_vtt_url: null, error: null, audio_tracks: [], subtitle_tracks: [],
  playback: { mode: 'direct', url: 'https://assets.example/video.mp4', available_until: null, duration: 60 }, last_played: null, updated_at: '2026-11-16T00:00:00Z'
};

describe('sprite VTT parsing', () => {
  it('extracts timing and image crop coordinates', () => {
    const cues = parseSpriteVtt('WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nthumbs.jpg#xywh=160,90,160,90');
    expect(cues).toEqual([{ start: 0, end: 5, image: 'thumbs.jpg', x: 160, y: 90, width: 160, height: 90 }]);
  });
});

describe('library labels and actions', () => {
  it('does not imply a cloud record is downloaded', () => {
    expect(stateLabel('cloud')).toBe('In cloud');
    expect(canDeleteCache('cloud')).toBe(false);
    expect(canDeleteCache('cached')).toBe(true);
  });
  it('never treats a card hover as a download trigger', () => {
    expect(startsMediaWork('hover')).toBe(false);
    expect(startsMediaWork('download')).toBe(true);
  });
  it('shows a cached-card preview on hover without invoking a media action', () => {
    const onPreviewing = vi.fn();
    const onAction = vi.fn();
    const card = LibraryCardView({ video: cachedVideo, previewing: false, onPreviewing, onAction });
    const articleProps = card.props as { onPointerEnter: () => void; onPointerLeave: () => void };

    articleProps.onPointerEnter();
    articleProps.onPointerLeave();

    expect(onPreviewing).toHaveBeenNthCalledWith(1, true);
    expect(onPreviewing).toHaveBeenLastCalledWith(false);
    expect(onAction).not.toHaveBeenCalled();
  });
});

describe('library grouping and watching progress', () => {
  it('groups episodes by the cleaned title prefix rather than episode names', () => {
    const pilot = seriesEpisode({ ...cachedVideo, title: 'Example Show S01E01 Pilot' });
    const sequel = seriesEpisode({ ...cachedVideo, id: 'preview-2', title: 'Example Show S01E02 The Return' });

    expect(pilot?.series).toBe('Example Show');
    expect(sequel?.series).toBe('Example Show');
    expect(sequel?.episode).toBe(2);
  });

  it('reports only real, clamped watch progress', () => {
    expect(resumeProgress({ ...cachedVideo, watch_position: 15, duration: 60 })).toBe(25);
    expect(resumeProgress({ ...cachedVideo, watch_position: 80, duration: 60 })).toBe(100);
    expect(resumeProgress({ ...cachedVideo, watch_position: 15, duration: null })).toBeNull();
  });
});
