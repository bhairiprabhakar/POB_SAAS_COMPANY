import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, setSession } from '../api';

export default function SuperAdminLogin() {
  const nav = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [brand, setBrand] = useState({ platform_name: 'CampaignOS', has_logo: false });

  useEffect(() => {
    api('/api/v1/auth/platform-branding').then(setBrand).catch(() => {});
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      const d = await api('/api/v1/auth/superadmin/login', {
        method: 'POST',
        body: { username, password },
      });
      setSession({
        kind: 'sa',
        access: d.access_token,
        refresh: d.refresh_token,
        user: d.user,
      });
      nav('/superadmin', { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">
          {brand.has_logo
            ? <img src="/api/v1/auth/platform-logo" alt={brand.platform_name} className="auth-logo" />
            : <span className="brand-mark lg">C</span>}
          {!brand.has_logo && <h1>{brand.platform_name}</h1>}
          <p>Platform console · super administrator sign in</p>
        </div>
        {error && <div className="error-box">{error}</div>}
        <label className="field">
          <span className="field-label">Username</span>
          <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </label>
        <label className="field">
          <span className="field-label">Password</span>
          <input className="input" type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        <div className="auth-links">
          <Link to="/login">Company sign in</Link>
        </div>
      </form>
    </div>
  );
}
