import { useEffect, useState } from 'react';
import { Navigate, useNavigate, useParams } from 'react-router-dom';
import { api, getSession, setSession } from '../../api';

function MfaStep({ user, onDone }) {
  const [status, setStatus] = useState(null);
  const [secret, setSecret] = useState(null);
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    api('/api/v1/auth/mfa/status')
      .then((d) => { setStatus(d); })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (status && status.enabled) onDone();
  }, [status]);

  if (status && status.enabled) {
    return <div className="card" style={{ maxWidth: 520, margin: '24px auto' }}>
      <p className="muted">Two-factor auth already enabled — taking you in…</p>
    </div>;
  }

  const generate = async () => {
    setBusy(true);
    setError('');
    try {
      const d = await api('/api/v1/auth/mfa/setup', { method: 'POST' });
      setSecret(d);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const enable = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api('/api/v1/auth/mfa/enable', { method: 'POST', body: { code } });
      await onDone();
    } catch (err) { setError(err.message); setBusy(false); }
  };

  return (
    <div className="card" style={{ maxWidth: 520, margin: '24px auto' }}>
      <h3 className="section-title">Set up two-factor authentication</h3>
      <p className="muted">Your account requires a one-time code from an authenticator app before you can continue.</p>
      {error && <div className="error-box">{error}</div>}

      {!status ? <p className="muted">Loading…</p> : (
        <>
          {!secret ? (
            <>
              {!status.has_secret && !status.enabled && (
                <p className="muted">Open your authenticator app (Google Authenticator, Microsoft Authenticator, Authy…), then generate a QR-encoded setup key.</p>
              )}
              <button className="btn btn-primary" onClick={generate} disabled={busy}>
                {busy ? 'Generating…' : status.has_secret ? 'Show setup key' : 'Start setup'}
              </button>
            </>
          ) : (
            <div style={{ marginTop: 12 }}>
              <label className="field">
                <span className="field-label">Setup key</span>
                <code className="input" style={{ display: 'block', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{secret.secret}</code>
              </label>
              <p className="muted">
                In your authenticator app, choose “Enter a setup key” and paste the key above.
                Or scan the QR via <a href={secret.otpauth_uri} target="_blank" rel="noreferrer">this link</a>.
              </p>
              <form onSubmit={enable}>
                <label className="field">
                  <span className="field-label">Code from your app</span>
                  <input className="input" value={code} onChange={(e) => setCode(e.target.value)}
                    placeholder="000000" maxLength={6} autoFocus required />
                </label>
                <button className="btn btn-primary btn-block" disabled={busy}>
                  {busy ? 'Verifying…' : 'Verify & continue'}
                </button>
              </form>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ProfileStep({ user, onDone }) {
  const [form, setForm] = useState({
    full_name: user.full_name || '',
    email: user.email || '',
    mobile: user.mobile || '',
    employee_id: user.employee_id || '',
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await api('/api/v1/auth/me', { method: 'PUT', body: form });
      await onDone();
    } catch (err) { setError(err.message); setBusy(false); }
  };

  return (
    <div className="card" style={{ maxWidth: 520, margin: '24px auto' }}>
      <h3 className="section-title">Complete your profile</h3>
      <p className="muted">A few details are missing. Your mobile number is required so your field colleagues can reach you.</p>
      {error && <div className="error-box">{error}</div>}
      <form onSubmit={submit}>
        <label className="field">
          <span className="field-label">Full name</span>
          <input className="input" value={form.full_name} required
            onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
        </label>
        <label className="field">
          <span className="field-label">Email</span>
          <input className="input" type="email" value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </label>
        <label className="field">
          <span className="field-label">Mobile number</span>
          <input className="input" value={form.mobile} required
            onChange={(e) => setForm({ ...form, mobile: e.target.value })} />
        </label>
        <label className="field">
          <span className="field-label">Employee ID</span>
          <input className="input" value={form.employee_id}
            onChange={(e) => setForm({ ...form, employee_id: e.target.value })} />
        </label>
        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy ? 'Saving…' : 'Save & continue'}
        </button>
      </form>
    </div>
  );
}

export default function Onboarding() {
  const { step } = useParams();
  const nav = useNavigate();

  const advance = async () => {
    const me = await api('/api/v1/auth/me');
    const s = getSession();
    const user = { ...(s?.user || {}), ...me.user, role: s?.user?.role };
    const next = me.user?.onboarding || null;
    if (next) user.onboarding = next; else delete user.onboarding;
    setSession({ ...s, user, permissions: me.permissions || (s?.permissions || []) });
    nav(next ? `/app/onboarding/${next}` : '/app', { replace: true });
  };

  if (step === 'mfa') return <MfaStep user={getSession()?.user || {}} onDone={advance} />;
  if (step === 'profile') return <ProfileStep user={getSession()?.user || {}} onDone={advance} />;
  return <Navigate to="/app" replace />;
}