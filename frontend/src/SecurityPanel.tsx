import { useCallback, useEffect, useState } from 'react';
import { ActionButton } from './ActionButton';
import { request } from './api';

type NetworkPolicy = { allowlist_enabled: boolean; allowed_cidrs: string[]; trusted_proxy_cidrs: string[]; proxy_gate_enabled: boolean };
type SecurityStatus = { mfa_enabled: boolean; recovery_codes_remaining: number; crypto_available: boolean; network_policy: NetworkPolicy };
type Session = { id: string; device_label: string; created_at: number; last_seen_at: number; current: boolean };
type SecurityEvent = { occurred_at: number; event_type: string };

export function SecurityPanel() {
  const [status, setStatus] = useState<SecurityStatus | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [events, setEvents] = useState<SecurityEvent[]>([]);
  const [password, setPassword] = useState('');
  const [factor, setFactor] = useState('');
  const [enrollment, setEnrollment] = useState<{ secret: string; provisioning_uri: string } | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [network, setNetwork] = useState<NetworkPolicy | null>(null);
  const [preview, setPreview] = useState<{ confirmation: string; current_network_permitted: boolean } | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const [security, devices, history] = await Promise.all([
        request<SecurityStatus>('/api/security'), request<{ items: Session[] }>('/api/security/sessions'),
        request<{ items: SecurityEvent[] }>('/api/security/events')
      ]);
      setStatus(security); setNetwork((current) => current ?? security.network_policy);
      setSessions(devices.items); setEvents(history.items);
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not load security settings.'); }
  }, []);
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 15000); return () => window.clearInterval(timer); }, [load]);
  const proof = () => ({ password, ...(!factor ? {} : /^\d{6}$/.test(factor) ? { code: factor } : { recovery_code: factor }) });
  const post = <T,>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) });
  const action = async (operation: () => Promise<void>) => {
    setBusy(true); setError(''); setMessage('');
    try { await operation(); await load(); }
    catch (err) { setError(err instanceof Error ? err.message : 'Security action failed.'); }
    finally { setBusy(false); setPassword(''); setFactor(''); }
  };
  const signOut = () => window.dispatchEvent(new Event('drivetheater-auth-expired'));
  const updateNetwork = (patch: Partial<NetworkPolicy>) => { if (network) setNetwork({ ...network, ...patch }); setPreview(null); };
  return <section className="security-section" aria-label="Administrator security">
    <header className="section-heading"><div><h2>Administrator security</h2><p className="muted">Optional protection, with explicit recovery. Nothing is enabled automatically.</p></div></header>
    {error && <p className="form-error" role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <div className="settings-grid">
      <section className="settings-card"><h3>Verify a sensitive action</h3>
        <p className="muted small">Enter your current password for each action. If MFA is enabled, also enter an unused authenticator or recovery code. These fields are cleared after each action and never saved in browser storage.</p>
        <label>Current password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <label>Authenticator or recovery code<input autoComplete="one-time-code" value={factor} onChange={(event) => setFactor(event.target.value)} maxLength={64} /></label>
      </section>
      <section className="settings-card"><h3>Two-step verification</h3>
        <p>{status ? status.mfa_enabled ? 'Enabled' : 'Disabled' : 'Reading status…'}</p>
        {status?.mfa_enabled && <p>{status.recovery_codes_remaining} unused recovery codes remain.</p>}
        {!status?.mfa_enabled && !enrollment && <ActionButton className="button" disabled={busy || !password || !status?.crypto_available} onClick={() => void action(async () => setEnrollment(await post('/api/security/mfa/begin', proof())))}>Set up authenticator</ActionButton>}
        {enrollment && <div className="secret-reveal"><p>Add this key to an authenticator, then enter its code and your current password to confirm. Setup expires after ten minutes.</p><code>{enrollment.secret}</code><details><summary>Provisioning URI</summary><code className="break-secret">{enrollment.provisioning_uri}</code></details>
          <ActionButton className="button button-primary" disabled={busy || !password || !factor} onClick={() => void action(async () => { const result = await post<{ recovery_codes: string[] }>('/api/security/mfa/confirm', proof()); setEnrollment(null); setRecoveryCodes(result.recovery_codes); setMessage('MFA enabled; other sessions were revoked. Save the recovery codes now.'); })}>Confirm authenticator</ActionButton>
          <ActionButton className="button" onClick={() => setEnrollment(null)}>Hide setup key</ActionButton>
        </div>}
        {status?.mfa_enabled && <div className="action-row">
          <ActionButton className="button" disabled={busy || !password || !factor} onClick={() => void action(async () => { const result = await post<{ recovery_codes: string[] }>('/api/security/mfa/recovery-codes', proof()); setRecoveryCodes(result.recovery_codes); })}>Replace recovery codes</ActionButton>
          <ActionButton className="button" disabled={busy || !password || !factor} onClick={() => void action(async () => { await post('/api/security/mfa/disable', proof()); signOut(); })}>Disable MFA and sign out</ActionButton>
        </div>}
        {recoveryCodes.length > 0 && <div className="secret-reveal"><h4>Save these codes privately</h4><p>Each works once. They cannot be retrieved again after you hide or leave this page. Replacing codes invalidates all previous ones.</p><pre>{recoveryCodes.join('\n')}</pre><ActionButton className="button" onClick={() => setRecoveryCodes([])}>I saved them; hide codes</ActionButton></div>}
      </section>
      <section className="settings-card"><h3>Signed-in devices</h3><p className="muted small">Labels are best-effort browser descriptions, not verified device identities. Raw addresses and session tokens are not displayed.</p>
        <ul className="plain-list">{sessions.map((session) => <li key={session.id}><strong>{session.device_label || 'Existing session'}{session.current ? ' · This session' : ''}</strong><span className="muted small">Last seen {session.last_seen_at ? new Date(session.last_seen_at * 1000).toLocaleString() : 'before this update'}</span><ActionButton className="button button-small" disabled={busy || !password} onClick={() => void action(async () => { const result = await post<{ signed_out: boolean }>('/api/security/sessions/revoke', { ...proof(), session_id: session.id }); if (result.signed_out) signOut(); })}>Revoke session</ActionButton></li>)}</ul>
        <ActionButton className="button" disabled={busy || !password} onClick={() => void action(async () => { await post('/api/security/sessions/revoke', { ...proof(), session_id: 'others' }); setMessage('Other sessions revoked.'); })}>Sign out other devices</ActionButton>
        <ActionButton className="button" disabled={busy || !password} onClick={() => void action(async () => { await post('/api/security/sessions/revoke', { ...proof(), session_id: 'all' }); signOut(); })}>Sign out everywhere</ActionButton>
      </section>
      {network && <section className="settings-card"><h3>Network access</h3><p className="muted small">Advanced: use a trusted proxy only when it strips incoming identity/forwarded headers. Proxy identity never replaces the app password or MFA. Run Uvicorn with proxy-header rewriting disabled.</p>
        <label className="checkbox"><input type="checkbox" checked={network.allowlist_enabled} onChange={(event) => updateNetwork({ allowlist_enabled: event.target.checked })} />Restrict client networks</label>
        <label>Allowed IPv4/IPv6 CIDRs, comma separated<textarea value={network.allowed_cidrs.join(', ')} onChange={(event) => updateNetwork({ allowed_cidrs: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) })} /></label>
        <label>Trusted proxy CIDRs, comma separated<textarea value={network.trusted_proxy_cidrs.join(', ')} onChange={(event) => updateNetwork({ trusted_proxy_cidrs: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) })} /></label>
        <label className="checkbox"><input type="checkbox" checked={network.proxy_gate_enabled} onChange={(event) => updateNetwork({ proxy_gate_enabled: event.target.checked })} />Require trusted proxy identity in addition to app login</label>
        <ActionButton className="button" disabled={busy} onClick={() => void action(async () => { setPreview(await post('/api/security/network/preview', { policy: network })); })}>Preview access policy</ActionButton>
        {preview && <><p role="status">{preview.current_network_permitted ? 'Your current request is permitted by this preview. Confirm within five minutes.' : 'This policy would block this request and cannot be applied from here.'}</p><ActionButton className="button button-primary" disabled={busy || !password || !preview.current_network_permitted} onClick={() => void action(async () => { await post('/api/security/network/apply', { ...proof(), confirmation: preview.confirmation }); setPreview(null); setMessage('Network policy applied. Keep local server recovery available.'); })}>Confirm access policy</ActionButton></>}
        <p className="muted small">Local server administrators can recover access with the documented root-only recovery command; there is no public password bypass.</p>
      </section>}
      <section className="settings-card"><h3>Security events</h3><p className="muted small">Retained separately for 90 days. Clearing watch/activity history does not erase these records.</p><ul className="plain-list">{events.slice(0, 30).map((event, index) => <li key={`${event.occurred_at}-${index}`}><span>{event.event_type.replaceAll('_', ' ')}</span><small>{new Date(event.occurred_at * 1000).toLocaleString()}</small></li>)}</ul></section>
    </div>
  </section>;
}
