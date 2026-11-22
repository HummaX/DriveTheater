import Hls from 'hls.js';
import { Pause, Play, Volume2, Maximize, Settings2, Upload, X, MonitorUp, Search } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { SpriteCue, SubtitleTrack, Video } from './types';
import { api } from './api';
import { ActionButton } from './ActionButton';
import { DeviceDownload } from './DeviceDownload';
import { cueAt, formatTime, parseSpriteVtt } from './utils';

type AudioChoice = { index: number; label: string };
type NativeAudioTrack = { enabled: boolean; label: string; language: string };
type NativeAudioTracks = { length: number; [index: number]: NativeAudioTrack };
type CaptionCue = { start: number; end: number; text: string };

function isEnglishSubtitle(track: SubtitleTrack) {
  const language = track.language.trim().toLowerCase();
  return language === 'en' || language === 'eng' || language === 'english' || language.startsWith('en-') || /^english(?:\b|[-_])/i.test(track.label.trim());
}

function parseCaptionVtt(source: string): CaptionCue[] {
  const blocks = source.replace(/\r/g, '').split(/\n\n+/);
  return blocks.flatMap((block) => {
    const lines = block.split('\n').filter(Boolean);
    const timingIndex = lines.findIndex((line) => line.includes('-->'));
    if (timingIndex < 0) return [];
    const [from, to] = lines[timingIndex].split('-->').map((part) => part.trim().split(/\s+/)[0]);
    const time = (value: string) => { const pieces = value.replace(',', '.').split(':').map(Number); return pieces.length === 3 ? pieces[0] * 3600 + pieces[1] * 60 + pieces[2] : pieces[0] * 60 + pieces[1]; };
    const start = time(from); const end = time(to);
    const text = lines.slice(timingIndex + 1).join(' ').replace(/<[^>]+>/g, '').trim();
    return Number.isFinite(start) && Number.isFinite(end) && text ? [{ start, end, text }] : [];
  });
}

export function Player({ video: initialVideo, onClose, onNext }: { video: Video; onClose: () => void; onNext?: (video: Video) => void }) {
  const [video, setVideo] = useState(initialVideo);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(initialVideo.watch_position ?? 0);
  const [bufferedUntil, setBufferedUntil] = useState(0);
  const [duration, setDuration] = useState(initialVideo.playback.duration ?? initialVideo.duration ?? 0);
  const [volume, setVolume] = useState(.85);
  const [rate, setRate] = useState(1);
  const [buffering, setBuffering] = useState(false);
  const [playerError, setPlayerError] = useState('');
  const [subtitleBusy, setSubtitleBusy] = useState(false);
  const [cues, setCues] = useState<SpriteCue[]>([]);
  const [hover, setHover] = useState<{ at: number; x: number } | null>(null);
  const [audioChoices, setAudioChoices] = useState<AudioChoice[]>([]);
  const [selectedAudio, setSelectedAudio] = useState('');
  const [selectedSubtitle, setSelectedSubtitle] = useState('off');
  const [captionCues, setCaptionCues] = useState<CaptionCue[]>([]);
  const [captionQuery, setCaptionQuery] = useState('');
  const [subtitleOffset, setSubtitleOffset] = useState(0);
  const [tvMode, setTvMode] = useState(false);
  const containerRef = useRef<HTMLElement | null>(null);
  const element = useRef<HTMLVideoElement | null>(null);
  const hls = useRef<Hls | null>(null);
  const sourceKey = useRef<string | null>(null);
  const autoplayIntent = useRef(true);
  const volumeRef = useRef(.85);
  const retryTimer = useRef<number | null>(null);
  const originalCueTimes = useRef(new Map<TextTrackCue, { start: number; end: number }>());
  const terminalFailure = useRef(false);
  const recoveryAttempts = useRef({ network: 0, media: 0, native: 0 });
  const [nextEpisode, setNextEpisode] = useState<Video | null>(null);
  const offerNext = async () => {
    try {
      const [result, preferences] = await Promise.all([api.nextEpisode(video.id), api.preferences()]);
      const next = result.item;
      if (!next || !next.playback.url || !['cached', 'playable'].includes(next.state)) return;
      if (preferences.autoplay_next && onNext) onNext(next); else setNextEpisode(next);
    } catch { /* No next episode is safer than admitting an implicit download. */ }
  };

  const reportPlayFailure = useCallback((error: unknown) => setPlayerError(error instanceof Error ? error.message : 'Playback could not start yet.'), []);
  const loadNativeAudioTracks = useCallback((el: HTMLVideoElement) => {
    const tracks = (el as HTMLVideoElement & { audioTracks?: NativeAudioTracks }).audioTracks;
    if (!tracks || tracks.length < 2) return false;
    setAudioChoices(Array.from({ length: tracks.length }, (_, index) => ({ index, label: tracks[index].label || tracks[index].language || `Audio ${index + 1}` })));
    return true;
  }, []);

  const attach = useCallback((next: Video) => {
    const el = element.current; const url = next.playback.url;
    if (!el || !url) return;
    const nextKey = `${next.playback.mode}:${url}`;
    if (sourceKey.current === nextKey || terminalFailure.current) return;
    const initialLoad = sourceKey.current === null;
    const snapshot = { time: el.currentTime, paused: el.paused, rate: el.playbackRate || rate };
    const shouldPlay = initialLoad ? autoplayIntent.current : !snapshot.paused;
    hls.current?.destroy(); hls.current = null; setAudioChoices([]); setSelectedAudio(''); setPlayerError(''); setBuffering(shouldPlay); el.pause(); el.volume = volumeRef.current; el.playbackRate = snapshot.rate; sourceKey.current = nextKey;
    const restore = () => { const resumePosition = initialLoad ? next.watch_position ?? 0 : snapshot.time; if (Number.isFinite(resumePosition) && resumePosition > 0) el.currentTime = resumePosition; el.playbackRate = snapshot.rate; if (shouldPlay) void el.play().catch(reportPlayFailure); };
    el.addEventListener('loadedmetadata', restore, { once: true });
    if (next.playback.mode === 'hls' && Hls.isSupported()) {
      const instance = new Hls({ enableWorker: true });
      instance.on(Hls.Events.AUDIO_TRACKS_UPDATED, (_, data) => setAudioChoices(data.audioTracks.map((track, index) => ({ index, label: track.name || track.lang || `Audio ${index + 1}` }))));
      instance.on(Hls.Events.ERROR, (_, data) => {
        if (!data.fatal) return;
        setBuffering(false);
        const unsupported = [Hls.ErrorDetails.MANIFEST_INCOMPATIBLE_CODECS_ERROR, Hls.ErrorDetails.BUFFER_ADD_CODEC_ERROR, Hls.ErrorDetails.BUFFER_INCOMPATIBLE_CODECS_ERROR].includes(data.details);
        if (!unsupported && data.type === Hls.ErrorTypes.NETWORK_ERROR && recoveryAttempts.current.network++ < 3) { instance.startLoad(); setPlayerError('Connection interrupted. Retrying playback…'); return; }
        if (!unsupported && data.type === Hls.ErrorTypes.MEDIA_ERROR && recoveryAttempts.current.media++ < 1) { instance.recoverMediaError(); setPlayerError('Trying one media recovery…'); return; }
        terminalFailure.current = true; instance.stopLoad();
        setPlayerError(unsupported ? 'This browser cannot play the original video codec. Download the original or use a compatible player; video conversion is disabled.' : 'Playback stopped after bounded recovery attempts. Download the original or reopen the player to try again.');
      });
      instance.loadSource(url); instance.attachMedia(el); hls.current = instance;
    } else { el.removeAttribute('src'); el.src = url; el.load(); if (!loadNativeAudioTracks(el) && next.audio_tracks.length > 1) setPlayerError('Audio selection is not supported by this browser for this stream.'); }
  }, [loadNativeAudioTracks, rate, reportPlayFailure]);

  useEffect(() => { attach(video); }, [attach, video.playback.mode, video.playback.url]);
  useEffect(() => () => { hls.current?.destroy(); if (retryTimer.current !== null) window.clearTimeout(retryTimer.current); }, []);
  useEffect(() => { if (!video.sprites_vtt_url) { setCues([]); return; } const controller = new AbortController(); fetch(video.sprites_vtt_url, { credentials: 'same-origin', signal: controller.signal }).then((response) => response.ok ? response.text() : Promise.reject()).then((text) => setCues(parseSpriteVtt(text).map((cue) => ({ ...cue, image: new URL(cue.image, new URL(video.sprites_vtt_url!, window.location.href)).toString() })))).catch(() => setCues([])); return () => controller.abort(); }, [video.sprites_vtt_url]);
  useEffect(() => { const controller = new AbortController(); const refresh = () => api.video(video.id, controller.signal).then((next) => { setVideo(next); attach(next); }).catch(() => undefined); const timer = window.setInterval(refresh, 2000); return () => { controller.abort(); window.clearInterval(timer); }; }, [attach, video.id]);
  useEffect(() => { const announce = (active: boolean) => { const el = element.current; void api.activity(video.id, { active, position: el && Number.isFinite(el.currentTime) ? el.currentTime : undefined, duration: el && Number.isFinite(el.duration) ? el.duration : undefined }).catch(() => undefined); }; announce(true); const timer = window.setInterval(() => announce(true), 30000); return () => { window.clearInterval(timer); announce(false); }; }, [video.id]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (!tvMode || event.defaultPrevented || (event.target instanceof HTMLElement && (event.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(event.target.tagName)))) return;
      const el = element.current; if (!el) return;
      if (event.key === ' ' || event.key === 'Spacebar') { event.preventDefault(); if (el.paused) void el.play().catch(reportPlayFailure); else el.pause(); }
      if (event.key === 'ArrowLeft') { event.preventDefault(); el.currentTime = Math.max(0, el.currentTime - 10); }
      if (event.key === 'ArrowRight') { event.preventDefault(); el.currentTime = Math.min(el.duration || Infinity, el.currentTime + 10); }
      if (event.key === 'ArrowUp') { event.preventDefault(); el.volume = Math.min(1, el.volume + .05); setVolume(el.volume); }
      if (event.key === 'ArrowDown') { event.preventDefault(); el.volume = Math.max(0, el.volume - .05); setVolume(el.volume); }
    };
    window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler);
  }, [tvMode, reportPlayFailure]);

  const toggle = () => {
    const el = element.current;
    if (!el || !video.playback.url) return;
    if (terminalFailure.current) {
      terminalFailure.current = false;
      recoveryAttempts.current = { network: 0, media: 0, native: 0 };
      sourceKey.current = null;
      attach(video);
      return;
    }
    if (el.paused) {
      autoplayIntent.current = true;
      void el.play().catch(reportPlayFailure);
    } else {
      autoplayIntent.current = false;
      el.pause();
    }
  };
  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(reportPlayFailure);
    } else {
      const target = containerRef.current || element.current;
      if (target?.requestFullscreen) {
        void target.requestFullscreen().catch(reportPlayFailure);
      }
    }
  }, [reportPlayFailure]);
  const seek = (time: number) => { if (element.current && Number.isFinite(time)) element.current.currentTime = Math.max(0, time); };
  const chooseAudio = (value: string) => { const index = Number(value); const el = element.current; setSelectedAudio(value); if (hls.current) { hls.current.audioTrack = index; return; } const tracks = el && (el as HTMLVideoElement & { audioTracks?: NativeAudioTracks }).audioTracks; if (tracks) for (let position = 0; position < tracks.length; position += 1) tracks[position].enabled = position === index; };
  const resetTextTrackTimes = () => { originalCueTimes.current.forEach((original, cue) => { try { cue.startTime = original.start; cue.endTime = original.end; } catch { /* browser-owned cue */ } }); originalCueTimes.current.clear(); };
  const chooseSubtitle = async (value: string) => {
    resetTextTrackTimes(); setSubtitleOffset(0); setCaptionQuery(''); setSelectedSubtitle(value); setCaptionCues([]);
    const tracks = element.current?.textTracks; if (!tracks) return;
    for (let index = 0; index < tracks.length; index += 1) tracks[index].mode = value === String(index) ? 'showing' : 'disabled';
    if (value === 'off') return;
    const track = englishSubtitleTracks[Number(value)];
    if (!track) return;
    try { const response = await fetch(track.url, { credentials: 'same-origin' }); if (!response.ok) throw new Error(); setCaptionCues(parseCaptionVtt(await response.text())); } catch { setPlayerError('English subtitle text could not be searched.'); }
  };
  const applySubtitleOffset = (next: number) => {
    const tracks = element.current?.textTracks; const selected = selectedSubtitle === 'off' ? null : tracks?.[Number(selectedSubtitle)];
    if (!selected?.cues) return;
    for (const cue of Array.from(selected.cues)) { const original = originalCueTimes.current.get(cue) ?? { start: cue.startTime, end: cue.endTime }; originalCueTimes.current.set(cue, original); try { cue.startTime = Math.max(0, original.start + next); cue.endTime = Math.max(0, original.end + next); } catch { /* unsupported browser cue mutation */ } }
    setSubtitleOffset(next);
  };
  const preview = hover && cueAt(cues, hover.at); const knownDuration = Number.isFinite(duration) && duration > 0 ? duration : (video.playback.duration ?? video.duration ?? 0);
  const updatePosition = (el: HTMLVideoElement) => {
    setCurrentTime(el.currentTime);
    let until = 0;
    const tolerance = 1.0;
    for (let index = 0; index < el.buffered.length; index += 1) {
      if (el.buffered.start(index) <= el.currentTime + tolerance && el.buffered.end(index) >= el.currentTime - tolerance) {
        until = Math.max(until, el.buffered.end(index));
      }
    }
    setBufferedUntil(until);
  };
  const uploadSubtitle = async (file: File | undefined) => { if (!file) return; if (!/\.(srt|vtt)$/i.test(file.name)) { setPlayerError('Choose a WebVTT (.vtt) or SubRip (.srt) subtitle file.'); return; } setSubtitleBusy(true); setPlayerError(''); try { setVideo(await api.uploadSubtitle(video.id, file)); } catch (error) { setPlayerError(error instanceof Error ? error.message : 'Subtitle upload failed.'); } finally { setSubtitleBusy(false); } };
  const captionStage = subtitleBusy ? 'Uploading captions…' : video.caption_status ? `Captions: ${video.caption_status}` : null;
  const englishSubtitleTracks = video.subtitle_tracks.filter(isEnglishSubtitle);
  const filteredCaptions = captionCues.filter((cue) => cue.text.toLowerCase().includes(captionQuery.trim().toLowerCase()));
  return <section ref={containerRef} className={`player-view ${tvMode ? 'tv-mode' : ''}`} aria-label={`Player: ${video.title}`}><div className="player-top"><div><p className="kicker">Now screening</p><h2>{video.title}</h2></div><ActionButton className="icon-button light" onClick={onClose} aria-label="Close player"><X /></ActionButton></div><div className="theater"><video playsInline onEnded={() => void offerNext()} ref={(node) => { element.current = node; if (node) node.volume = .85; }} poster={video.poster_url ?? undefined} onPlay={(event) => { autoplayIntent.current = true; setPlaying(true); setPlayerError(''); const el = event.currentTarget; if (Number.isFinite(el.duration) && el.duration > 0) void api.activity(video.id, { active: true, position: el.currentTime, duration: el.duration }).catch(() => undefined); }} onPause={() => setPlaying(false)} onWaiting={() => setBuffering(true)} onCanPlay={(event) => { setBuffering(false); updatePosition(event.currentTarget); if (retryTimer.current !== null) { window.clearTimeout(retryTimer.current); retryTimer.current = null; } }} onPlaying={() => { setBuffering(false); setPlayerError(''); }} onLoadedData={(event) => updatePosition(event.currentTarget)} onProgress={(event) => updatePosition(event.currentTarget)} onError={() => { setBuffering(false); if (terminalFailure.current) return; const code = element.current?.error?.code; if (code === 3 || code === 4 || recoveryAttempts.current.native++ >= 2) { terminalFailure.current = true; hls.current?.stopLoad(); setPlayerError('This original stream could not be decoded by this browser. Download the original or use a compatible player; automatic retries have stopped.'); return; } setPlayerError('The cache connection was interrupted. Trying a bounded recovery…'); if (retryTimer.current !== null) window.clearTimeout(retryTimer.current); retryTimer.current = window.setTimeout(() => { sourceKey.current = null; attach(video); }, 5000); }} onTimeUpdate={(event) => updatePosition(event.currentTarget)} onLoadedMetadata={(event) => { const nextDuration = event.currentTarget.duration; setDuration(Number.isFinite(nextDuration) && nextDuration > 0 ? nextDuration : (video.playback.duration ?? video.duration ?? 0)); loadNativeAudioTracks(event.currentTarget); updatePosition(event.currentTarget); }}>{englishSubtitleTracks.map((track) => <track key={track.id} kind="subtitles" src={track.url} label={track.label} srcLang={track.language} />)}</video>{buffering && <div className="buffering" role="status">Preparing next frames</div>}{(!video.playback.url || playerError) && <div className="player-unavailable"><strong>{playerError || (video.state === 'failed' ? 'This item needs attention.' : 'Preparing this screening.')}</strong><span>{video.playback.url ? (terminalFailure.current ? 'No video conversion is performed. Try the complete original in a compatible player.' : 'A bounded recovery is attempted only for transient connection errors.') : 'Playback will begin when enough media is available.'}</span></div>}<ActionButton className="video-toggle" onClick={toggle} aria-label={playing ? 'Pause' : 'Play'} disabled={!video.playback.url}>{playing ? <Pause fill="currentColor" /> : <Play fill="currentColor" />}</ActionButton></div><div className="control-deck"><div className="scrub-wrap" onMouseMove={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setHover({ at: Math.max(0, Math.min(knownDuration, ((event.clientX - rect.left) / rect.width) * knownDuration)), x: event.clientX - rect.left }); }} onMouseLeave={() => setHover(null)}>{hover && <div className="sprite-preview" style={{ left: hover.x }}>{preview && <div style={{ backgroundImage: `url(${preview.image})`, backgroundPosition: `-${preview.x}px -${preview.y}px`, width: preview.width, height: preview.height }} />}<span>{formatTime(hover.at)}</span></div>}<div className="buffered-strip" role="progressbar" aria-label="Buffered media" aria-valuemin={0} aria-valuemax={knownDuration || 0} aria-valuenow={Math.min(bufferedUntil, knownDuration || bufferedUntil)} aria-valuetext={`Loaded until ${formatTime(bufferedUntil)}`}><i style={{ width: `${knownDuration ? Math.min(100, (bufferedUntil / knownDuration) * 100) : 0}%` }} /></div><input aria-label="Seek" type="range" min="0" max={knownDuration || 0} step=".1" value={Math.min(currentTime, knownDuration || currentTime)} disabled={!knownDuration} onChange={(event) => seek(Number(event.target.value))} /></div><div className="controls"><DeviceDownload video={video} /><ActionButton className="icon-button" onClick={toggle} disabled={!video.playback.url} aria-label={playing ? 'Pause' : 'Play'}>{playing ? <Pause fill="currentColor" /> : <Play fill="currentColor" />}</ActionButton><span className="time">{formatTime(currentTime)} / {formatTime(knownDuration)}</span><label className="volume"><Volume2 size={18} /><input aria-label="Volume" type="range" min="0" max="1" step=".05" value={volume} onChange={(event) => { const next = Number(event.target.value); volumeRef.current = next; setVolume(next); if (element.current) element.current.volume = next; }} /></label><label className="compact-select"><Settings2 size={16} /><select aria-label="Playback speed" value={rate} onChange={(event) => { const next = Number(event.target.value); setRate(next); if (element.current) element.current.playbackRate = next; }}><option value=".75">0.75×</option><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label>{audioChoices.length > 1 && <label className="compact-select"><select aria-label="Audio language" value={selectedAudio} onChange={(event) => chooseAudio(event.target.value)}><option value="" disabled>Audio</option>{audioChoices.map((track) => <option key={track.index} value={track.index}>{track.label}</option>)}</select></label>}{englishSubtitleTracks.length > 0 && <label className="compact-select"><select aria-label="English subtitles" value={selectedSubtitle} onChange={(event) => void chooseSubtitle(event.target.value)}><option value="off">Subtitles off</option>{englishSubtitleTracks.map((track, index) => <option key={track.id} value={index}>{track.label || track.language}</option>)}</select></label>}{captionStage && <span className="caption-status" role="status">{captionStage}</span>}<label className="subtitle-upload button button-small"><Upload size={15} />{subtitleBusy ? 'Uploading…' : 'Add captions'}<input aria-label="Upload captions" type="file" accept=".srt,.vtt,text/vtt,application/x-subrip" disabled={subtitleBusy} onChange={(event) => { void uploadSubtitle(event.currentTarget.files?.[0]); event.currentTarget.value = ''; }} /></label><ActionButton className="icon-button" onClick={() => setTvMode((value) => !value)} aria-label={tvMode ? 'Exit TV mode' : 'Enable TV mode'} aria-pressed={tvMode}><MonitorUp /></ActionButton><ActionButton className="icon-button" onClick={toggleFullscreen} aria-label="Fullscreen"><Maximize /></ActionButton></div>{selectedSubtitle !== 'off' && <div className="subtitle-tools"><div className="subtitle-offset"><span>Subtitle timing: {subtitleOffset > 0 ? '+' : ''}{subtitleOffset.toFixed(1)}s</span><ActionButton className="button button-small" type="button" onClick={() => applySubtitleOffset(Math.max(-10, subtitleOffset - .5))}>−0.5s</ActionButton><ActionButton className="button button-small" type="button" onClick={() => applySubtitleOffset(Math.min(10, subtitleOffset + .5))}>+0.5s</ActionButton><ActionButton className="button button-small" type="button" onClick={() => { resetTextTrackTimes(); setSubtitleOffset(0); }}>Reset</ActionButton></div>{captionCues.length > 0 && <><label className="subtitle-search"><Search size={16} /><input value={captionQuery} onChange={(event) => setCaptionQuery(event.target.value)} placeholder="Search English subtitles" /></label><div className="caption-cue-list" aria-label="Matching English subtitle cues">{filteredCaptions.slice(0, 100).map((cue, index) => <ActionButton hint={`Seek to ${formatTime(cue.start)} in “${video.title}”: ${cue.text}`} action="seek-caption" type="button" key={`${cue.start}-${index}`} onClick={() => seek(cue.start)}><time>{formatTime(cue.start)}</time><span>{cue.text}</span></ActionButton>)}</div></>}</div>}{nextEpisode && <section className="next-episode" aria-label="Next episode"><h3>Next: {nextEpisode.title}</h3><p>Already playable on this server. No download will be queued.</p><ActionButton className="button button-primary" onClick={() => onNext?.(nextEpisode)}>Play next episode</ActionButton><ActionButton className="button" onClick={() => setNextEpisode(null)}>Not now</ActionButton></section>}<p className="player-track-note">Audio choices are English, Hindi, or an explicitly labeled original track; captions are English only.</p>{tvMode && <p className="player-track-note" role="status">TV mode: Space plays or pauses; Left/Right seek 10 seconds; Up/Down adjust volume. Controls in forms keep their usual keys.</p>}</div></section>;
}
