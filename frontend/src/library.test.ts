import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ActionButton, TOOLTIP_DELAY_MS } from './ActionButton';
import { GroupedLibrary, libraryEntries, SeriesCard } from './App';
import type { Video } from './types';

function video(id: string, title: string): Video {
  return { id, title, source_id: 'fixture', source_kind: 'http', remote_id: id, state: 'cloud', mime_type: null, size_bytes: null, bytes_downloaded: 0, duration: null, width: null, height: null, thumbnail_url: null, poster_url: null, preview_url: null, sprites_vtt_url: null, error: null, audio_tracks: [], subtitle_tracks: [], playback: { mode: null, url: null, available_until: null, duration: null }, last_played: null, updated_at: '2026-11-16T00:00:00Z' };
}
const titles = [video('movie-1', 'A standalone movie'), video('ep-1', 'Test Series S01E01 First light'), video('movie-2', 'Another standalone movie'), video('ep-2', 'Test Series S02E01 Second light')];

describe('mixed movie and series library', () => {
  it('groups each series into one entry while preserving mixed catalog order', () => {
    const entries = libraryEntries(titles);
    expect(entries.map((entry) => entry.kind)).toEqual(['video', 'series', 'video']);
    const series = entries[1];
    expect(series.kind).toBe('series');
    if (series.kind === 'series') {
      expect(series.title).toBe('Test Series');
      expect(series.episodes).toHaveLength(2);
      expect(series.episodes.map((episode) => episode.season)).toEqual([1, 2]);
    }
  });

  it('shows one tagged series card alongside movies, not expanded seasons or episodes', () => {
    const onAction = vi.fn();
    const html = renderToStaticMarkup(createElement(GroupedLibrary, { items: titles, onAction }));
    expect(html).toContain('aria-label="Movies and series"');
    expect(html.match(/data-action="open-series"/g)).toHaveLength(1);
    expect(html).toContain('class="series-pill">Series');
    expect(html).toContain('2 seasons · 2 episodes');
    expect(html).not.toContain('<details');
    expect(html).not.toContain('S01E01 First light');
    expect(html).toContain('A standalone movie');
    expect(html).toContain('Another standalone movie');
    expect(onAction).not.toHaveBeenCalled();
  });

  it('opens a series as navigation only, without calling a media action', () => {
    const entry = libraryEntries(titles).find((item) => item.kind === 'series');
    if (!entry || entry.kind !== 'series') throw new Error('Synthetic series was not grouped');
    const onOpen = vi.fn();
    const card = SeriesCard({ title: entry.title, episodes: entry.episodes, onOpen });
    const button = card.props.children;
    expect(button.props.hint).toContain('No media download starts');
    button.props.onClick();
    expect(onOpen).toHaveBeenCalledOnce();
  });
});

describe('selective delayed tooltips', () => {
  it('waits half a second instead of opening instantly', () => {
    expect(TOOLTIP_DELAY_MS).toBe(500);
  });

  it('keeps accessible metadata without showing tooltips on ordinary labeled buttons', () => {
    const html = renderToStaticMarkup(createElement(ActionButton, {}, 'Library'));
    expect(html).toContain('aria-description=');
    expect(html).not.toContain('data-tooltip=');
  });

  it('enables help for icon-only controls and explicitly selected complex actions', () => {
    const icon = renderToStaticMarkup(createElement(ActionButton, { 'aria-label': 'Fullscreen' }, createElement('svg')));
    const cleanup = renderToStaticMarkup(createElement(ActionButton, { tooltip: true }, 'Clean eligible cache'));
    expect(icon).toContain('data-tooltip="true"');
    expect(cleanup).toContain('data-tooltip="true"');
  });
});
