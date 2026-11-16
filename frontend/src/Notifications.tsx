import { useEffect, useId, useState, type FormEvent } from 'react';
import { ActionButton } from './ActionButton';

export type NotificationKind = 'telegram' | 'discord' | 'smtp';
export type NotificationItem = {
  id: string; kind: string; title: string; message: string; entity_id: string | null;
  created_at: number; read_at: number | null; read: boolean;
};
export type NotificationChannel = { kind: NotificationKind; configured: boolean; enabled: boolean };
type Inbox = { items: NotificationItem[]; unread_count: number; retention_days: number };
const labels: Record<NotificationKind, string> = { telegram: 'Telegram', discord: 'Discord', smtp: 'Email (SMTP)' };
const emptyChannels: NotificationChannel[] = (['telegram', 'discord', 'smtp'] as const).map(kind => ({ kind, configured: false, enabled: false }));

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...options, credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } });
  if (!response.ok) {
    // Do not reflect provider/request details, including tokens, into the UI.
    throw new Error(response.status === 401 ? 'Sign in again to manage notifications.' : response.status === 429 ? 'Wait a minute before trying again.' : 'The notification request failed. Review the fields and verification, then try again.');
  }
  return response.json() as Promise<T>;
}

export function NotificationInbox({ items, busy, onRead, onDismiss }: {
  items: NotificationItem[]; busy: boolean; onRead: (id: string) => void; onDismiss: (id: string) => void;
}) {
  if (items.length === 0) return <p className="muted">No notifications yet. Source failures, recoveries and account events will appear here.</p>;
  return <ul className="alerts-list" aria-label="Notification inbox">{items.map(item => <li key={item.id} className={item.read ? 'alert-info' : undefined}>
    <div className="section-heading"><strong>{!item.read && <span className="small">Unread: </span>}{item.title}</strong><time className="muted small" dateTime={new Date(item.created_at * 1000).toISOString()}>{new Date(item.created_at * 1000).toLocaleString()}</time></div>
    <p className="muted">{item.message}</p>
    <div className="row-actions">{!item.read && <ActionButton type="button" className="button button-small" disabled={busy} hint="Mark this notification as read for your account." onClick={() => onRead(item.id)}>Mark read</ActionButton>}<ActionButton type="button" className="button button-small" disabled={busy} hint="Hide this notification from your inbox." onClick={() => onDismiss(item.id)}>Dismiss</ActionButton></div>
  </li>)}</ul>;
}

/** Inbox and explicitly enabled channels. Mounting only performs authenticated GETs. */
export function Notifications({ refreshKey = 0 }: { refreshKey?: number }) {
  const prefix = useId();
  const [inbox, setInbox] = useState<Inbox>({ items: [], unread_count: 0, retention_days: 90 });
  const [channels, setChannels] = useState<NotificationChannel[]>(emptyChannels);
  const [kind, setKind] = useState<NotificationKind>('telegram');
  const [fields, setFields] = useState<Record<string, string>>({});
  const [enabled, setEnabled] = useState(false);
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [recovery, setRecovery] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const selected = channels.find(channel => channel.kind === kind)!;
  const reload = async (signal?: AbortSignal) => {
    const [nextInbox, nextChannels] = await Promise.all([
      request<Inbox>('/api/notifications', { signal }), request<{ items: NotificationChannel[] }>('/api/notifications/channels', { signal }),
    ]);
    if (!signal?.aborted) { setInbox(nextInbox); setChannels(nextChannels.items); }
  };
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void reload(controller.signal).then(() => { if (!controller.signal.aborted) setError(''); }).catch(() => {
      if (!controller.signal.aborted) setError('Notifications could not be loaded. Refresh to try again.');
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [refreshKey]);
  const clearProof = () => { setPassword(''); setCode(''); setRecovery(''); };
  const choose = (next: NotificationKind) => { setKind(next); setFields({}); setEnabled(false); clearProof(); setNotice(''); setError(''); };
  const update = (field: string, value: string) => setFields(old => ({ ...old, [field]: value }));
  const mutateInbox = async (identity: string, action: 'read' | 'dismiss') => {
    setBusy(true); setError('');
    try { await request(`/api/notifications/${encodeURIComponent(identity)}/${action}`, { method: 'POST' }); await reload(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'The inbox could not be updated.'); }
    finally { setBusy(false); }
  };
  const channelAction = async (action: 'save' | 'enabled' | 'delete' | 'test', nextEnabled?: boolean) => {
    if (!password || busy) return;
    setBusy(true); setError(''); setNotice('');
    const body: Record<string, unknown> = { password, code: code || null, recovery_code: recovery || null };
    if (action === 'save') {
      const config: Record<string, string | number> = {};
      for (const [field, value] of Object.entries(fields)) if (value !== '') config[field] = field === 'port' ? Number(value) : value;
      body.config = config; body.enabled = enabled;
    }
    if (action === 'enabled') body.enabled = nextEnabled;
    const suffix = action === 'save' ? '' : `/${action}`;
    try {
      await request(`/api/notifications/channels/${kind}${suffix}`, { method: 'POST', body: JSON.stringify(body) });
      setFields({});
      setNotice(action === 'test' ? 'Test queued for this channel. Delivery is handled by the worker and may be retried.' : action === 'delete' ? 'Channel deleted.' : action === 'enabled' ? `Channel ${nextEnabled ? 'enabled' : 'disabled'}.` : `Channel saved ${enabled ? 'and enabled' : 'with external delivery disabled'}.`);
      await reload();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Notification settings could not be updated.'); }
    finally {
      clearProof();
      // Secret inputs never enter localStorage/sessionStorage or survive an attempt.
      setFields(old => Object.fromEntries(Object.entries(old).filter(([field]) => !['bot_token', 'webhook_url', 'password'].includes(field))));
      setBusy(false);
    }
  };
  const save = (event: FormEvent) => { event.preventDefault(); void channelAction('save'); };
  const field = (name: string, label: string, secret = false, maximum = 512) => <label htmlFor={`${prefix}-${name}`}>{label}<input id={`${prefix}-${name}`} type={secret ? 'password' : 'text'} value={fields[name] || ''} onChange={event => update(name, event.target.value)} autoComplete="off" spellCheck={false} maxLength={maximum} placeholder={selected.configured ? (secret ? 'Saved secret hidden; leave blank to keep' : 'Leave blank to keep the saved value') : undefined} /></label>;
  return <section aria-label="Notifications">
    <div className="section-heading"><div><h2>Notifications</h2><p className="muted small">Your inbox stays on this server. External delivery is off until you enable a channel.</p></div><ActionButton type="button" className="button button-small" disabled={busy || loading} onClick={() => { setLoading(true); void reload().then(() => setError('')).catch(() => setError('Notifications could not be loaded.')).finally(() => setLoading(false)); }}>Refresh notifications</ActionButton></div>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="notice" role="status">{notice}</p>}
    <section className="server-card" aria-label="Inbox" aria-busy={loading}>
      <div className="section-heading"><h3>Inbox</h3><span className="muted small">{inbox.unread_count} unread · Kept for up to {inbox.retention_days} days</span></div>
      {loading ? <p role="status" className="muted">Loading notifications…</p> : <NotificationInbox items={inbox.items} busy={busy} onRead={identity => void mutateInbox(identity, 'read')} onDismiss={identity => void mutateInbox(identity, 'dismiss')} />}
    </section>
    <details className="source-form activity-card"><summary>External notification channels</summary>
      <form onSubmit={save} autoComplete="off">
        <p className="muted small">Saved credentials are encrypted and never shown again. Saving does not send a test. Disabling cancels queued messages; an already in-flight delivery cannot be recalled.</p>
        <label htmlFor={`${prefix}-channel`}>Channel<select id={`${prefix}-channel`} value={kind} disabled={busy} onChange={event => choose(event.target.value as NotificationKind)}>{channels.map(channel => <option key={channel.kind} value={channel.kind}>{labels[channel.kind]} — {channel.configured ? (channel.enabled ? 'enabled' : 'saved, disabled') : 'not configured'}</option>)}</select></label>
        <fieldset disabled={busy}><legend>{labels[kind]} settings</legend>
          {kind === 'telegram' && <div className="field-grid">{field('bot_token', 'Bot token', true, 145)}{field('chat_id', 'Numeric chat ID', false, 21)}</div>}
          {kind === 'discord' && <>{field('webhook_url', 'Discord webhook URL', true, 350)}<p className="muted small">Only https://discord.com/api/webhooks/ URLs are accepted. Mentions are disabled.</p></>}
          {kind === 'smtp' && <><div className="field-grid">{field('host', 'Public SMTP hostname', false, 253)}<label htmlFor={`${prefix}-tls`}>Connection security<select id={`${prefix}-tls`} value={fields.tls_mode || ''} onChange={event => { update('tls_mode', event.target.value); update('port', event.target.value === 'implicit' ? '465' : event.target.value === 'starttls' ? '587' : ''); }}><option value="">{selected.configured ? 'Keep saved TLS settings' : 'Choose verified TLS'}</option><option value="implicit">TLS on port 465</option><option value="starttls">Required STARTTLS on port 587</option></select></label>{field('username', 'SMTP username', true, 254)}{field('password', 'SMTP password', true)}{field('from_address', 'From email address', false, 254)}{field('to_address', 'Recipient email address', false, 254)}</div><p className="muted small">A public hostname and valid server certificate are required. Local/private mail servers and plaintext delivery are not supported.</p></>}
        </fieldset>
        <label className="checkbox"><input type="checkbox" checked={enabled} disabled={busy} onChange={event => setEnabled(event.target.checked)} />Enable external delivery when saved</label>
        <fieldset disabled={busy}><legend>Verify channel changes</legend><p className="muted small">Enter your current password. If MFA is enabled, also use a fresh authenticator code or one recovery code.</p><label htmlFor={`${prefix}-proof`}>Current password<input id={`${prefix}-proof`} type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete="current-password" maxLength={1024} required /></label><div className="field-grid"><label htmlFor={`${prefix}-code`}>Authenticator code<input id={`${prefix}-code`} value={code} onChange={event => setCode(event.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={8} /></label><label htmlFor={`${prefix}-recovery`}>Or one recovery code<input id={`${prefix}-recovery`} type="password" value={recovery} onChange={event => setRecovery(event.target.value)} autoComplete="off" maxLength={64} /></label></div></fieldset>
        <div className="row-actions"><ActionButton type="submit" className="button button-primary" disabled={busy || loading || !password} hint="Save encrypted settings. Delivery is enabled only if the switch is selected.">Save channel</ActionButton>{selected.configured && <><ActionButton type="button" className="button" disabled={busy || loading || !password} onClick={() => void channelAction('enabled', !selected.enabled)}>{selected.enabled ? 'Disable channel' : 'Enable channel'}</ActionButton><ActionButton type="button" className="button" disabled={busy || loading || !password || !selected.enabled} hint="Queue one test for this enabled channel; do not send it in this request." onClick={() => void channelAction('test')}>Queue test</ActionButton><ActionButton type="button" className="button" disabled={busy || loading || !password} onClick={() => void channelAction('delete')}>Delete channel</ActionButton></>}</div>
      </form>
    </details>
  </section>;
}
