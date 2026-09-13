import { useEffect, useState } from 'react';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { api, getSession, setSession } from '../api';

export default function Login() {
  const nav = useNavigate();
  const { divisionSlug: urlSlug } = useParams();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [mfaToken, setMfaToken] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [division, setDivision] = useState(null);

  useEffect(() => {
    if (urlSlug) {
      api(`/api/v1/auth/login-context/${encodeURIComponent(urlSlug)}`)
        .then((d) => setDivision(d.division))
        .catch(() => setError('Division not found. Check the link or contact your administrator.'));
    }
  }, [urlSlug]);

  const finish = async (d) => {
    setSession({
      kind: 'tenant',
      access: d.access_token,
      refresh: d.refresh_token,
      user: d.user,
      division: d.division,
    });
    const me = await api('/api/v1/auth/me');
    const s = getSession();
    setSession({
      ...s,
      user: { ...d.user, ...me.user, role: d.user.role,
              onboarding: d.onboarding || me.user?.onboarding || undefined },
      hierarchy_level: me.hierarchy_level,
      permissions: me.permissions || [],
    });
    const step = d.onboarding || me.user?.onboarding;
    nav(step ? `/app/onboarding/${step}` : '/app', { replace: true });
  };

  const submit = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      const body = { username, password };
      if (urlSlug) body.division_slug = urlSlug;
      const d = await api('/api/v1/auth/login', { method: 'POST', body });
      if (d.mfa_required) {
        setMfaToken(d.mfa_token);
        return;
      }
      if (d.password_change_required) {
        nav('/change-password', {
          state: {
            username,
            division: d.division || division,
            password_change_token: d.password_change_token,
          },
        });
        return;
      }
      await finish(d);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const submitCode = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      const d = await api('/api/v1/auth/mfa/verify', {
        method: 'POST',
        body: { username, mfa_token: mfaToken, code },
      });
      if (d.password_change_required) {
        nav('/change-password', {
          state: {
            username,
            division: d.division || division,
            password_change_token: d.password_change_token,
          },
        });
        return;
      }
      await finish(d);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const logoUrl = division ? `/api/v1/auth/division-logo/${division.id}` : null;

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={mfaToken ? submitCode : submit}>
        <div className="auth-brand">
          {logoUrl
            ? <img src={logoUrl} alt={division.name} className="auth-logo"
                onError={(e) => { e.currentTarget.style.display = 'none'; }} />
            : <>
                <span className="brand-mark lg">{division ? (division.name || 'D').charAt(0) : 'C'}</span>
                <h1>{division ? division.name : 'CampaignOS'}</h1>
              </>}
          <p className="muted">Sign in with your username &amp; password</p>
        </div>
        {error && <div className="error-box">{error}</div>}
        {mfaToken ? (
          <>
            <p className="muted">Two-factor authentication is enabled for <strong>{username}</strong>. Enter the 6-digit code from your authenticator app.</p>
            <label className="field">
              <span className="field-label">Authentication code</span>
              <input className="input" value={code} onChange={(e) => setCode(e.target.value)}
                placeholder="000000" maxLength={6} autoFocus required />
            </label>
            <button className="btn btn-primary btn-block" disabled={busy}>
              {busy ? 'Verifying…' : 'Verify & sign in'}
            </button>
            <button type="button" className="btn-link btn-block" onClick={() => setMfaToken(null)}>
              Back
            </button>
          </>
        ) : (
          <>
            <label className="field">
              <span className="field-label">Username</span>
              <input className="input" value={username}
                onChange={(e) => setUsername(e.target.value)} required />
            </label>
            <label className="field">
              <span className="field-label">Password</span>
              <input className="input" type="password" value={password}
                onChange={(e) => setPassword(e.target.value)} required />
            </label>
            <button className="btn btn-primary btn-block" disabled={busy}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          </>
        )}
        <div className="auth-links">
          <Link to="/forgot-password">Forgot username or password?</Link>
        </div>
        <div className="auth-links">
          <Link to="/superadmin-login">Platform administrator sign in</Link>
        </div>
      </form>
    </div>
  );
}