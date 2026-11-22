import { useCallback, useEffect, useState } from 'react';
import { ActionButton } from './ActionButton';
import { request } from './api';

export type MetadataCandidate = { media_type: 'movie' | 'series'; media_id: string; requested_language?: string };
type Metadata = {
  title?: string; year?: number; genre?: string[]; cast?: string[]; director?: string[]; runtime?: number;
  rating?: number; poster_url?: string; description?: string; trailer_links?: string[]; imdb_id?: string;
  media_id?: string; type?: 'movie' | 'series'; requested_language?: string;
};
type Related = { video_id: string; title?: string; year?: number; poster_url?: string; shared: string[] };
type Episode = { season: number; episode: number; title: string; released?: string; thumbnail?: string; status: 'available' | 'missing' | 'future' | 'unknown'; video_id?: string };


/**
 * Explicit metadata controls. GET refreshes only local data; the configured provider
 * is contacted exclusively by Preview and Confirm save POSTs.
 */
export function MetadataTools({ videoId, initialType = 'movie', onChanged, onRelated }: {
  videoId: string; initialType?: 'movie' | 'series'; onChanged?: () => void; onRelated?: (videoId: string) => void;
}) {
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [related, setRelated] = useState<Related[]>([]);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [mediaId, setMediaId] = useState('');
  const [mediaType, setMediaType] = useState<'movie' | 'series'>(initialType);
  const [candidate, setCandidate] = useState<Metadata | null>(null);
  const [poster, setPoster] = useState<File | null>(null);
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const detail = await request<{ metadata: Metadata | null }>(`/api/metadata/videos/${encodeURIComponent(videoId)}`);
      setMetadata(detail.metadata);
      const relatedResult = await request<{ items: Related[] }>(`/api/metadata/videos/${encodeURIComponent(videoId)}/related`);
      setRelated(relatedResult.items);
      if (detail.metadata?.type === 'series') {
        const inventory = await request<{ episodes: Episode[] }>(`/api/metadata/videos/${encodeURIComponent(videoId)}/episodes`);
        setEpisodes(inventory.episodes);
      } else setEpisodes([]);
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Unable to load local metadata'); }
  }, [videoId]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => { setCandidate(null); setMediaId(''); setPoster(null); }, [videoId]);

  const lookup = async () => {
    setLoading(true); setMessage('');
    try {
      const body: MetadataCandidate = { media_id: mediaId.trim(), media_type: mediaType };
      const preview = await request<{ candidate: Metadata }>('/api/metadata/videos/' + encodeURIComponent(videoId) + '/preview', { method: 'POST', body: JSON.stringify(body) });
      setCandidate(preview.candidate);
      setMessage('Review the candidate, then confirm to save it locally.');
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Metadata preview failed'); }
    finally { setLoading(false); }
  };

  const confirm = async () => {
    if (!candidate) return;
    setLoading(true); setMessage('');
    try {
      await request('/api/metadata/videos/' + encodeURIComponent(videoId) + '/fetch', { method: 'POST', body: JSON.stringify({ media_id: candidate.media_id, media_type: candidate.type, language: candidate.requested_language }) });
      setCandidate(null); setMessage('Metadata saved locally.'); await refresh(); onChanged?.();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Unable to save metadata'); }
    finally { setLoading(false); }
  };

  const uploadPoster = async () => {
    if (!poster) return;
    setLoading(true); setMessage('');
    try {
      const form = new FormData(); form.append('poster', poster);
      await request('/api/metadata/videos/' + encodeURIComponent(videoId) + '/poster', { method: 'POST', body: form });
      setPoster(null); setMessage('Custom poster saved locally.'); await refresh(); onChanged?.();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Poster upload failed'); }
    finally { setLoading(false); }
  };

  const shown = candidate || metadata;
  return <section className="metadata-tools" aria-label="Metadata tools">
    <div><label>IMDb ID <input aria-label="IMDb ID" value={mediaId} maxLength={14} placeholder="tt1234567" onChange={event => setMediaId(event.target.value)} /></label>
      <label>Kind <select aria-label="Metadata kind" value={mediaType} onChange={event => setMediaType(event.target.value as 'movie' | 'series')}><option value="movie">Movie</option><option value="series">Series</option></select></label>
      <ActionButton className="button button-small" type="button" disabled={loading || !/^tt\d{4,12}$/.test(mediaId.trim())} onClick={() => void lookup()}>Preview metadata</ActionButton>
      <ActionButton className="button button-small" type="button" disabled={loading} onClick={() => void refresh()}>Refresh local details</ActionButton>
    </div>
    {candidate && <div role="status"><strong>Candidate: {candidate.title}</strong> {candidate.year ? `(${candidate.year})` : ''}
      <ActionButton className="button button-primary button-small" type="button" disabled={loading} onClick={() => void confirm()}>Confirm and save</ActionButton>
      <ActionButton className="button button-small" type="button" disabled={loading} onClick={() => { setCandidate(null); setMessage('Candidate discarded.'); }}>Discard candidate</ActionButton>
    </div>}
    <div><label>Custom poster <input aria-label="Custom poster" type="file" accept="image/png" onChange={event => setPoster(event.target.files?.[0] || null)} /></label>
      <ActionButton className="button button-small" type="button" disabled={loading || !poster} onClick={() => void uploadPoster()}>Upload poster</ActionButton></div>
    {shown && <article aria-label="Metadata details">
      {shown.poster_url && <img src={shown.poster_url} alt="" />}
      <h3>{shown.title}{shown.year ? ` (${shown.year})` : ''}</h3>
      {shown.description && <p>{shown.description}</p>}
      <p>{shown.genre?.join(', ')}{shown.director?.length ? ` · Directed by ${shown.director.join(', ')}` : ''}{shown.runtime ? ` · ${shown.runtime} min` : ''}{shown.rating !== undefined ? ` · Rating ${shown.rating}/10` : ''}</p>
      {shown.cast?.length ? <p>Cast: {shown.cast.join(', ')}</p> : null}
      {shown.imdb_id && <p>IMDb: {shown.imdb_id}</p>}
      {shown.trailer_links?.length ? <p>Trailers: {shown.trailer_links.filter((link) => /^https?:\/\//i.test(link.trim())).map((link, index) => <a key={link} href={link.trim()} target="_blank" rel="noreferrer noopener">Trailer {index + 1}</a>)}</p> : null}
    </article>}
    {episodes.length > 0 && <section aria-label="Known episodes"><h3>Known episodes</h3><ul>{episodes.map(episode => <li key={`${episode.season}-${episode.episode}`}>S{episode.season.toString().padStart(2, '0')}E{episode.episode.toString().padStart(2, '0')} {episode.title} — {episode.status}{episode.released ? ` (${episode.released})` : ''}</li>)}</ul></section>}
    {related.length > 0 && <section aria-label="Related local titles"><h3>Related local titles</h3><ul>{related.map(item => <li key={item.video_id}><ActionButton className="button button-small" type="button" onClick={() => onRelated?.(item.video_id)}>{item.title || item.video_id}</ActionButton> {item.year || ''} {item.shared.length ? `(${item.shared.join(', ')})` : ''}</li>)}</ul></section>}
    {message && <p role="status">{message}</p>}
  </section>;
}
