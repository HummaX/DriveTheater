import { useEffect, useState } from 'react';
import { request } from './api';

type Probe = { duration_seconds: number | null; video: Array<{ codec: string; width: number | null; height: number | null }>; audio: Array<{ codec: string; channels: number | null }> };
type Stage = { stage: string; state: string; details: { elapsed_seconds?: number } };

export function VideoDiagnostics({ videoId }: { videoId: string }) {
  const [facts, setFacts] = useState<Probe | null>(null);
  const [error, setError] = useState('');
  const [capabilities, setCapabilities] = useState<string[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    void request<{ facts: Probe | null }>(`/api/videos/${encodeURIComponent(videoId)}/diagnostics`, {}, controller.signal).then(result => setFacts(result.facts)).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Probe diagnostics are unavailable.'); });
    return () => controller.abort();
  }, [videoId]);
  useEffect(() => {
    if (!facts || typeof document === 'undefined') return;
    const element = document.createElement('video');
    const declarations: Record<string, string> = { h264: 'video/mp4; codecs="avc1.42E01E"', hevc: 'video/mp4; codecs="hvc1"', av1: 'video/mp4; codecs="av01.0.08M.08"', vp9: 'video/webm; codecs="vp9"', vp8: 'video/webm; codecs="vp8"' };
    setCapabilities(facts.video.map(stream => { const declaration = declarations[stream.codec]; if (!declaration) return `${stream.codec}: no generic declaration is available.`; const answer = element.canPlayType(declaration); return `${stream.codec}: ${answer || 'no support reported'} for generic declaration ${declaration}.`; }));
  }, [facts]);
  return <section className="video-diagnostics" aria-label="Codec and probe diagnostics"><h3>Original stream facts</h3>{error && <p role="alert">{error}</p>}{facts ? <><ul>{facts.video.map((stream, index) => <li key={index}>Video: {stream.codec}, {stream.width ?? 'unknown'} × {stream.height ?? 'unknown'}</li>)}{facts.audio.map((stream, index) => <li key={index}>Audio: {stream.codec}, {stream.channels ?? 'unknown'} channels</li>)}</ul><h4>This browser's best-effort report</h4><ul>{capabilities.map(value => <li key={value}>{value}</li>)}</ul><p className="muted small">This checks generic codec declarations, not this file's exact profile, container, audio, device or browser bugs. Only actual playback can confirm compatibility. There is no video conversion; use Download to device and a compatible player if needed.</p></> : !error && <p>No stored probe yet. A probe is recorded during an explicitly requested media job; opening this panel does not start one.</p>}</section>;
}

export function JobStages({ jobId, refreshKey }: { jobId: string; refreshKey?: string }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Stage[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void request<{ items: Stage[] }>(`/api/jobs/${encodeURIComponent(jobId)}/stages`, {}, controller.signal).then(result => { setItems(result.items); setError(''); }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Stage timings are unavailable.'); });
    return () => controller.abort();
  }, [jobId, refreshKey, open]);
  return <details onToggle={event => setOpen(event.currentTarget.open)}><summary>Observed job stages</summary>{error && <p role="alert">{error}</p>}{items.length ? <table><thead><tr><th>Stage</th><th>State</th><th>Observed elapsed</th></tr></thead><tbody>{items.map((item, index) => <tr key={index}><td>{item.stage.replaceAll('_', ' ')}</td><td>{item.state}</td><td>{item.details.elapsed_seconds === undefined ? 'Still running / unavailable' : `${item.details.elapsed_seconds.toFixed(1)} seconds`}</td></tr>)}</tbody></table> : <p>No stages were recorded for this older job.</p>}</details>;
}
