import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, AlertTriangle, Check, Download, Film, FolderOpen, Library, Link, LogOut, Play, Plus, RefreshCw, Search, Server, Settings as SettingsIcon, Trash2, X } from 'lucide-react';
import { api, ApiError, type ActivityLog, type ServerSnapshot, type TorrentioEpisodeResult, type TorrentioStream } from './api';
import { Login } from './Login';
import { ActionButton } from './ActionButton';
import { DeviceDownload } from './DeviceDownload';
import { Discover } from './Discover';
import { JobStages } from './DiagnosticsDetails';
import { StorageTools } from './StorageTools';
import { PwaInstall } from './PwaInstall';
import { clearPwaShellCache } from './pwa';
import { SecurityPanel } from './SecurityPanel';
import { Notifications } from './Notifications';
import { LibraryControls } from './LibraryTools';
import { LibraryDetails } from './LibraryDetails';
import { SeasonSelection } from './SeasonSelection';
import { mediaActionHint } from './buttonMetadata';
import { Player } from './Player';
import type { Job, Settings, Source, Video, VideoState } from './types';
import { canDeleteCache, formatBytes, stateLabel } from './utils';

type View = 'library' | 'discover' | 'sources' | 'downloads' | 'server' | 'notifications' | 'settings';
const nav: Array<{ id: View; label: string; icon: typeof Library }> = [
  { id: 'library', label: 'Drive library', icon: Library },
  { id: 'discover', label: 'Discover', icon: Search },
  { id: 'sources', label: 'Sources', icon: Server },
  { id: 'downloads', label: 'Downloads', icon: Download },
  { id: 'server', label: 'Server', icon: Activity },
  { id: 'notifications', label: 'Notifications', icon: AlertTriangle },
  { id: 'settings', label: 'Settings', icon: SettingsIcon }
];

function Notice({ children }: { children: React.ReactNode }) { return <p className="notice" role="status"><Check size={16} />{children}</p>; }
function Failure({ children }: { children: React.ReactNode }) { return <p className="form-error" role="alert"><AlertTriangle size={16} />{children}</p>; }

function StageProgress({ stage, processed, total, unit = 'items' }: { stage: string; processed?: number; total?: number | null; unit?: string }) {
  const totalValue = typeof total === 'number' && Number.isFinite(total) && total > 0 ? total : null;
  const value = totalValue === null ? null : Math.max(0, Math.min(100, ((processed ?? 0) / totalValue) * 100));
  const detail = totalValue === null ? 'Progress is still being measured.' : `${processed ?? 0} of ${totalValue} ${unit}`;
  return <div className="stage-progress" role="status"><div className="stage-progress-heading"><span>{stage}</span><span>{value === null ? 'Working…' : `${Math.round(value)}%`}</span></div><div className={`progress ${value === null ? 'progress-indeterminate' : ''}`} role="progressbar" aria-label={stage} aria-valuemin={totalValue === null ? undefined : 0} aria-valuemax={totalValue === null ? undefined : 100} aria-valuenow={value ?? undefined} aria-valuetext={value === null ? detail : `${Math.round(value)}%, ${detail}`}><i style={value === null ? undefined : { width: `${value}%` }} /></div><p className="muted small">{detail}</p></div>;
}

function JobProgress({ job }: { job: Job }) {
  const measured = job.progress_known === true && job.progress_unit !== 'indeterminate' && Number.isFinite(job.progress);
  const percent = measured ? Math.max(0, Math.min(100, job.progress)) : null;
  const detail = job.progress_unit === 'bytes' && job.total_bytes !== null
    ? `${formatBytes(job.bytes_downloaded)} of ${formatBytes(job.total_bytes)}`
    : job.progress_unit === 'media_seconds' ? 'Media duration is being processed.'
      : job.progress_unit === 'items' ? 'Items are being processed.'
        : 'Progress is not measurable for this stage.';
  return <div className="job-progress" role="status"><div className="job-progress-heading"><span>{job.phase}</span><span>{percent === null ? 'Working…' : `${Math.round(percent)}%`}</span></div><div className={`progress ${percent === null ? 'progress-indeterminate' : ''}`} role="progressbar" aria-label={`${job.phase} progress`} aria-valuemin={percent === null ? undefined : 0} aria-valuemax={percent === null ? undefined : 100} aria-valuenow={percent ?? undefined} aria-valuetext={percent === null ? detail : `${Math.round(percent)}%, ${detail}`}><i style={percent === null ? undefined : { width: `${percent}%` }} /></div><p>{detail}</p></div>;
}

function UrlModal({ onClose, onAdded }: { onClose: () => void; onAdded: (video: Video, reused: boolean) => void }) {
  const [url, setUrl] = useState('');
  const [title, setTitle] = useState('');
  const [download, setDownload] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try { const added = await api.addUrl({ url, title: title || undefined, engine: 'auto', download }); onAdded(added, added.reused === true); onClose(); }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not add this URL.'); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation"><form className="modal" onSubmit={submit} aria-label="Add a URL">
    <ActionButton hint="Close this form without registering a URL." action="close-url-form" className="icon-button close" type="button" onClick={onClose} aria-label="Close"><X /></ActionButton>
    <p className="kicker">Add a title</p><h2>Register a video URL</h2>
    <p className="muted">Add a link, magnet URI, or .torrent source. DriveTheater selects an available downloader; media moves only if you choose Download or Play later.</p>
    <label>URL, magnet link, or .torrent source<input type="text" placeholder="https://… or magnet:?xt=…" value={url} onChange={(e) => setUrl(e.target.value)} required /></label>
    <label>Display title <span className="optional">optional</span><input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
    <label className="checkbox"><input type="checkbox" checked={download} onChange={(e) => setDownload(e.target.checked)} />Start a download now</label>
    {busy && <StageProgress stage={download ? 'Registering URL and starting download' : 'Registering URL'} />}{error && <Failure>{error}</Failure>}<ActionButton hint={download ? 'Register this URL and explicitly start a download to the server.' : 'Register this URL as metadata only. No media downloads until you choose Play or Download.'} action={download ? 'add-and-download' : 'add-url'} className="button button-primary" disabled={busy}>{busy ? 'Adding…' : download ? 'Add and download' : 'Add to library'}</ActionButton>
  </form></div>;
}

type LibraryCardAction = 'play' | 'download' | 'delete' | 'details';
type LibraryCardViewProps = { video: Video; previewing: boolean; onPreviewing: (previewing: boolean) => void; onAction: (kind: LibraryCardAction, video: Video) => void };

export function resumeProgress(video: Video): number | null {
  const duration = video.duration;
  const position = video.watch_position;
  if (
    typeof duration !== 'number' ||
    !Number.isFinite(duration) ||
    duration <= 0 ||
    typeof position !== 'number' ||
    !Number.isFinite(position) ||
    position <= 0
  ) {
    return null;
  }
  return Math.max(0, Math.min(100, (position / duration) * 100));
}

export function LibraryCardView({ video, previewing, onPreviewing, onAction }: LibraryCardViewProps) {
  const resumePercent = resumeProgress(video);
  const hasPreview = Boolean(video.preview_url && (video.state === 'cached' || video.state === 'playable'));
  const hoverPreviewEnabled = hasPreview && (typeof window === 'undefined' || typeof window.matchMedia !== 'function' || !window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  return <article className="film-card" onPointerEnter={() => hoverPreviewEnabled && onPreviewing(true)} onPointerLeave={() => onPreviewing(false)}>
    <div className="artwork">
      {previewing && video.preview_url ? <video className="card-preview" src={video.preview_url} muted loop autoPlay playsInline aria-label={`Cached preview of ${video.title}`} /> : video.poster_url || video.thumbnail_url ? <img src={video.poster_url ?? video.thumbnail_url ?? undefined} alt="" /> : <div className="artwork-fallback"><Film size={30} /></div>}
      <span className={`state-pill state-${video.state}`}>{stateLabel(video.state)}</span>    </div>
    <div className="film-copy"><h2 title={video.title}>{video.title}</h2><p>{video.duration ? `${Math.floor(video.duration / 60)} min` : 'Duration pending'} · {formatBytes(video.size_bytes)}</p>
      {resumePercent !== null && <div className="resume-progress" role="progressbar" aria-label={`Watching progress for ${video.title}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={resumePercent} aria-valuetext={`${Math.round(resumePercent)}% watched`}><i style={{ width: `${resumePercent}%` }} /></div>}
      <div className="card-actions">
        {hasPreview && <ActionButton hint={previewing ? `Hide the cached preview of “${video.title}”.` : mediaActionHint('preview', video)} action="preview" context={video.id} className="button button-small" type="button" aria-label={`${previewing ? 'Hide preview of' : 'Preview'} ${video.title}`} aria-pressed={previewing} onClick={() => onPreviewing(!previewing)}>{previewing ? 'Hide preview' : 'Preview'}</ActionButton>}
        <ActionButton hint={mediaActionHint('play', video)} action="play" context={video.id} className="button button-small button-primary" aria-label={`Play ${video.title}`} onClick={() => onAction('play', video)}><Play size={15} fill="currentColor" />Play</ActionButton>
        <ActionButton hint={mediaActionHint('download', video)} action="download" context={video.id} className="icon-button" aria-label={`Save ${video.title} to server`} onClick={() => onAction('download', video)}><Download size={17} /></ActionButton>
        {canDeleteCache(video.state) && <ActionButton hint={mediaActionHint('delete', video)} action="remove-cache" context={video.id} className="icon-button" aria-label={`Remove cached media for ${video.title}`} onClick={() => onAction('delete', video)}><Trash2 size={17} /></ActionButton>}
        <DeviceDownload video={video} />
        <ActionButton className="button button-small" onClick={() => onAction('details', video)}>Details</ActionButton>
      </div>
    </div>
  </article>;
}

function LibraryCard({ video, onAction }: { video: Video; onAction: (kind: LibraryCardAction, video: Video) => void }) { const [previewing, setPreviewing] = useState(false); return <LibraryCardView video={video} previewing={previewing} onPreviewing={setPreviewing} onAction={onAction} />; }

type SeriesEpisode = { video: Video; series: string; season: number; episode: number };
export function seriesEpisode(video: Video): SeriesEpisode | null {
  if (video.series && Number.isInteger(video.season) && Number.isInteger(video.episode)) return { video, series: video.series, season: video.season!, episode: video.episode! };
  const match = /(?:^|[ ._\-])S(\d{1,2})E(\d{1,3})(?:[ ._\-]|$)|(?:^|[ ._\-])(\d{1,2})x(\d{1,3})(?:[ ._\-]|$)/i.exec(video.title);
  if (!match) return null;
  const start = Math.max(0, match.index ?? 0);
  const series = video.title.slice(0, start).replace(/[._-]+/g, ' ').replace(/\s+/g, ' ').trim() || video.title;
  return { video, series, season: Number(match[1] ?? match[3]), episode: Number(match[2] ?? match[4]) };
}
function GroupedEpisodeCard({ entry, onAction }: { entry: SeriesEpisode; onAction: (kind: LibraryCardAction, video: Video) => void }) {
  const resumePercent = resumeProgress(entry.video);
  return <article className="episode-card"><div><strong>E{String(entry.episode).padStart(2, '0')}</strong><span>{entry.video.title}</span>{resumePercent !== null && <div className="episode-resume-progress" role="progressbar" aria-label={`Watching progress for ${entry.video.title}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={resumePercent} aria-valuetext={`${Math.round(resumePercent)}% watched`}><i style={{ width: `${resumePercent}%` }} /></div>}</div><div className="card-actions"><ActionButton hint={mediaActionHint('play', entry.video)} action="play" context={entry.video.id} aria-label={`Play ${entry.video.title}`} className="button button-small button-primary" onClick={() => onAction('play', entry.video)}><Play size={15} fill="currentColor" />Play</ActionButton><ActionButton hint={mediaActionHint('download', entry.video)} action="download" context={entry.video.id} className="icon-button" aria-label={`Save ${entry.video.title} to server`} onClick={() => onAction('download', entry.video)}><Download size={17} /></ActionButton><DeviceDownload video={entry.video} /><ActionButton className="button button-small" onClick={() => onAction('details', entry.video)}>{entry.video.watched ? 'Watched · Details' : 'Details'}</ActionButton></div></article>;
}

type LibraryEntry = { kind: 'video'; video: Video } | { kind: 'series'; title: string; episodes: SeriesEpisode[] };

export function libraryEntries(items: Video[]): LibraryEntry[] {
  const result: LibraryEntry[] = [];
  const grouped = new Map<string, Extract<LibraryEntry, { kind: 'series' }>>();
  for (const video of items) {
    const episode = seriesEpisode(video);
    if (!episode) { result.push({ kind: 'video', video }); continue; }
    let entry = grouped.get(episode.series);
    if (!entry) {
      entry = { kind: 'series', title: episode.series, episodes: [] };
      grouped.set(episode.series, entry);
      result.push(entry);
    }
    entry.episodes.push(episode);
  }
  return result;
}

export function SeriesCard({ title, episodes, onOpen }: { title: string; episodes: SeriesEpisode[]; onOpen: () => void }) {
  const poster = episodes.map(({ video }) => video.poster_url || video.thumbnail_url).find(Boolean);
  const seasons = new Set(episodes.map((episode) => episode.season)).size;
  return <article className="film-card series-card"><ActionButton className="series-open" action="open-series" context={title} aria-label={`Open series ${title}`} hint={`Open “${title}” to browse ${seasons} seasons and ${episodes.length} episodes. No media download starts.`} onClick={onOpen}>
    <span className="artwork">{poster ? <img src={poster} alt="" /> : <span className="artwork-fallback"><Film size={34} /></span>}<span className="series-pill">Series</span></span>
    <span className="film-copy"><strong className="series-name">{title}</strong><span className="series-count">{seasons} season{seasons === 1 ? '' : 's'} · {episodes.length} episode{episodes.length === 1 ? '' : 's'}</span><span className="series-open-label">View seasons and episodes</span></span>
  </ActionButton></article>;
}

export function GroupedLibrary({ items, onAction }: { items: Video[]; onAction: (kind: LibraryCardAction, video: Video) => void }) {
  const [openedSeries, setOpenedSeries] = useState<string | null>(null);
  const entries = libraryEntries(items);
  const selected = entries.find((entry): entry is Extract<LibraryEntry, { kind: 'series' }> => entry.kind === 'series' && entry.title === openedSeries);
  if (selected) {
    const bySeason = selected.episodes.reduce<Record<number, SeriesEpisode[]>>((all, episode) => { (all[episode.season] ??= []).push(episode); return all; }, {});
    return <section className="library-series series-detail" aria-label={`Seasons and episodes of ${selected.title}`}>
      <ActionButton className="button button-small series-back" action="back-to-library" hint="Return to the mixed movie and series library." onClick={() => setOpenedSeries(null)}>Back to all titles</ActionButton>
      <div className="series-detail-heading"><span className="series-pill">Series</span><h2>{selected.title}</h2><p className="muted">Choose a season, then Play or Download an episode. Browsing never starts a download.</p></div>
      {Object.entries(bySeason).sort(([a], [b]) => Number(a) - Number(b)).map(([season, episodes]) => <details key={season} open><summary>{Number(season) === 0 ? 'Specials' : `Season ${season}`} <span>{episodes.filter(entry => entry.video.watched).length} watched of {episodes.length}</span></summary><SeasonSelection episodes={episodes.map(entry => entry.video)} /><div className="film-grid episode-film-grid">{[...episodes].sort((a, b) => a.episode - b.episode).map((entry) => <GroupedEpisodeCard key={entry.video.id} entry={entry} onAction={onAction} />)}</div></details>)}
    </section>;
  }
  return <div className="film-grid" aria-label="Movies and series">{entries.map((entry) => entry.kind === 'series'
    ? <SeriesCard key={`series:${entry.title}`} title={entry.title} episodes={entry.episodes} onOpen={() => setOpenedSeries(entry.title)} />
    : <LibraryCard key={`video:${entry.video.id}`} video={entry.video} onAction={onAction} />)}</div>;
}

function LibraryPage({ onPlayer, refreshKey }: { onPlayer: (video: Video) => void; refreshKey: number }) {
  const [items, setItems] = useState<Video[]>([]);
  const [search, setSearch] = useState('');
  const [state, setState] = useState('');
  const [watchingOnly, setWatchingOnly] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [modal, setModal] = useState(false);
  const [message, setMessage] = useState('');
  const [filters, setFilters] = useState<Record<string, unknown>>({});
  const [sort, setSort] = useState('title');
  const [inspecting, setInspecting] = useState<Video | null>(null);
  const load = useCallback(async (signal?: AbortSignal) => {
    try { const data = await api.library({ ...filters, ...(state ? { state } : {}) }, sort, search, signal); setItems(data.items); setError(''); }
    catch (err) { if (!(err instanceof DOMException)) setError(err instanceof Error ? err.message : 'Could not load the library.'); }
    finally { setLoading(false); }
  }, [search, state, filters, sort]);
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); const timer = window.setInterval(() => void load(controller.signal), 2000); return () => { controller.abort(); window.clearInterval(timer); }; }, [load, refreshKey]);
  const action = async (kind: LibraryCardAction, video: Video) => {
    try {
      if (kind === 'play') { const result = await api.play(video.id); setItems((all) => all.map((v) => v.id === result.video.id ? result.video : v)); onPlayer(result.video); }
      if (kind === 'download') { const result = await api.download(video.id); setItems((all) => all.map((v) => v.id === result.video.id ? result.video : v)); setMessage('Download added to the queue.'); }
      if (kind === 'details') { setInspecting(video); return; }
      if (kind === 'delete') { if (!window.confirm(`Remove cached media for “${video.title}”? The source catalog entry stays.`)) return; await api.deleteCache(video.id); setItems((all) => all.map((v) => v.id === video.id ? { ...v, state: 'cloud' } : v)); setMessage('Cached media was removed.'); }
    } catch (err) { setError(err instanceof Error ? err.message : 'That action could not be completed.'); }
  };
  const continueWatching = items.filter((video) => (video.watch_position ?? 0) > 0 && (video.duration == null || video.watch_position! < video.duration - 5));
  const shown = watchingOnly ? continueWatching : items;
  return <>
    <header className="page-head"><div><p className="kicker">Screening room</p><h1>Drive library</h1><p className="muted">All synced titles stay as metadata until you choose Play or save one to the server.</p></div><ActionButton className="button button-primary" onClick={() => setModal(true)}><Plus size={18} />Add URL</ActionButton></header>
    <section className="filter-row" aria-label="Library filters"><label className="search"><Search size={18} /><input placeholder="Search titles" value={search} onChange={(e) => setSearch(e.target.value)} /></label><label className="select-label">Status<select value={state} onChange={(e) => setState(e.target.value)}><option value="">All statuses</option>{(['cloud', 'queued', 'downloading', 'preparing', 'playable', 'cached', 'failed', 'cancelled'] as VideoState[]).map((key) => <option key={key} value={key}>{stateLabel(key)}</option>)}</select></label><label className="checkbox"><input type="checkbox" checked={watchingOnly} onChange={(event) => setWatchingOnly(event.target.checked)} />Continue watching</label></section>
    <details className="library-tools-shell"><summary>Library tools, collections and playback preferences</summary><LibraryControls onChanged={() => void load()} onFilter={(nextFilters, nextSort, query) => { setFilters(nextFilters); setSort(nextSort); setSearch(query); setState(''); setWatchingOnly(false); }} /></details>
    {inspecting && <LibraryDetails video={items.find(video => video.id === inspecting.id) ?? inspecting} onClose={() => setInspecting(null)} onChanged={() => void load()} />}
    {message && <Notice>{message}</Notice>}{error && <Failure>{error}</Failure>}
    {!loading && !watchingOnly && continueWatching.length > 0 && <section className="continue-watching" aria-label="Continue watching"><div className="section-heading"><h2>Continue watching</h2><p className="muted small">Resumes from your last recorded position.</p></div><div className="film-grid continue-grid">{continueWatching.map((video) => <LibraryCard key={video.id} video={video} onAction={(kind, selected) => void action(kind, selected)} />)}</div></section>}
    {loading ? <p className="empty-state">Opening the library…</p> : shown.length === 0 ? <section className="empty-state library-empty"><FolderOpen size={32} /><h2>{watchingOnly ? 'Nothing is waiting to resume.' : 'Your library is ready for a first title.'}</h2><p>{watchingOnly ? 'Play a title and its last position will appear here.' : 'Connect a Google Drive folder in Sources, or add a media URL here. Browsing will not download any media.'}</p>{!watchingOnly && <ActionButton className="button button-primary" onClick={() => setModal(true)}><Link size={17} />Add a URL</ActionButton>}</section> : <GroupedLibrary items={shown} onAction={(kind, selected) => void action(kind, selected)} />}
    {modal && <UrlModal onClose={() => setModal(false)} onAdded={(video, reused) => { setItems((all) => all.some((item) => item.id === video.id) ? all : [video, ...all]); setMessage(reused ? 'Existing title reused from your library.' : 'Title added to the library.'); }} />}
  </>;
}

function SourceForm({ onAdded }: { onAdded: () => void }) {
 const [name, setName] = useState(''); const [folder, setFolder] = useState(''); const [mode, setMode] = useState<'public_link' | 'api_key' | 'service_account' | 'oauth'>('public_link'); const [credential, setCredential] = useState(''); const [redirect, setRedirect] = useState(''); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
 const submit = async (event: React.FormEvent) => { event.preventDefault(); setBusy(true); setError(''); try { const payload: Record<string, unknown> = { name, folder, auth_mode: mode }; if (mode === 'api_key') payload.api_key = credential; if (mode === 'service_account' || mode === 'oauth') { const parsed: unknown = JSON.parse(credential); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Enter a JSON object exported by Google Cloud.'); if (mode === 'service_account') payload.service_account_json = parsed; else { payload.oauth_client_json = parsed; if (redirect) payload.redirect_base_url = redirect; } } await api.addSource(payload); setName(''); setFolder(''); setCredential(''); onAdded(); } catch (err) { setError(err instanceof Error ? err.message : 'Could not add this source.'); } finally { setBusy(false); } };
 return <details className="source-form"><summary><Plus size={17} />Connect a Google Drive folder</summary><form onSubmit={submit}><div className="field-grid"><label>Source name<input value={name} onChange={(e) => setName(e.target.value)} required /></label><label>Folder link or ID<input value={folder} onChange={(e) => setFolder(e.target.value)} required /></label></div><fieldset><legend>Authorization</legend><label className="radio"><input type="radio" checked={mode === 'public_link'} onChange={() => setMode('public_link')} />Public shared folder (no Cloud project)</label><label className="radio"><input type="radio" checked={mode === 'api_key'} onChange={() => setMode('api_key')} />Public metadata API key</label><label className="radio"><input type="radio" checked={mode === 'service_account'} onChange={() => setMode('service_account')} />Service account JSON</label><label className="radio"><input type="radio" checked={mode === 'oauth'} onChange={() => setMode('oauth')} />OAuth client JSON</label></fieldset>{mode !== 'public_link' && <label>{mode === 'api_key' ? 'API key' : mode === 'service_account' ? 'Service account JSON' : 'OAuth client JSON'}<textarea value={credential} onChange={(e) => setCredential(e.target.value)} required /></label>}{mode === 'oauth' && <label>Public redirect base URL <span className="optional">optional</span><input type="url" value={redirect} onChange={(e) => setRedirect(e.target.value)} placeholder="https://theater.example" /></label>}<details className="source-help"><summary>Connection guide</summary>{mode === 'public_link' && <p>Use an <strong>Anyone with the link</strong> folder. No Cloud project is needed for public links. Private folders still need OAuth or a shared service account; availability of rclone depends on this server.</p>}{mode === 'api_key' && <p>Set the folder to <strong>Anyone with the link</strong>, enable the Google Drive API in Google Cloud, then enter that project’s API key.</p>}{mode === 'service_account' && <p>Create a service account in Google Cloud, paste its downloaded JSON object, then share the Drive folder with that service account’s email address.</p>}{mode === 'oauth' && <p>Create Google OAuth web application credentials, paste the downloaded JSON object, and register this server’s exact callback path shown after reconnecting. The redirect base URL must be the public origin that serves DriveTheater.</p>}</details><p className="muted small">Credentials are sent only to this server and are never returned to the browser. Folder access still needs to be granted to the selected identity.</p>{busy && <StageProgress stage="Saving Drive connection" />}{error && <Failure>{error}</Failure>}<ActionButton className="button button-primary" disabled={busy}>{busy ? 'Connecting…' : 'Connect source'}</ActionButton></form></details>;
}


function TorrentioLookup() {
  const [manifestUrl, setManifestUrl] = useState('');
  const [metadataManifestUrl, setMetadataManifestUrl] = useState('');
  const [mediaType, setMediaType] = useState<'movie' | 'series'>('movie');
  const [mediaId, setMediaId] = useState('');
  const [items, setItems] = useState<TorrentioStream[]>([]);
  const [episodes, setEpisodes] = useState<TorrentioEpisodeResult | null>(null);
  const [season, setSeason] = useState<number | null>(null);
  const [unsupportedCount, setUnsupportedCount] = useState<number | null>(null);
  const [busy, setBusy] = useState(false); const [episodesBusy, setEpisodesBusy] = useState(false); const [adding, setAdding] = useState<string | null>(null);
  const [message, setMessage] = useState(''); const [error, setError] = useState('');
  const baseImdbId = mediaId.trim().split(':')[0];
  const findStreams = async (event: React.FormEvent) => {
    event.preventDefault(); const normalizedId = mediaId.trim();
    if (!/^tt\d+(?::\d+:\d+)?$/i.test(normalizedId)) { setError('Enter an IMDb ID such as tt1234567, or choose an episode for tt1234567:1:2.'); return; }
    if (mediaType === 'series' && !/^tt\d+:\d+:\d+$/i.test(normalizedId)) { setError('Choose an episode before finding series streams.'); return; }
    setBusy(true); setError(''); setMessage(''); setItems([]); setUnsupportedCount(null);
    try { const result = await api.torrentioStreams({ manifest_url: manifestUrl.trim(), media_type: mediaType, media_id: normalizedId }); setItems(result.items); setUnsupportedCount(result.unsupported_count); setMessage(result.items.length ? `${result.items.length} compatible stream${result.items.length === 1 ? '' : 's'} found.` : 'No compatible streams were returned.'); }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not find streams.'); } finally { setBusy(false); }
  };
  const loadEpisodes = async () => {
    if (!/^tt\d+$/i.test(baseImdbId)) { setError('Enter a series IMDb ID such as tt1234567 before loading episodes.'); return; }
    setEpisodesBusy(true); setError(''); setMessage('');
    try { const result = await api.torrentioEpisodes({ media_id: baseImdbId, metadata_manifest_url: metadataManifestUrl.trim() || undefined }); setEpisodes(result); setSeason(result.episodes[0]?.season ?? null); setMessage(`${result.episodes.length} episode${result.episodes.length === 1 ? '' : 's'} loaded. Choose one to find streams; this does not play or download it.`); }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not load episodes.'); } finally { setEpisodesBusy(false); }
  };
  const addStream = async (stream: TorrentioStream) => { setAdding(stream.magnet_url); setError(''); try { const added = await api.addUrl({ url: stream.magnet_url, engine: 'auto', title: stream.title || stream.name }); setMessage(added.reused ? 'An existing library title was reused. It has not started downloading.' : `Added ${stream.title || stream.name} to the library. It has not started downloading.`); } catch (err) { setError(err instanceof Error ? err.message : 'Could not add this stream to the library.'); } finally { setAdding(null); } };
  const seasons = [...new Set(episodes?.episodes.map((episode) => episode.season) ?? [])].sort((a, b) => a - b);
  const shownEpisodes = episodes?.episodes.filter((episode) => episode.season === season) ?? [];
  return <details className="source-form torrentio-lookup"><summary><Search size={17} />Find compatible streams</summary><form onSubmit={findStreams}><p className="muted small">Use a compatible add-on you supply for media you are authorized to access. Stream availability is unverified; looking up or selecting an episode never plays or downloads media.</p><label>Stream manifest URL<input type="url" value={manifestUrl} onChange={(event) => setManifestUrl(event.target.value)} placeholder="https://…/manifest.json" required /></label><div className="field-grid"><label>Media type<select value={mediaType} onChange={(event) => { const next = event.target.value as 'movie' | 'series'; setMediaType(next); setItems([]); }}><option value="movie">Movie</option><option value="series">Series</option></select></label><label>IMDb ID<input value={mediaId} onChange={(event) => { setMediaId(event.target.value); if (episodes) setEpisodes(null); }} placeholder={mediaType === 'series' ? 'tt1234567' : 'tt1234567'} required /></label></div>{mediaType === 'series' && <section className="episode-picker" aria-label="Series episode picker"><label>Metadata manifest URL <span className="optional">optional</span><input type="url" value={metadataManifestUrl} onChange={(event) => setMetadataManifestUrl(event.target.value)} placeholder="Uses the server default when left blank" /></label><p className="muted small">Loading metadata sends the IMDb ID to the selected metadata add-on, or the server’s public default, only after you press Load episodes.</p><ActionButton className="button button-small" type="button" disabled={episodesBusy} onClick={() => void loadEpisodes()}>{episodesBusy ? 'Loading episodes…' : 'Load episodes'}</ActionButton>{episodesBusy && <StageProgress stage="Looking up series episodes" />}{episodes && <><div className="episode-title">{episodes.poster && <img src={episodes.poster} alt="" />}<h3>{episodes.title}</h3></div><label className="compact-select">Season<select value={season ?? ''} onChange={(event) => setSeason(Number(event.target.value))}>{seasons.map((value) => <option key={value} value={value}>Season {value}</option>)}</select></label><div className="episode-list">{shownEpisodes.map((episode) => <ActionButton hint={`Select season ${episode.season}, episode ${episode.episode}: “${episode.title}”. This only selects metadata; choose Find streams next.`} action="select-episode" aria-pressed={mediaId === `${baseImdbId}:${episode.season}:${episode.episode}`} key={episode.id} type="button" className={mediaId === `${baseImdbId}:${episode.season}:${episode.episode}` ? 'episode-current' : ''} onClick={() => { setMediaId(`${baseImdbId}:${episode.season}:${episode.episode}`); setItems([]); setMessage(`Selected S${String(episode.season).padStart(2, '0')}E${String(episode.episode).padStart(2, '0')}. Find streams when ready.`); }}><span>S{String(episode.season).padStart(2, '0')}E{String(episode.episode).padStart(2, '0')}</span><strong>{episode.title}</strong>{episode.released && <small>{episode.released}</small>}</ActionButton>)}</div></>}</section>}{busy && <StageProgress stage="Looking up compatible streams" />}{error && <Failure>{error}</Failure>}{message && <Notice>{message}</Notice>}{unsupportedCount !== null && unsupportedCount > 0 && <p className="muted small" role="status">{unsupportedCount} returned stream{unsupportedCount === 1 ? '' : 's'} could not be used by this server.</p>}<ActionButton className="button button-primary" disabled={busy}>{busy ? 'Finding streams…' : 'Find streams'}</ActionButton>{items.length > 0 && <div className="torrentio-results" aria-label="Compatible streams">{items.map((stream) => <article className="torrentio-stream" key={stream.magnet_url}><div><h3>{stream.title || stream.name}</h3>{stream.name && stream.title && stream.name !== stream.title && <p className="muted small">{stream.name}</p>}</div><ActionButton className="button button-small" type="button" disabled={adding !== null} onClick={() => void addStream(stream)}>{adding === stream.magnet_url ? 'Adding…' : 'Add to library'}</ActionButton></article>)}</div>}</form></details>;
}

function SourcesPage({ refreshKey }: { refreshKey: number }) {
 const [items, setItems] = useState<Source[]>([]); const [error, setError] = useState(''); const [message, setMessage] = useState(''); const [busySource, setBusySource] = useState<{ id: string; action: 'test' | 'sync' | 'oauth' | 'delete' } | null>(null);
 const load = useCallback(async (signal?: AbortSignal) => { try { setItems((await api.sources(signal)).items); } catch (err) { if (!(err instanceof DOMException)) setError(err instanceof Error ? err.message : 'Could not load sources.'); } }, []);
 useEffect(() => { const controller = new AbortController(); void load(controller.signal); const timer = window.setInterval(() => void load(controller.signal), 2000); return () => { controller.abort(); window.clearInterval(timer); }; }, [load, refreshKey]);
 const command = async (name: 'test' | 'sync' | 'oauth' | 'delete', source: Source) => { setError(''); setBusySource({ id: source.id, action: name }); try { if (name === 'test') { const result = await api.testSource(source.id); setMessage(result.message); } if (name === 'sync') { const result = await api.syncSource(source.id); setMessage(`Sync finished: ${result.added} added, ${result.updated} updated.`); await load(); } if (name === 'oauth') window.location.assign((await api.oauthStart(source.id)).url); if (name === 'delete') { await api.deleteSource(source.id); setItems((all) => all.filter((item) => item.id !== source.id)); setMessage('Source disconnected. Cached videos were kept.'); } } catch (err) { setError(err instanceof Error ? err.message : 'This source action failed.'); } finally { setBusySource((current) => current?.id === source.id ? null : current); } };
 return <><header className="page-head"><div><p className="kicker">Ingest desk</p><h1>Sources</h1><p className="muted">Connect folders for metadata only. Media moves only after a Play or Download request.</p></div></header><SourceForm onAdded={() => void load()} /><TorrentioLookup />{message && <Notice>{message}</Notice>}{error && <Failure>{error}</Failure>}<div className="source-list">{items.length === 0 ? <section className="empty-state"><Server size={32} /><h2>No folders connected.</h2><p>Use a public shared folder, service account with shared-folder access, or OAuth for a private Google Drive folder.</p></section> : items.map((source) => <article className="source-row" key={source.id}><div className="source-symbol"><Server size={22} /></div><div className="source-main"><h2>{source.name}</h2><p>{source.video_count} videos · {source.has_credentials ? 'Credentials stored' : 'Public link'}{source.service_account_email ? ` · ${source.service_account_email}` : ''}</p>{source.health && <div className="source-health"><p>Last successful test: {source.health.last_test_success_at ? new Date(source.health.last_test_success_at * 1000).toLocaleString() : 'Not yet tested successfully'}</p><p>Last successful sync: {source.health.last_sync_success_at ? new Date(source.health.last_sync_success_at * 1000).toLocaleString() : 'Not yet synced successfully'}</p>{source.health.state === 'error' && <p role="status">{source.health.message}{source.health.retry_at ? ` Try after ${new Date(source.health.retry_at * 1000).toLocaleTimeString()}.` : source.health.retryable ? ' Check connectivity, then use Test again.' : ' Review permissions or reconnect this source.'}</p>}</div>}{source.error && <Failure>{source.error}</Failure>}{source.sync_progress && <StageProgress stage={`Drive sync: ${source.sync_progress.phase}`} processed={source.sync_progress.processed} total={source.sync_progress.total} unit="items" />}{busySource?.id === source.id && !source.sync_progress && <StageProgress stage={busySource.action === 'sync' ? 'Starting Drive sync' : busySource.action === 'test' ? 'Testing source connection' : busySource.action === 'oauth' ? 'Opening authorization' : 'Disconnecting source'} />}</div><span className={`source-status ${source.status}`}>{source.status}</span><div className="row-actions"><ActionButton className="button button-small" disabled={Boolean(busySource)} onClick={() => void command('test', source)}>{busySource?.id === source.id && busySource.action === 'test' ? 'Testing…' : 'Test'}</ActionButton><ActionButton className="button button-small" disabled={Boolean(busySource)} onClick={() => void command('sync', source)}><RefreshCw size={14} />{busySource?.id === source.id && busySource.action === 'sync' ? 'Syncing…' : 'Sync'}</ActionButton>{source.auth_mode === 'oauth' && <ActionButton className="button button-small" disabled={Boolean(busySource)} onClick={() => void command('oauth', source)}>{busySource?.id === source.id && busySource.action === 'oauth' ? 'Opening…' : 'Reconnect'}</ActionButton>}<ActionButton hint={`Disconnect “${source.name}” and remove its saved authorization. Cached videos are kept.`} action="disconnect-source" context={source.id} className="icon-button" disabled={Boolean(busySource)} onClick={() => void command('delete', source)} aria-label={`Disconnect ${source.name}`}><Trash2 size={17} /></ActionButton></div></article>)}</div></>;
}

function DownloadsPage({ refreshKey }: { refreshKey: number }) { const [jobs, setJobs] = useState<Job[]>([]); const [error, setError] = useState(''); const load = useCallback(async (signal?: AbortSignal) => { try { setJobs((await api.jobs(signal)).items); } catch (err) { if (!(err instanceof DOMException)) setError(err instanceof Error ? err.message : 'Could not load downloads.'); } }, []); useEffect(() => { const controller = new AbortController(); void load(controller.signal); const timer = window.setInterval(() => void load(controller.signal), 2000); return () => { controller.abort(); clearInterval(timer); }; }, [load, refreshKey]); const command = async (kind: 'cancel' | 'retry', id: string) => { try { const next = kind === 'cancel' ? await api.cancelJob(id) : await api.retryJob(id); setJobs((all) => all.map((job) => job.id === id ? next : job)); } catch (err) { setError(err instanceof Error ? err.message : 'That job action failed.'); } }; return <><header className="page-head"><div><p className="kicker">Machine room</p><h1>Downloads</h1><p className="muted">Progress describes the current phase, not a completed screening.</p></div></header>{error && <Failure>{error}</Failure>}<section className="job-list">{jobs.length === 0 ? <div className="empty-state"><Download size={32} /><h2>No active work.</h2><p>Choose Download from the Library when a title needs to be ready ahead of time.</p></div> : jobs.map((job) => <article className="job-row" key={job.id}><div className="job-heading"><div><h2>{job.title}</h2><p>{job.phase} · attempt {job.attempt}</p></div></div><JobProgress job={job} /><JobStages jobId={job.id} refreshKey={job.updated_at} /><div className="job-foot"><span>{formatBytes(job.bytes_downloaded)}{job.total_bytes ? ` of ${formatBytes(job.total_bytes)}` : ''}</span>{job.error && <Failure>{job.error}</Failure>}<span className="push" />{['queued', 'downloading', 'preparing'].includes(job.state) && <ActionButton className="button button-small" onClick={() => void command('cancel', job.id)}>Cancel</ActionButton>}{['failed', 'cancelled'].includes(job.state) && <ActionButton className="button button-small button-primary" onClick={() => void command('retry', job.id)}>Retry</ActionButton>}</div></article>)}</section></>; }

function seconds(value: number) { const hours = Math.floor(value / 3600); const minutes = Math.floor((value % 3600) / 60); return hours ? `${hours}h ${minutes}m` : `${minutes}m`; }
function rate(value: number | null) { return value === null ? 'Measuring…' : `${formatBytes(value)}/s`; }
function ServerTable({ title, rows }: { title: string; rows: Array<React.ReactNode[]> }) { return <section className="server-card"><h2>{title}</h2><table><tbody>{rows.map(([label, value]) => <tr key={String(label)}><th scope="row">{label}</th><td>{value}</td></tr>)}</tbody></table></section>; }
function ServerPage({ refreshKey }: { refreshKey: number }) {
 const [server, setServer] = useState<ServerSnapshot | null>(null); const [activity, setActivity] = useState<ActivityLog | null>(null); const [alerts, setAlerts] = useState<Awaited<ReturnType<typeof api.alerts>> | null>(null); const [error, setError] = useState(''); const [clearingActivity, setClearingActivity] = useState(false); const [activityMessage, setActivityMessage] = useState('');
 const load = useCallback(async (signal?: AbortSignal) => { try { const [snapshot, log, nextAlerts] = await Promise.all([api.server(signal), api.activityLog(100, signal), api.alerts(signal)]); setServer(snapshot); setActivity(log); setAlerts(nextAlerts); setError(''); } catch (err) { if (!(err instanceof DOMException)) setError(err instanceof Error ? err.message : 'Could not load server information.'); } }, []);
 useEffect(() => { const controller = new AbortController(); void load(controller.signal); const timer = window.setInterval(() => void load(controller.signal), 10000); return () => { controller.abort(); window.clearInterval(timer); }; }, [load, refreshKey]);
 const clearActivity = async () => { setClearingActivity(true); setActivityMessage(''); setError(''); try { await api.clearActivity(); setActivity((current) => current ? { ...current, items: [] } : current); setActivityMessage('Local activity history cleared.'); } catch (err) { setError(err instanceof Error ? err.message : 'Could not clear local activity history.'); } finally { setClearingActivity(false); } };
 return <><header className="page-head"><div><p className="kicker">Operations desk</p><h1>Server</h1><p className="muted">Local runtime and activity readouts. Values describe the {server?.scope === 'container_namespace' ? 'container namespace' : 'host or runtime namespace'} reported by the server.</p></div></header>{error && <Failure>{error}</Failure>}{!server || !activity || !alerts ? <p className="empty-state">Reading server status…</p> : <><section className="server-card alerts-card"><h2>Alerts</h2><p className="muted small">Checked {new Date(alerts.checked_at).toLocaleString()}.</p>{alerts.items.length === 0 ? <p className="muted">No current server alerts.</p> : <ul className="alerts-list">{alerts.items.map((alert) => <li key={alert.id} className={`alert-${alert.level}`}><strong>{alert.title}</strong><span>{alert.detail}</span></li>)}</ul>}</section><div className="server-grid"><ServerTable title="Runtime" rows={[["Scope", server.scope === 'container_namespace' ? 'Container namespace' : 'Host/runtime namespace'], ["Uptime", seconds(server.uptime_seconds)], ["CPU", `${server.cpu.percent.toFixed(1)}% of ${server.cpu.cores} cores`], ["Memory", `${formatBytes(server.memory.used_bytes)} of ${formatBytes(server.memory.total_bytes)}`], ["Media CPU", `${server.media_cpu.effective_cpus ?? 'Automatic'} effective${server.media_cpu.configured_cpus ? ` / ${server.media_cpu.configured_cpus} configured` : ''}`], ["Note", server.media_cpu.scope_note]]} /><ServerTable title="Storage and database" rows={[["Disk", `${formatBytes(server.disk.used_bytes)} used of ${formatBytes(server.disk.total_bytes)}`], ["Free", formatBytes(server.disk.free_bytes)], ["Path", server.disk.path], ["SQLite", formatBytes(server.database.sqlite_bytes)], ["WAL", formatBytes(server.database.wal_bytes)], ["Process RSS", formatBytes(server.process.rss_bytes)]]} /><ServerTable title="Network" rows={[["Sent", formatBytes(server.network.bytes_sent)], ["Received", formatBytes(server.network.bytes_recv)], ["Upload rate", rate(server.network.send_rate_bps)], ["Download rate", rate(server.network.recv_rate_bps)], ["Active jobs", String(server.active_jobs)], ["Worker PID", String(server.process.pid)]]} /><ServerTable title="Library counts" rows={Object.entries(server.counts).map(([key, value]) => [key.replace(/_/g, ' '), String(value)])} /></div><section className="server-card downloader-card"><h2>Downloader availability</h2><table><tbody>{Object.entries(server.downloaders).map(([name, downloader]) => <tr key={name}><th scope="row">{name.replace(/_/g, ' ')}</th><td>{downloader.available ? 'Available' : 'Unavailable'}</td><td>{downloader.detail}</td></tr>)}</tbody></table></section><section className="server-card activity-card"><div className="activity-card-heading"><div><h2>Local activity</h2><p className="muted small">Sign-ins and media activity are recorded on this server for {activity.retention_days} days. Passwords and external analytics are never shown here.</p></div><ActionButton className="button button-small" type="button" disabled={clearingActivity || activity.items.length === 0} onClick={() => void clearActivity()}>{clearingActivity ? 'Clearing…' : 'Clear history'}</ActionButton></div>{activityMessage && <Notice>{activityMessage}</Notice>}<table><thead><tr><th>When</th><th>User</th><th>Activity</th><th>Details</th></tr></thead><tbody>{activity.items.length === 0 ? <tr><td colSpan={4}>No local activity in the retention window.</td></tr> : activity.items.map((entry, index) => <tr key={`${entry.created_at}-${index}`}><td>{new Date(entry.occurred_at).toLocaleString()}</td><td>{entry.username}</td><td>{entry.event_type.replace(/_/g, ' ')}</td><td>{entry.details ? Object.entries(entry.details).map(([key, value]) => `${key}: ${String(value)}`).join(' · ') : '—'}</td></tr>)}</tbody></table></section></>}</>;
}

function settingsForm(value: Settings): Record<string, number | boolean> {
  return {
    cache_limit_gb: value.cache_limit_gb,
    min_free_gb: value.min_free_gb,
    download_concurrency: value.download_concurrency,
    max_transcodes: value.max_transcodes,
    download_schedule_enabled: value.download_schedule_enabled ?? false,
    download_start_hour: value.download_start_hour ?? 0,
    download_end_hour: value.download_end_hour ?? 0,
    download_limit_mbps: value.download_limit_mbps ?? 0,
    auto_cache_cleanup: value.auto_cache_cleanup ?? false,
    cache_cleanup_target_percent: value.cache_cleanup_target_percent ?? 85,
    activity_retention_days: value.activity_retention_days ?? 30
  };
}

function SettingsPage({ refreshKey }: { refreshKey: number }) {
 const [settings, setSettings] = useState<Settings | null>(null); const [form, setForm] = useState<Record<string, number | boolean>>({}); const dirtyRef = useRef(false); const [message, setMessage] = useState(''); const [error, setError] = useState(''); const [current, setCurrent] = useState(''); const [next, setNext] = useState(''); const [passwordFactor, setPasswordFactor] = useState('');
 const load = useCallback(async (signal?: AbortSignal) => { try { const value = await api.settings(signal); setSettings(value); if (!dirtyRef.current) setForm(settingsForm(value)); setError(''); } catch (err) { if (!(err instanceof DOMException)) setError(err instanceof Error ? err.message : 'Could not load settings.'); } }, []);
 useEffect(() => { const controller = new AbortController(); void load(controller.signal); const timer = window.setInterval(() => void load(controller.signal), 5000); return () => { controller.abort(); window.clearInterval(timer); }; }, [load, refreshKey]);
 const setField = (key: string, value: number | boolean) => { dirtyRef.current = true; setForm((currentForm) => ({ ...currentForm, [key]: value })); };
 const setNumber = (key: string, value: string) => setField(key, Number(value));
 const save = async (event: React.FormEvent) => { event.preventDefault(); try { const value = await api.updateSettings({ ...form, download_concurrency: Math.min(2, Math.max(1, Number(form.download_concurrency))), max_transcodes: Math.min(2, Math.max(1, Number(form.max_transcodes))) }); setSettings(value); setForm(settingsForm(value)); dirtyRef.current = false; setMessage('Settings saved.'); } catch (err) { setError(err instanceof Error ? err.message : 'Settings could not be saved.'); } };
 const password = async (event: React.FormEvent) => { event.preventDefault(); try { await api.changePassword(current, next, passwordFactor); setCurrent(''); setNext(''); setPasswordFactor(''); window.dispatchEvent(new Event('drivetheater-auth-expired')); } catch (err) { setError(err instanceof Error ? err.message : 'Password could not be changed.'); } };
 return <><header className="page-head"><div><p className="kicker">Projection desk</p><h1>Settings</h1><p className="muted">Cache, scheduling, and bandwidth limits keep the server responsive.</p></div></header>{message && <Notice>{message}</Notice>}{error && <Failure>{error}</Failure>}{!settings ? <p className="empty-state">Reading server settings…</p> : <div className="settings-grid"><section className="settings-card"><h2>Storage</h2><dl className="storage-readout"><div><dt>Cache in use</dt><dd>{formatBytes(settings.cache_used_bytes)}</dd></div><div><dt>Free disk space</dt><dd>{formatBytes(settings.disk_free_bytes)}</dd></div><div><dt>Worker</dt><dd>{settings.worker_online ? 'Online' : 'Offline'}</dd></div><div><dt>FFmpeg</dt><dd>{settings.ffmpeg_available ? 'Available' : 'Unavailable'}</dd></div></dl><p className="muted small">Data directory: {settings.data_dir}</p><a className="button" href="#local-storage">Review storage and preview cleanup</a></section><form className="settings-card" onSubmit={save}><h2>Cache and processing</h2><label>Cache limit (GB)<input type="number" min="1" value={String(form.cache_limit_gb ?? '')} onChange={(e) => setNumber('cache_limit_gb', e.target.value)} /></label><label>Keep free (GB)<input type="number" min="0" value={String(form.min_free_gb ?? '')} onChange={(e) => setNumber('min_free_gb', e.target.value)} /></label><label>Download concurrency<input type="number" min="1" max="2" value={String(form.download_concurrency ?? '')} onChange={(e) => setNumber('download_concurrency', e.target.value)} /></label><label>Media preparation slots<input type="number" min="1" max="2" value={String(form.max_transcodes ?? '')} onChange={(e) => setNumber('max_transcodes', e.target.value)} /></label><label className="checkbox"><input type="checkbox" checked={Boolean(form.auto_cache_cleanup)} onChange={(e) => setField('auto_cache_cleanup', e.target.checked)} />Automatically clean eligible cache</label><label>Cleanup target (%)<input type="number" min="1" max="100" value={String(form.cache_cleanup_target_percent ?? '')} onChange={(e) => setNumber('cache_cleanup_target_percent', e.target.value)} /></label><ActionButton className="button button-primary">Save settings</ActionButton></form><form className="settings-card" onSubmit={save}><h2>Download schedule</h2><label className="checkbox"><input type="checkbox" checked={Boolean(form.download_schedule_enabled)} onChange={(e) => setField('download_schedule_enabled', e.target.checked)} />Use a daily download window</label><label>Start hour (local, 0–23)<input type="number" min="0" max="23" disabled={!form.download_schedule_enabled} value={String(form.download_start_hour ?? '')} onChange={(e) => setNumber('download_start_hour', e.target.value)} /></label><label>End hour (local, 0–23)<input type="number" min="0" max="23" disabled={!form.download_schedule_enabled} value={String(form.download_end_hour ?? '')} onChange={(e) => setNumber('download_end_hour', e.target.value)} /></label><label>Bandwidth limit (Mbps)<input type="number" min="0" step="0.1" value={String(form.download_limit_mbps ?? '')} onChange={(e) => setNumber('download_limit_mbps', e.target.value)} /></label><p className="muted small">Use 0 for no bandwidth limit. The schedule delays newly requested downloads outside its window.</p><label>Activity retention (days)<input type="number" min="1" value={String(form.activity_retention_days ?? '')} onChange={(e) => setNumber('activity_retention_days', e.target.value)} /></label><ActionButton className="button button-primary">Save settings</ActionButton></form><form className="settings-card" onSubmit={password}><h2>Change password</h2><label>Current password<input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></label><label>New password<input type="password" autoComplete="new-password" minLength={10} value={next} onChange={(e) => setNext(e.target.value)} required /></label><label>Unused MFA or recovery code, if enabled<input autoComplete="one-time-code" value={passwordFactor} onChange={(e) => setPasswordFactor(e.target.value)} maxLength={64} /></label><ActionButton className="button">Change password</ActionButton></form></div>}<section id="local-storage"><StorageTools /></section><SecurityPanel /><PwaInstall /></>;
}

export default function App() {
  const [username, setUsername] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [view, setView] = useState<View>(() => new URLSearchParams(location.search).get('view') as View || 'library');
  const [player, setPlayer] = useState<Video | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [eventsState, setEventsState] = useState<'connecting' | 'connected' | 'reconnecting'>('connecting');

  useEffect(() => {
    const controller = new AbortController();
    api.me(controller.signal)
      .then((result) => setUsername(result.username))
      .catch((err) => { if (!(err instanceof ApiError && err.status === 401)) console.warn(err); })
      .finally(() => setChecking(false));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!username) {
      setEventsState('connecting');
      return;
    }
    setEventsState('connecting');
    const events = new EventSource('/api/events');
    events.onopen = () => setEventsState('connected');
    events.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as { type?: string };
        if (event.type === 'auth-expired') {
          setPlayer(null);
          setUsername(null);
          return;
        }
        if (event.type === 'invalidate') setRefreshKey((value) => value + 1);
      } catch {
        // Ignore malformed event payloads and continue polling as the fallback.
      }
    };
    events.onerror = () => setEventsState('reconnecting');
    return () => events.close();
  }, [username]);

  useEffect(() => { const expired = () => { setPlayer(null); setUsername(null); }; window.addEventListener('drivetheater-auth-expired', expired); return () => window.removeEventListener('drivetheater-auth-expired', expired); }, []);
  const login = async (name: string, password: string) => { const result = await api.login(name, password); if (result.username) setUsername(result.username); return result; };
  const mfaLogin = async (challenge: string, code?: string, recovery?: string) => { const result = await api.mfaLogin(challenge, code, recovery); setUsername(result.username); };
  if (checking) return <main className="loading-screen">Opening DriveTheater…</main>;
  if (!username) return <Login onLogin={login} onMfa={mfaLogin} />;
  const content = player ? <Player key={player.id} video={player} onClose={() => setPlayer(null)} onNext={setPlayer} /> : view === 'library' ? <LibraryPage onPlayer={setPlayer} refreshKey={refreshKey} /> : view === 'discover' ? <Discover onPlayer={setPlayer} /> : view === 'sources' ? <SourcesPage refreshKey={refreshKey} /> : view === 'downloads' ? <DownloadsPage refreshKey={refreshKey} /> : view === 'notifications' ? <Notifications refreshKey={refreshKey} /> : view === 'server' ? <ServerPage refreshKey={refreshKey} /> : <SettingsPage refreshKey={refreshKey} />;
  return <div className="app-shell"><aside className="sidebar"><div className="wordmark"><Film size={21} />DriveTheater</div><nav>{nav.map(({ id, label, icon: Icon }) => <ActionButton key={id} className={view === id && !player ? 'nav-current' : ''} aria-current={view === id && !player ? 'page' : undefined} action={`nav-${id}`} onClick={() => { setPlayer(null); setView(id); history.replaceState(null, '', `?view=${id}`); }}><Icon size={18} />{label}</ActionButton>)}</nav><div className="side-footer"><span>{username}</span><ActionButton onClick={() => void api.logout().finally(() => { clearPwaShellCache(); setPlayer(null); setUsername(null); })}><LogOut size={17} />Sign out</ActionButton></div></aside><main className="main-panel"><div className="workspace-bar"><span className="workspace-connection"><i className={`status-dot ${eventsState === 'connected' ? 'status-online' : ''}`} />{eventsState === 'connected' ? 'Live server connection' : 'Reconnecting · polling active'}</span><span className="workspace-policy">Original video. No quality conversion.</span></div>{content}</main></div>;
}
