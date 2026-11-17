import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DeviceDownload } from './DeviceDownload';
import { Discover } from './Discover';
import { Player } from './Player';
import type { Video } from './types';

const video: Video = { id: 'fixture', title: 'Original movie', source_id: 'fixture', source_kind: 'http', remote_id: 'fixture', state: 'cached', mime_type: 'video/mp4', size_bytes: 200, bytes_downloaded: 200, duration: 20, width: 1920, height: 1080, thumbnail_url: null, poster_url: null, preview_url: null, sprites_vtt_url: null, error: null, audio_tracks: [], subtitle_tracks: [], playback: { mode: null, url: null, available_until: null, duration: 20 }, last_played: null, updated_at: '2026-11-16T00:00:00Z' };

describe('original-file downloads', () => {
  it('does not offer partial or cloud files as a completed device download', () => {
    expect(DeviceDownload({ video })).toBeNull();
  });
  it('uses a direct browser attachment link, not a full-file JavaScript blob', () => {
    const html = renderToStaticMarkup(createElement(DeviceDownload, { video: { ...video, download_url: '/api/media/fixture/download' } }));
    expect(html).toContain('href="/api/media/fixture/download"');
    expect(html).toContain('download="Original movie.mp4"');
    expect(html).toContain('Download to device');
  });
  it('keeps audio/caption controls but never renders a video-quality selector', () => {
    const html = renderToStaticMarkup(createElement(Player, { video, onClose: vi.fn() }));
    expect(html).not.toContain('Video quality');
    expect(html).not.toContain('1080p');
    expect(html).toContain('Add captions');
  });
});

describe('separate discovery workspace', () => {
  it('starts with an explicit catalog action, not automatic provider or media requests', () => {
    const html = renderToStaticMarkup(createElement(Discover, { onPlayer: vi.fn() }));
    expect(html).toContain('Discover');
    expect(html).toContain('Browse movies');
    expect(html).toContain('Search movies by title');
    expect(html).toContain('Metadata only until you choose Play or Download');
    expect(html).not.toContain('Synthetic');
  });
});
