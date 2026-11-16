import { createElement } from 'react';
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ConfirmedSeasonDownload, LibraryControls, LibraryInspector } from './LibraryTools';

describe('library feature controls', () => {
  it('renders initial local controls without assuming an asynchronous preference response', () => {
    const markup = renderToStaticMarkup(createElement(LibraryControls, { onFilter: () => undefined }));
    expect(markup).toContain('Create collection');
    expect(markup).toContain('Save filters');
    expect(markup).toContain('Search library');
    expect(markup).toContain('Continue watching');
    expect(markup).toContain('Metadata filters');
  });

  it('renders editable local metadata and explicit watch control', () => {
    const markup = renderToStaticMarkup(createElement(LibraryInspector, { item: {
      id: 'video-1', title: 'Episode one', watched: false, type: 'series',
      series: 'Example show', season: 0, episode: 1,
      metadata: { provider: 'local' }, overrides: { title: 'Episode one' },
    } }));
    expect(markup).toContain('Save local details');
    expect(markup).toContain('Mark watched');
    expect(markup).toContain('Metadata provenance');
    expect(markup).toContain('Example show');
  });

  it('requires an explicit non-empty selection before preparing a download plan', () => {
    const empty = renderToStaticMarkup(createElement(ConfirmedSeasonDownload, { videoIds: [] }));
    const selected = renderToStaticMarkup(createElement(ConfirmedSeasonDownload, { videoIds: ['video-1'] }));
    expect(empty).toContain('disabled');
    expect(selected).not.toContain('disabled');
    expect(selected).toContain('Prepare season download');
  });
});
