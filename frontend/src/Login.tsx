import { FormEvent, useState } from 'react';
import { Film } from 'lucide-react';
import { ActionButton } from './ActionButton';
import type { AuthLoginResult } from './api';

export function Login({ onLogin, onMfa }: {
  onLogin: (username: string, password: string) => Promise<AuthLoginResult>;
  onMfa: (challenge: string, code?: string, recovery?: string) => Promise<void>;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [challenge, setChallenge] = useState('');
  const [factor, setFactor] = useState('');
  const [recovery, setRecovery] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try {
      if (challenge) await onMfa(challenge, recovery ? undefined : factor, recovery ? factor : undefined);
      else {
        const result = await onLogin(username, password);
        if (result.mfa_required && result.challenge) { setChallenge(result.challenge); setPassword(''); }
      }
    } catch (err) { setError(err instanceof Error ? err.message : 'Sign in failed.'); }
    finally { setBusy(false); setFactor(''); }
  };
  return <main className="login-shell"><section className="login-aside">
    <div className="wordmark"><Film size={22} /> DriveTheater</div>
    <div><p className="kicker">A private projection desk</p><h1>Keep the room<br />for the picture.</h1><p>Connect your own library, prepare the films you choose, and watch without losing your place.</p></div>
  </section><section className="login-panel"><form onSubmit={submit} className="login-form">
    <h2>{challenge ? 'Verify your sign-in' : 'Welcome back'}</h2>
    <p className="muted">{challenge ? 'Enter an unused authenticator code or one-time recovery code. No login session is created until verification succeeds.' : 'Use the administrator account configured for this installation.'}</p>
    {challenge ? <>
      <label>{recovery ? 'Recovery code' : 'Authenticator code'}<input autoFocus inputMode={recovery ? 'text' : 'numeric'} autoComplete="one-time-code" value={factor} onChange={(e) => setFactor(e.target.value)} maxLength={64} required /></label>
      <label className="checkbox"><input type="checkbox" checked={recovery} onChange={(e) => { setRecovery(e.target.checked); setFactor(''); }} />Use a recovery code</label>
      <ActionButton type="button" className="button" onClick={() => { setChallenge(''); setFactor(''); }}>Start again</ActionButton>
    </> : <>
      <label>Username<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></label>
      <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
    </>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <ActionButton className="button button-primary" disabled={busy}>{busy ? 'Verifying…' : challenge ? 'Verify and sign in' : 'Sign in'}</ActionButton>
    <p className="muted small">Local activity is recorded on this server. Recovery requires your saved code or local server administrator access.</p>
  </form></section></main>;
}
