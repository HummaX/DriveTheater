import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Download, Film, Play, Search, Settings2, Star, X } from 'lucide-react';
import { ActionButton } from './ActionButton';
import { api, request, type DiscoverMovie, type TorrentioStream, type TorrentioEpisodeResult } from './api';
import type { Video } from './types';

export function Discover({ onPlayer }: { onPlayer: (video: Video) => void }) {
  const [configured, setConfigured] = useState(false);
  const [configReady, setConfigReady] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const [manifest, setManifest] = useState('');
  const [metadataManifest, setMetadataManifest] = useState('');
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState('');
  const [mediaType, setMediaType] = useState<'movie' | 'series'>('movie');
  const [episodes, setEpisodes] = useState<TorrentioEpisodeResult['episodes']>([]);
  const [selectedEpisode, setSelectedEpisode] = useState<TorrentioEpisodeResult['episodes'][number] | null>(null);
  const catalogToken = useRef(0);
  const activeQuery = useRef('');
  const [items, setItems] = useState<DiscoverMovie[]>([]);
  const [nextSkip, setNextSkip] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<DiscoverMovie | null>(null);
  const [streams, setStreams] = useState<TorrentioStream[]>([]);
  const [streamsBusy, setStreamsBusy] = useState(false);
  const [streamError, setStreamError] = useState('');
  const [adding, setAdding] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const lookupToken = useRef(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const catalogInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let mounted = true;
    api.discoveryConfig().then((value) => { if (mounted) setConfigured(value.torrentio_configured); })
      .catch(() => { if (mounted) setError('Could not read add-on connection status.'); })
      .finally(() => { if (mounted) setConfigReady(true); });
    return () => { mounted = false; lookupToken.current += 1; };
  }, []);
  useEffect(() => {
    if (selected && dialog.current && !dialog.current.open) dialog.current.showModal();
    if (!selected && dialog.current?.open) dialog.current.close();
  }, [selected]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setError('');
    try {
      const values = await api.saveDiscoveryConfig({ torrentio_manifest_url: manifest.trim() || undefined, metadata_manifest_url: metadataManifest.trim() || undefined });
      setConfigured(values.torrentio_configured); setManifest(''); setMetadataManifest(''); setConfigOpen(false);
      setMessage('Add-on connection saved privately on this server.');
    } catch (err) { setError(err instanceof Error ? err.message : 'Connection could not be saved.'); }
    finally { setSaving(false); }
  };
  const load = async (more = false) => {
    const catalogRequest = ++catalogToken.current;
    setLoading(true); setError('');
    const term = more ? activeQuery.current : query.trim();
    try {
      const result = await api.discoverMovies({ query: term, skip: more ? nextSkip : 0, media_type: mediaType });
      if (catalogRequest !== catalogToken.current) return;
      const tagged = result.items.map(item => ({ ...item, media_type: mediaType }));
      const unique = more ? tagged.filter((item) => !items.some((current) => current.id === item.id)) : tagged;
      setItems((current) => more ? [...current, ...unique] : unique);
      setNextSkip((more ? nextSkip : 0) + result.items.length);
      setHasMore(result.items.length >= 20 && unique.length > 0);
      activeQuery.current = term; setLoaded(true);
    } catch (err) { if (catalogRequest === catalogToken.current) setError(err instanceof Error ? err.message : 'Catalog could not be loaded.'); }
    finally { if (catalogRequest === catalogToken.current) setLoading(false); }
  };
  const openMovie = async (movie: DiscoverMovie) => {
    const token = ++lookupToken.current;
    setSelected(movie); setStreams([]); setEpisodes([]); setSelectedEpisode(null); setStreamError('');
    if (movie.media_type === 'series') {
      setStreamsBusy(true);
      try { const result = await api.torrentioEpisodes({ media_id: movie.id }); if (token === lookupToken.current) setEpisodes(result.episodes); }
      catch (err) { if (token === lookupToken.current) setStreamError(err instanceof Error ? err.message : 'Episodes could not be loaded.'); }
      finally { if (token === lookupToken.current) setStreamsBusy(false); }
      return;
    }
    if (!configured) { setStreamsBusy(false); return; }
    setStreamsBusy(true);
    try {
      const result = await api.torrentioStreams({ media_type: 'movie', media_id: movie.id });
      if (token === lookupToken.current) setStreams(result.items);
    } catch (err) { if (token === lookupToken.current) setStreamError(err instanceof Error ? err.message : 'Streams could not be loaded.'); }
    finally { if (token === lookupToken.current) setStreamsBusy(false); }
  };
  const chooseEpisode = async (episode: TorrentioEpisodeResult['episodes'][number]) => {
    const token = ++lookupToken.current;
    setSelectedEpisode(episode); setStreams([]); setStreamError(''); setStreamsBusy(true);
    try { const result = await api.torrentioStreams({ media_type: 'series', media_id: episode.id }); if (token === lookupToken.current) setStreams(result.items); }
    catch (err) { if (token === lookupToken.current) setStreamError(err instanceof Error ? err.message : 'Episode streams could not be loaded.'); }
    finally { if (token === lookupToken.current) setStreamsBusy(false); }
  };
  const closeMovie = () => { if (adding) return; lookupToken.current += 1; setSelected(null); };
  const choose = async (stream: TorrentioStream, play: boolean) => {
    if (!selected || adding) return;
    setAdding(stream.magnet_url); setStreamError('');
    try {
      if (selected.media_type === 'series' && !selectedEpisode) throw new Error('Choose an episode first.');
      const title = selectedEpisode ? `${selected.title} S${String(selectedEpisode.season).padStart(2, '0')}E${String(selectedEpisode.episode).padStart(2, '0')} ${selectedEpisode.title}` : selected.title;
      const added = await api.addUrl({ url: stream.magnet_url, title, engine: 'auto' });
      if (selectedEpisode) await request(`/api/library/videos/${encodeURIComponent(added.id)}/override`, { method: 'POST', body: JSON.stringify({ type: 'series', series: selected.title, season: selectedEpisode.season, episode: selectedEpisode.episode }) });
      const result = play ? await api.play(added.id) : await api.download(added.id);
      setMessage(`${selected.title} was ${added.reused ? 'reused from your library and ' : ''}queued. Follow live progress in Downloads.`);
      lookupToken.current += 1; setSelected(null);
      if (play) onPlayer(result.video);
    } catch (err) { setStreamError(err instanceof Error ? err.message : 'The selected stream could not be queued.'); }
    finally { setAdding(null); }
  };

  return <>
    <header className="page-head discover-head"><div><p className="kicker">Find your next screening</p><h1>Discover</h1><p className="muted">Browse movie or series metadata, then choose an authorized Torrentio stream. Original video stays unchanged.</p></div><ActionButton className="button" onClick={() => setConfigOpen((value) => !value)} aria-expanded={configOpen} hint="Configure a compatible stream add-on. Private manifest URLs stay on your server."><Settings2 size={17} />Add-on connection</ActionButton></header>
    <div className="discovery-status"><span className={`status-dot ${configured ? 'status-online' : ''}`} /><span>{!configReady ? 'Reading connection…' : configured ? 'Stream add-on connected' : 'Connect a stream add-on to download'}</span><span className="metadata-note">Metadata only until you choose Play or Download</span></div>
    {configOpen && <form className="settings-card discovery-config" onSubmit={(event) => void save(event)}><h2>Private add-on connection</h2><label>Stream manifest URL<input type="password" autoComplete="off" value={manifest} onChange={(event) => setManifest(event.target.value)} placeholder={configured ? 'Saved privately; enter a URL to replace' : 'https://…/manifest.json'} required={!configured} /></label><label>Metadata manifest URL <span className="optional">optional</span><input type="password" autoComplete="off" value={metadataManifest} onChange={(event) => setMetadataManifest(event.target.value)} placeholder="Uses Cinemeta when left blank" /></label><p className="muted small">Use an add-on and content you are authorized to access. Private URLs may contain tokens; they are never returned in the connection status.</p><ActionButton className="button button-primary" disabled={saving}>{saving ? 'Saving…' : 'Save connection'}</ActionButton></form>}
    <form className="discovery-search" onSubmit={(event) => { event.preventDefault(); void load(); }}><label>Catalog<select value={mediaType} onChange={event => { catalogToken.current += 1; setMediaType(event.target.value as 'movie' | 'series'); setItems([]); setLoaded(false); setHasMore(false); setLoading(false); }}><option value="movie">Movies</option><option value="series">Series</option></select></label><label className="search"><Search size={19} /><input ref={catalogInput} aria-label="Search movies or series" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search movies by title" maxLength={120} /></label><ActionButton className="button button-primary" disabled={loading}>{loading ? 'Loading metadata…' : query.trim() ? 'Search catalog' : 'Browse catalog'}</ActionButton></form>
    {message && <p className="notice" role="status">{message}</p>}{error && <p className="form-error" role="alert">{error}</p>}
    {!loaded && !loading && <section className="discovery-intro"><Film size={38} /><h2>A movie shelf, separate from your Drive.</h2><p>Load a catalog or search by name. Opening a movie only looks up streams; you decide which file to download.</p><p className="muted small">Movie metadata is requested from the configured add-on, or Cinemeta, when you browse or search. IMDb IDs identify titles; this is not an IMDb service.</p></section>}
    {loading && <p className="muted" role="status">Loading movie metadata. No video is downloading.</p>}
    {loaded && items.length === 0 && !loading && <section className="empty-state"><Search size={30} /><h2>No movies found.</h2><p>Try another title or check the metadata add-on connection.</p></section>}
    <div className="discover-grid" aria-label="Discover movies">{items.map((movie) => <article className="discovery-card" key={movie.id}><ActionButton className="discovery-movie" action="open-movie" context={movie.id} aria-label={`View streams for ${movie.title}`} hint="Open this movie’s metadata and compatible stream list. No media download starts yet." onClick={() => void openMovie(movie)}><span className="discovery-poster">{movie.poster ? <img src={movie.poster} alt="" loading="lazy" referrerPolicy="no-referrer" /> : <span className="artwork-fallback"><Film size={38} /></span>}<span className="poster-open"><ArrowRight size={22} /><span>View streams</span></span></span><span className="discovery-copy"><strong>{movie.title}</strong><span>{movie.year || 'Year not supplied'}{movie.rating && <span className="movie-rating"><Star size={13} />{movie.rating}</span>}</span></span></ActionButton></article>)}</div>
    {hasMore && <ActionButton className="button load-more" disabled={loading} onClick={() => void load(true)}>Load more movies</ActionButton>}
    <dialog className="movie-dialog" ref={dialog} aria-labelledby="movie-dialog-title" onCancel={(event) => { event.preventDefault(); closeMovie(); }}>
      {selected && <><div className="movie-dialog-head"><div><p className="kicker">Choose the file, not a quality conversion</p><h2 id="movie-dialog-title">{selected.title}</h2><p className="muted small">{selected.year} {selected.genres.join(' / ')}</p></div><ActionButton className="icon-button" disabled={adding !== null} onClick={closeMovie} aria-label="Close movie details" hint="Return to the movie catalog without downloading anything."><X /></ActionButton></div>{selected.description && <p className="movie-description">{selected.description}</p>}
        {selected.media_type === 'series' && <section aria-label="Choose a series episode"><h3>Episodes and specials</h3><p>Opening an episode looks up sources only. Choose one source explicitly to Play or Download.</p><div className="episode-selection">{[...episodes].sort((a, b) => a.season - b.season || a.episode - b.episode).map(episode => <ActionButton className="button button-small" key={episode.id} disabled={adding !== null || !configured} aria-pressed={selectedEpisode?.id === episode.id} onClick={() => void chooseEpisode(episode)}>{episode.season === 0 ? 'Special' : `S${episode.season}`} E{episode.episode}: {episode.title}</ActionButton>)}</div></section>}
        {!configured ? <div className="empty-state"><p>Connect your stream add-on before looking up downloads.</p><ActionButton className="button button-primary" onClick={() => { closeMovie(); setConfigOpen(true); }}>Configure add-on</ActionButton></div> : <>
          <p className="muted small">Availability and browser codec support vary. No seeding after download completion. Downloading is only for content you are authorized to access.</p>
          {streamsBusy && <p className="lookup-progress" role="status">Looking up streams… No video is downloading.</p>}{streamError && <p className="form-error" role="alert">{streamError}</p>}{!streamsBusy && !streamError && streams.length === 0 && (selected.media_type !== 'series' || selectedEpisode) && <p className="empty-state">No compatible torrent streams were returned. Try another movie or check the add-on.</p>}
          <div className="discovery-streams">{streams.map((stream, index) => <article className="discovery-stream" key={stream.magnet_url}><div><span className="stream-source">{stream.name}</span><h3>{stream.title}</h3></div><div className="stream-actions"><ActionButton className="button button-small" disabled={adding !== null} onClick={() => void choose(stream, false)} hint={`Download the selected source for ${selected.title} to this server. No video conversion.`}><Download size={15} />{adding === stream.magnet_url ? 'Starting…' : 'Download to server'}</ActionButton><ActionButton className="button button-small button-primary" disabled={adding !== null} aria-label={`Play ${selected.title}, stream ${index + 1}`} onClick={() => void choose(stream, true)} hint="Explicitly start preparing the selected stream and open the player."><Play size={15} />Play</ActionButton></div></article>)}</div>
        </>}
      </>}
    </dialog>
  </>;
}
