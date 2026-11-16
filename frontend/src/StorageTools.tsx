import { useEffect, useState } from 'react';
import { request } from './api';
import { ActionButton } from './ActionButton';

type Sizes = { logical_bytes: number; physical_bytes: number; reclaimable_bytes: number; shared_inode_count: number; file_count: number };
type StorageItem = Sizes & { video_id: string; title: string; source_id: string | null; artifacts: Record<string, number>; active: boolean };
type Inventory = { totals: Sizes; artifacts: Record<string, Sizes>; items: StorageItem[]; note: string; physical_measurement: string };
type Policy = { scope_kind: 'source' | 'title'; scope_id: string; keep_days: number | null; delete_after_watch: boolean | number };
type Plan = Sizes & { plan_id: string; expires_at: number; items: { video_id: string; title: string }[] };
type Monitor = Record<string, number | boolean | string | null>;
const formatBytes = (value: number) => value < 1024 ? `${value} B` : `${(value / 1024 ** (value >= 1024 ** 3 ? 3 : value >= 1024 ** 2 ? 2 : 1)).toFixed(1)} ${value >= 1024 ** 3 ? 'GiB' : value >= 1024 ** 2 ? 'MiB' : 'KiB'}`;
const endpoint = (path: string, body: unknown, method = 'POST') => request(path, { method, body: JSON.stringify(body) });

/** Mount in storage settings, or pass videoId for one title's tools. */
export function StorageTools({ videoId }: { videoId?: string } = {}) {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [monitor, setMonitor] = useState<Monitor | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [scope, setScope] = useState<'source' | 'title'>('title');
  const [target, setTarget] = useState(videoId ?? '');
  const [keepDays, setKeepDays] = useState('off');
  const [afterWatch, setAfterWatch] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [integrity, setIntegrity] = useState<Record<string, string>>({});

  const refresh = async (signal?: AbortSignal) => {
    const [details, policyResult, facts] = await Promise.all([
      request<Inventory>('/api/storage/details', {}, signal),
      request<{ items: Policy[] }>('/api/storage/policies', {}, signal),
      request<Monitor>('/api/diagnostics/monitor', {}, signal),
    ]);
    if (signal?.aborted) return;
    setInventory(details); setPolicies(policyResult.items); setMonitor(facts);
    setTarget(current => current || details.items[0]?.video_id || '');
  };
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Storage details could not be loaded.'); });
    return () => controller.abort();
  }, []);
  useEffect(() => { setTarget(videoId ?? ''); setSelected([]); setPlan(null); setConfirmed(false); }, [videoId]);
  useEffect(() => {
    const policy = policies.find(item => item.scope_kind === scope && item.scope_id === target);
    setKeepDays(policy?.keep_days ? String(policy.keep_days) : 'off');
    setAfterWatch(Boolean(policy?.delete_after_watch));
  }, [policies, scope, target]);

  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError(''); setMessage('');
    try { await operation(); } catch (reason) { setError(reason instanceof Error ? reason.message : 'The action failed. Refresh and try again.'); }
    finally { setBusy(false); }
  };
  const rows = inventory?.items.filter(item => !videoId || item.video_id === videoId) ?? [];
  const targets = scope === 'title' ? rows.map(item => ({ id: item.video_id, label: item.title })) : Array.from(new Map(rows.filter(item => item.source_id).map(item => [item.source_id!, { id: item.source_id!, label: `Source containing ${item.title}` }])).values());
  const validTarget = targets.some(item => item.id === target);
  const setSelection = (id: string, checked: boolean) => { setSelected(current => checked ? [...current, id] : current.filter(value => value !== id)); setPlan(null); setConfirmed(false); };

  return <section className="panel storage-tools" aria-label="Storage tools" aria-busy={busy}>
    <h2>Local storage</h2>
    <p>Keep the cloud library. Manage only the copies and playback files stored on this server.</p>
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <ActionButton disabled={busy} onClick={() => void run(() => refresh())} hint="Read current local file sizes without calculating checksums.">Refresh storage</ActionButton>
    {!inventory && !error && <p role="status">Reading local storage…</p>}
    {inventory && <>
      <div style={{ overflowX: 'auto' }}><table>
        <caption>Artifact sizes on this server</caption>
        <thead><tr><th scope="col">Artifact</th><th scope="col">Logical size</th><th scope="col">Unique physical size</th><th scope="col">Files</th></tr></thead>
        <tbody>{Object.entries(inventory.artifacts).map(([kind, sizes]) => <tr key={kind}><th scope="row">{kind}</th><td>{formatBytes(sizes.logical_bytes)}</td><td>{formatBytes(sizes.physical_bytes)}</td><td>{sizes.file_count}</td></tr>)}</tbody>
        <tfoot><tr><th scope="row">All local artifacts</th><td>{formatBytes(inventory.totals.logical_bytes)}</td><td>{formatBytes(inventory.totals.physical_bytes)}</td><td>{inventory.totals.file_count}</td></tr></tfoot>
      </table></div>
      <p>{inventory.note} {inventory.physical_measurement === 'file_size_fallback' && 'This runtime does not expose allocated blocks; physical size uses unique file lengths.'}</p>
      <div style={{ overflowX: 'auto' }}><table>
        <caption>Choose cached titles to preview cleanup</caption>
        <thead><tr><th scope="col">Select</th><th scope="col">Title</th><th scope="col">Original</th><th scope="col">Playback files</th><th scope="col">Previews</th><th scope="col">Captions</th><th scope="col">Integrity</th></tr></thead>
        <tbody>{rows.map(item => <tr key={item.video_id}>
          <td><input type="checkbox" aria-label={`Select ${item.title} for cleanup`} disabled={busy || item.active} checked={selected.includes(item.video_id)} onChange={event => setSelection(item.video_id, event.target.checked)} /></td>
          <th scope="row">{item.title}{item.active && ' (in use)'}</th>
          {['original', 'derived', 'previews', 'captions'].map(kind => <td key={kind}>{formatBytes(item.artifacts[kind] ?? 0)}</td>)}
          <td><ActionButton disabled={busy || item.active} onClick={() => void run(async () => {
            const result = await request<{ status: string }>(`/api/videos/${encodeURIComponent(item.video_id)}/integrity`, { method: 'POST' });
            setIntegrity(current => ({ ...current, [item.video_id]: result.status }));
          })} aria-label={`Verify integrity of ${item.title}`} hint="Explicitly calculate SHA256 for this local original. This can take time; local checks are not provider verification.">Verify SHA256</ActionButton>{integrity[item.video_id] && <p role="status">{integrity[item.video_id].replaceAll('_', ' ')}</p>}</td>
        </tr>)}</tbody>
      </table></div>
      {rows.length === 0 && <p>No completed local files to manage. Download a title to get started.</p>}
      <ActionButton disabled={busy || selected.length === 0} onClick={() => void run(async () => {
        const preview = await request<Plan>('/api/storage/cleanup/preview', { method: 'POST', body: JSON.stringify({ video_ids: selected }) });
        setPlan(preview); setConfirmed(false);
      })} hint="Create an expiring snapshot; no files are removed yet.">Preview cleanup</ActionButton>
      {plan && <section aria-label="Confirm local cleanup">
        <h3>Remove these local copies?</h3>
        <ul>{plan.items.map(item => <li key={item.video_id}>{item.title}</li>)}</ul>
        <p>{formatBytes(plan.logical_bytes)} of local files; up to {formatBytes(plan.reclaimable_bytes)} reclaimable. Shared links may retain bytes. Cloud originals are not changed. Preview expires at {new Date(plan.expires_at * 1000).toLocaleTimeString()}.</p>
        <label><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} /> I confirm removal of only these local copies.</label>
        <ActionButton disabled={busy || !confirmed} onClick={() => void run(async () => {
          const current = plan; setPlan(null); setConfirmed(false);
          const result = await request<{ removed: number; freed_bytes: number }>(`/api/storage/cleanup/${encodeURIComponent(current.plan_id)}/confirm`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
          setSelected([]); await refresh(); setMessage(`Removed ${result.removed} local copies; ${formatBytes(result.freed_bytes)} reclaimed.`);
        })} hint="Recheck the snapshot, active playback and jobs, then remove only the reserved local artifacts.">Remove local copies</ActionButton>
        <ActionButton disabled={busy} onClick={() => { setPlan(null); setConfirmed(false); }}>Cancel cleanup</ActionButton>
      </section>}
    </>}
    <form onSubmit={event => { event.preventDefault(); void run(async () => {
      await endpoint(`/api/storage/policies/${scope}/${encodeURIComponent(target)}`, { keep_days: keepDays === 'off' ? null : Number(keepDays), delete_after_watch: afterWatch }, 'PUT');
      await refresh(); setMessage('Retention policy saved. The worker applies enabled policies during periodic maintenance; saving does not immediately remove files.');
    }); }}>
      <h3>Retention</h3>
      <p>New retention policies are off by default. Once enabled, the worker applies them periodically without another cleanup confirmation. Title policies override source policies. Delete after watch requires every recorded viewer to have marked the title watched.</p>
      <label>Apply to <select disabled={busy} value={scope} onChange={event => { setScope(event.target.value as 'source' | 'title'); setTarget(''); }}><option value="title">Title</option><option value="source">Source</option></select></label>
      <label>Policy target <select disabled={busy} value={validTarget ? target : ''} onChange={event => setTarget(event.target.value)}><option value="">Choose a {scope}</option>{targets.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <label>Keep after last use <select disabled={busy} value={keepDays} onChange={event => setKeepDays(event.target.value)}><option value="off">Off</option>{[7, 30, 90].map(days => <option key={days} value={days}>{days} days</option>)}</select></label>
      <label><input type="checkbox" disabled={busy} checked={afterWatch} onChange={event => setAfterWatch(event.target.checked)} /> Delete after watch</label>
      <ActionButton type="submit" disabled={busy || !validTarget} hint="Save this explicit policy. Saving does not remove any local files.">Save retention policy</ActionButton>
    </form>
    <details><summary>Local diagnostics</summary>
      <p>These are observations from the visible runtime, not a host inventory. Host process and container totals remain unknown.</p>
      {monitor && <table><caption>Runtime facts</caption><tbody>{[
        ['Visible processes', 'visible_process_count'], ['Host processes', 'host_process_count'], ['Containers', 'container_count'], ['Worker online', 'worker_alive'], ['Active sessions', 'active_sessions'], ['Running jobs', 'running_jobs'], ['Backup records', 'backup_count'],
      ].map(([label, key]) => <tr key={key}><th scope="row">{label}</th><td>{monitor[key] === null || monitor[key] === undefined ? 'Unknown' : typeof monitor[key] === 'boolean' ? monitor[key] ? 'Yes' : 'No' : String(monitor[key])}</td></tr>)}</tbody></table>}
      <ActionButton disabled={busy} onClick={() => void run(async () => {
        const report = await request<unknown>('/api/diagnostics/support-export');
        const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
        const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'drivetheater-support.json'; anchor.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        setMessage('Exported allowlisted runtime facts and timings without titles, private paths, URLs, credentials or raw logs.');
      })} hint="Download an allowlisted report without private titles, paths, URLs or credentials.">Export support diagnostics</ActionButton>
    </details>
  </section>;
}
