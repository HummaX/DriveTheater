import { FormEvent, useEffect, useState } from 'react';
import { ActionButton } from './ActionButton';
import { request } from './api';

type Collection = { id: string; name: string; item_count: number };
type SavedView = { id: string; name: string; filters: Record<string, unknown>; sort: string };
type Search = { id: string; query: string; created_at: number };
type Preferences = { watched_threshold: number; autoplay_next: boolean; metadata_language: string };
export type LibraryItem = { id: string; title: string; watched: boolean; type?: 'movie' | 'series'; year?: number; series?: string; season?: number; episode?: number; metadata?: Record<string, unknown>; overrides?: Record<string, unknown> };


/**
 * LibraryControls props: onChanged fires after a persisted mutation; onFilter
 * receives (filters, sort, query) for the owning library list to apply.
 */
export function LibraryControls({ onChanged, onFilter }: { onChanged?: () => void; onFilter?: (filters: Record<string, unknown>, sort: string, query: string) => void }) {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [views, setViews] = useState<SavedView[]>([]);
  const [searches, setSearches] = useState<Search[]>([]);
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [name, setName] = useState('');
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState('title');
  const [error, setError] = useState('');
  const [activeFilters, setActiveFilters] = useState<Record<string, unknown>>({});
  const [metadataFilters, setMetadataFilters] = useState({ year: '', genre: '', cast: '', director: '' });
  const applyFilter = (filters: Record<string, unknown>, nextSort: string, term: string) => { setActiveFilters(filters); setSort(nextSort); setQuery(term); onFilter?.(filters, nextSort, term); };
  const refresh = async () => {
    try {
      const [c, v, s, p] = await Promise.all([
        request<{ items: Collection[] }>('/api/library/collections'), request<{ items: SavedView[] }>('/api/library/saved-views'),
        request<{ items: Search[] }>('/api/library/recent-searches'), request<Preferences>('/api/library/preferences'),
      ]);
      setCollections(c.items); setViews(v.items); setSearches(s.items); setPreferences(p);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to load library controls'); }
  };
  useEffect(() => { void refresh(); }, []);
  const mutate = async (work: () => Promise<unknown>) => { try { await work(); await refresh(); onChanged?.(); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Library update failed'); } };
  const create = async (event: FormEvent) => { event.preventDefault(); if (name.trim()) await mutate(() => request('/api/library/collections', { method: 'POST', body: JSON.stringify({ name }) })); setName(''); };
  const updatePreferences = (patch: Partial<Preferences>) => void mutate(() => request('/api/library/preferences', { method: 'PUT', body: JSON.stringify(patch) }));
  return <section className="library-tools" aria-label="Library controls">
    <form onSubmit={create}><label>Collection or saved filter name <input value={name} maxLength={120} onChange={event => setName(event.target.value)} /></label><ActionButton className="button button-small">Create collection</ActionButton><ActionButton className="button button-small" type="button" disabled={!name.trim()} onClick={() => void mutate(() => request('/api/library/saved-views', { method: 'POST', body: JSON.stringify({ name, filters: { ...activeFilters, ...(query.trim() ? { search: query.trim() } : {}) }, sort }) }))}>Save filters</ActionButton></form>
    <div className="library-filter"><input aria-label="Search library" value={query} maxLength={160} placeholder="Search titles or local captions" onChange={event => setQuery(event.target.value)} /><select aria-label="Sort library" value={sort} onChange={event => setSort(event.target.value)}><option value="title">Title</option><option value="year">Year</option><option value="duration">Duration</option><option value="recentlyadded">Recently added</option><option value="lastwatched">Last watched</option><option value="type">Type</option><option value="state">Availability</option></select><ActionButton className="button button-small" type="button" onClick={() => applyFilter({}, sort, query)}>Apply</ActionButton></div>
    <fieldset><legend>Metadata filters</legend>{(['year', 'genre', 'cast', 'director'] as const).map(key => <label key={key}>{key === 'cast' ? 'Cast member' : key[0].toUpperCase() + key.slice(1)}<input value={metadataFilters[key]} maxLength={120} onChange={event => setMetadataFilters(current => ({ ...current, [key]: event.target.value }))} /></label>)}<ActionButton className="button button-small" type="button" onClick={() => applyFilter(Object.fromEntries(Object.entries(metadataFilters).filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()])), sort, query)}>Apply metadata filters</ActionButton></fieldset>
    <div aria-label="Smart filters"><ActionButton className="button button-small" type="button" onClick={() => applyFilter({ continuewatching: true }, 'lastwatched', '')}>Continue watching</ActionButton><ActionButton className="button button-small" type="button" onClick={() => applyFilter({ unwatched: true }, 'title', '')}>Unwatched</ActionButton><ActionButton className="button button-small" type="button" onClick={() => applyFilter({ cached: true }, 'title', '')}>Cached</ActionButton><ActionButton className="button button-small" type="button" onClick={() => applyFilter({ failed: true }, 'title', '')}>Failed</ActionButton></div>
    {collections.length > 0 && <div>{collections.map(collection => <span key={collection.id}><ActionButton className="button button-small" type="button" onClick={() => applyFilter({ collection: collection.id }, sort, '')}>{collection.name} ({collection.item_count})</ActionButton><ActionButton className="button button-small" type="button" onClick={() => void mutate(() => request(`/api/library/collections/${encodeURIComponent(collection.id)}`, { method: 'DELETE' }))}>Delete</ActionButton></span>)}</div>}
    {views.length > 0 && <div>{views.map(view => <span key={view.id}><ActionButton className="button button-small" type="button" onClick={() => applyFilter(view.filters, view.sort, '')}>{view.name}</ActionButton><ActionButton className="button button-small" type="button" onClick={() => void mutate(() => request(`/api/library/saved-views/${encodeURIComponent(view.id)}`, { method: 'DELETE' }))}>Delete</ActionButton></span>)}</div>}
    {searches.length > 0 && <div aria-label="Recent searches">Recent: {searches.map(search => <ActionButton key={search.id} className="button button-small" type="button" onClick={() => { setQuery(search.query); applyFilter({}, sort, search.query); }}>{search.query}</ActionButton>)}<ActionButton className="button button-small" type="button" onClick={() => void mutate(() => request('/api/library/recent-searches', { method: 'DELETE' }))}>Clear</ActionButton></div>}
    {preferences && <fieldset><legend>Playback preferences</legend><label>Watched at <input aria-label="Watched threshold" type="number" min="50" max="100" value={preferences.watched_threshold} onChange={event => updatePreferences({ watched_threshold: Number(event.target.value) })} />%</label><label><input type="checkbox" checked={preferences.autoplay_next} onChange={event => updatePreferences({ autoplay_next: event.target.checked })} /> Autoplay already-playable next episode only</label><label>Requested metadata language<select value={preferences.metadata_language} onChange={event => updatePreferences({ metadata_language: event.target.value })}><option value="en">English</option><option value="hi">Hindi</option></select></label><p className="muted small">Providers may ignore the requested language. Autoplay never queues a media job.</p></fieldset>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

/** LibraryInspector props: item is the selected enriched library item; onChanged refreshes its owner after saved local edits. */
export function LibraryInspector({ item, onChanged }: { item: LibraryItem; onChanged?: () => void }) {
  const [title, setTitle] = useState(item.title);
  const [series, setSeries] = useState(item.series || '');
  const [season, setSeason] = useState(item.season?.toString() || '');
  const [episode, setEpisode] = useState(item.episode?.toString() || '');
  const [type, setType] = useState(item.type || 'movie');
  const [message, setMessage] = useState('');
  useEffect(() => { setTitle(item.title); setSeries(item.series || ''); setSeason(item.season?.toString() || ''); setEpisode(item.episode?.toString() || ''); setType(item.type || 'movie'); }, [item.id, item.title, item.series, item.season, item.episode, item.type]);
  const save = async () => {
    try { await request(`/api/library/videos/${encodeURIComponent(item.id)}/override`, { method: 'POST', body: JSON.stringify({ title, type, series: series || null, season: season === '' ? null : Number(season), episode: episode === '' ? null : Number(episode) }) }); setMessage('Local details saved.'); onChanged?.(); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Unable to save details'); }
  };
  const toggleWatched = async () => { try { await request(`/api/library/videos/${encodeURIComponent(item.id)}/watched`, { method: 'POST', body: JSON.stringify({ watched: !item.watched }) }); setMessage(item.watched ? 'Marked unwatched.' : 'Marked watched.'); onChanged?.(); } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Unable to update watched state'); } };
  return <section className="library-inspector" aria-label={`Edit ${item.title}`}><label>Title <input value={title} maxLength={500} onChange={event => setTitle(event.target.value)} /></label><label>Type <select value={type} onChange={event => setType(event.target.value as 'movie' | 'series')}><option value="movie">Movie</option><option value="series">Series</option></select></label><label>Series <input value={series} maxLength={500} onChange={event => setSeries(event.target.value)} /></label><label>Season <input type="number" min="0" max="999" value={season} onChange={event => setSeason(event.target.value)} /></label><label>Episode <input type="number" min="1" max="9999" value={episode} onChange={event => setEpisode(event.target.value)} /></label><ActionButton className="button button-small" type="button" onClick={() => void save()}>Save local details</ActionButton><ActionButton className="button button-small" type="button" onClick={() => void toggleWatched()}>{item.watched ? 'Mark unwatched' : 'Mark watched'}</ActionButton><details><summary>Metadata provenance</summary><pre>{JSON.stringify({ metadata: item.metadata, overrides: item.overrides }, null, 2)}</pre></details>{message && <p role="status">{message}</p>}</section>;
}

type DownloadPlan = {
  id: string; video_ids: string[]; known_bytes: number; unknown_count: number; excluded: string[]; expires_at: number;
  items: Array<{ id: string; title: string; source_kind: string; size_bytes: number | null; excluded_reason: string | null }>;
};
const omissionLabels: Record<string, string> = { active_job: 'already queued or running', already_playable: 'already playable', cache_removal: 'cache removal in progress', source_unavailable: 'source unavailable' };

/** Confirmation uses the server's reviewed source snapshot, not the current checkbox count. */
export function ConfirmedSeasonDownload({ videoIds, onChanged }: { videoIds: string[]; onChanged?: () => void }) {
  const [plan, setPlan] = useState<DownloadPlan | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const prepare = async () => {
    setBusy(true); setMessage('');
    try { setPlan(await request<DownloadPlan>('/api/library/download-plans', { method: 'POST', body: JSON.stringify({ video_ids: videoIds }) })); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Unable to prepare download'); }
    finally { setBusy(false); }
  };
  const confirm = async () => {
    if (!plan) return;
    setBusy(true); setMessage('');
    try {
      const result = await request<{ queued_job_ids: string[]; excluded: string[] }>(`/api/library/download-plans/${plan.id}/confirm`, { method: 'POST' });
      setMessage(`${result.queued_job_ids.length} downloads queued; ${result.excluded.length} excluded.`);
      onChanged?.();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'Unable to confirm download'); }
    finally { setPlan(null); setBusy(false); }
  };
  return <div className="season-download">
    {!plan ? <ActionButton className="button button-small" type="button" disabled={busy || !videoIds.length} onClick={() => void prepare()}>{busy ? 'Preparing plan…' : 'Prepare season download'}</ActionButton> : <>
      <p>{plan.video_ids.length} episodes eligible; {plan.excluded.length} excluded. {plan.known_bytes.toLocaleString()} bytes known; {plan.unknown_count} sizes unknown.</p>
      <ul>{plan.items.map(item => <li key={item.id}>{item.title} ({item.source_kind}; {item.size_bytes === null ? 'size unknown' : `${item.size_bytes.toLocaleString()} bytes`}){item.excluded_reason ? ` — excluded: ${omissionLabels[item.excluded_reason] ?? 'unavailable'}` : ' — selected'}</li>)}</ul>
      <p className="muted small">The exact sources and sizes will be rechecked. Confirmation expires at {new Date(plan.expires_at * 1000).toLocaleTimeString()}.</p>
      <ActionButton className="button button-primary button-small" type="button" disabled={busy || !plan.video_ids.length} onClick={() => void confirm()}>{busy ? 'Confirming…' : 'Confirm download'}</ActionButton>
      <ActionButton className="button button-small" type="button" disabled={busy} onClick={() => setPlan(null)}>Cancel</ActionButton>
    </>}
    {message && <p role="status">{message}</p>}
  </div>;
}
