import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ActionButton } from './ActionButton';
import { LibraryCardView } from './App';
import { buttonMetadata, mediaActionHint } from './buttonMetadata';
import { Login } from './Login';
import type { Video } from './types';

const video: Video = {
  id: 'synthetic-title', title: 'Synthetic movie', source_id: 'synthetic-source', source_kind: 'http', remote_id: 'fixture',
  state: 'cloud', mime_type: 'video/mp4', size_bytes: null, bytes_downloaded: 0, duration: 600,
  width: null, height: null, thumbnail_url: null, poster_url: null, preview_url: null, sprites_vtt_url: null,
  error: null, audio_tracks: [], subtitle_tracks: [], playback: { mode: null, url: null, available_until: null, duration: 600 },
  last_played: null, updated_at: '2026-11-16T00:00:00Z'
};

describe('button metadata', () => {
  it('describes both the action and its download boundary', () => {
    expect(mediaActionHint('play', video)).toContain('bypasses the download schedule');
    expect(mediaActionHint('download', video)).toContain('without opening the player');
    expect(mediaActionHint('preview', video)).toContain('No source download starts');
    expect(mediaActionHint('delete', video)).toContain('source file and library entry are kept');
  });

  it('uses actual cache, job, and resume state rather than generic promises', () => {
    expect(mediaActionHint('download', { ...video, state: 'cached' })).toContain('already cached');
    expect(mediaActionHint('download', { ...video, state: 'downloading' })).toContain('do not start a duplicate');
    expect(mediaActionHint('play', { ...video, watch_position: 65 })).toContain('Resume from 1:05');
    expect(mediaActionHint('play', { ...video, playback: { ...video.playback, url: '/api/media/synthetic' } })).toContain('using available cached media');
  });

  it('provides explanations for navigation, source, server, and player controls', () => {
    for (const label of ['Library', 'Sources', 'Downloads', 'Server', 'Settings', 'Sign out', 'Sign in', 'Add URL', 'Connect source', 'Load episodes', 'Find streams', 'Test', 'Sync', 'Reconnect', 'Cancel', 'Retry', 'Clear history', 'Clean eligible cache', 'Save settings', 'Change password', 'Close player', 'Play', 'Pause', 'Enable TV mode', 'Exit TV mode', 'Fullscreen', '−0.5s', '+0.5s', 'Reset']) {
      const metadata = buttonMetadata(label);
      expect(metadata.action).not.toBe('');
      expect(metadata.hint).not.toBe(label);
    }
  });

  it('preserves button semantics and exposes safe local metadata without wrappers', () => {
    const onClick = vi.fn();
    const html = renderToStaticMarkup(createElement(ActionButton, { hint: 'Explain the action.', action: 'test-action', context: 'synthetic', type: 'submit', disabled: true, 'aria-label': 'Run test', onClick }, 'Run'));
    expect(html).toMatch(/^<button /);
    expect(html).toContain('type="submit"');
    expect(html).toContain('disabled=""');
    expect(html).toContain('aria-label="Run test"');
    expect(html).toContain('aria-description="Explain the action."');
    expect(html).toContain('data-action="test-action"');
    expect(html).toContain('data-context="synthetic"');
    expect(onClick).not.toHaveBeenCalled();
  });

  it('gives each library action title-specific metadata without invoking an action', () => {
    const onAction = vi.fn();
    const html = renderToStaticMarkup(createElement(LibraryCardView, { video: { ...video, state: 'cached', preview_url: '/api/media/preview.mp4' }, previewing: false, onPreviewing: vi.fn(), onAction }));
    const buttons = html.match(/<button\b[^>]*>/g) ?? [];
    expect(buttons).toHaveLength(4);
    for (const button of buttons) {
      expect(button).toContain('data-action=');
      expect(button).toContain('data-context="synthetic-title"');
      expect(button).toContain('aria-description=');
      expect(button).toContain('Synthetic movie');
    }
    expect(onAction).not.toHaveBeenCalled();
  });

  it('provides sign-in help without including password inputs in metadata', () => {
    const html = renderToStaticMarkup(createElement(Login, { onLogin: vi.fn() }));
    expect(html).toContain('data-action="sign-in"');
    expect(html).toContain('aria-description="Sign in to this installation.');
  });
});
