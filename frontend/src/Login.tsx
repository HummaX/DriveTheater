import { FormEvent, useState } from 'react';
import { Film } from 'lucide-react';
import { ActionButton } from './ActionButton';

export function Login({ onLogin }: { onLogin: (username: string, password: string) => Promise<void> }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try { await onLogin(username, password); } catch (err) { setError(err instanceof Error ? err.message : 'Sign in failed.'); } finally { setBusy(false); }
  };
  return <main className="login-shell"><section className="login-aside"><div className="wordmark"><Film size={22} /> DriveTheater</div><div><p className="kicker">A private projection desk</p><h1>Keep the room<br />for the picture.</h1><p>Connect your own library, prepare the films you choose, and watch without losing your place.</p></div></section><section className="login-panel"><form onSubmit={submit} className="login-form"><p className="kicker">Sign in</p><h2>Welcome back</h2><p className="muted">Use the administrator account configured for this installation.</p><p className="muted small">Local activity is recorded on this server.</p><label>Username<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></label><label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>{error && <p className="form-error" role="alert">{error}</p>}<ActionButton className="button button-primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</ActionButton></form></section></main>;
}
