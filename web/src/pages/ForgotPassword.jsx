import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';

const PURPOSES = [
  { key: 'division_code', label: 'Division code', hint: 'Find which division you belong to' },
  { key: 'username', label: 'Username', hint: 'Recover your sign-in username' },
  { key: 'password', label: 'Password', hint: 'Set a new password' },
];

export default function ForgotPassword() {
  const [purpose, setPurpose] = useState('password');
  const [contact, setContact] = useState('');
  const [otp, setOtp] = useState('');
  const [devOtp, setDevOtp] = useState('');
  const [step, setStep] = useState('form'); // form | otp | result
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const requestCode = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      const d = await api('/api/v1/auth/recovery/request', {
        method: 'POST',
        body: { contact, purpose },
      });
      setDevOtp(d.dev_otp || '');
      setStep('otp');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const verifyCode = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      const d = await api('/api/v1/auth/recovery/verify', {
        method: 'POST',
        body: { contact, purpose, otp },
      });
      setResult(d);
      setStep('result');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={step === 'form' ? requestCode : step === 'otp' ? verifyCode : (e) => e.preventDefault()}>
        <div className="auth-brand">
          <span className="brand-mark lg">C</span>
          <h1>Account recovery</h1>
          <p className="muted">Recover your division code, username or password using your registered email or mobile.</p>
        </div>
        {error && <div className="error-box">{error}</div>}

        {step === 'form' && (
          <>
            <div className="field">
              <span className="field-label">What did you forget?</span>
              <div className="recovery-purpose">
                {PURPOSES.map((p) => (
                  <button
                    type="button"
                    key={p.key}
                    className={`btn ${purpose === p.key ? 'btn-primary' : 'btn-ghost'} btn-block`}
                    onClick={() => setPurpose(p.key)}
                  >
                    <strong>{p.label}</strong>
                    <span className="muted" style={{ display: 'block', fontSize: 12 }}>{p.hint}</span>
                  </button>
                ))}
              </div>
            </div>
            <label className="field">
              <span className="field-label">Email or mobile number</span>
              <input className="input" value={contact} onChange={(e) => setContact(e.target.value)}
                placeholder="you@company.com or 9898989898" required />
            </label>
            <button className="btn btn-primary btn-block" disabled={busy}>
              {busy ? 'Sending…' : 'Send verification code'}
            </button>
          </>
        )}

        {step === 'otp' && (
          <>
            <p className="muted">We sent a one-time code to <strong>{contact}</strong>. Enter it below.</p>
            {devOtp && (
              <div className="error-box" style={{ background: 'var(--bg)', border: '1px dashed var(--border)' }}>
                <span className="muted">Development mode (no SMS/email configured):</span>
                <br /><strong style={{ letterSpacing: 4 }}>{devOtp}</strong>
              </div>
            )}
            <label className="field">
              <span className="field-label">Verification code</span>
              <input className="input" value={otp} onChange={(e) => setOtp(e.target.value)}
                placeholder="000000" maxLength={6} autoFocus required />
            </label>
            <button className="btn btn-primary btn-block" disabled={busy}>
              {busy ? 'Verifying…' : 'Verify'}
            </button>
            <button type="button" className="btn-link btn-block" onClick={() => { setStep('form'); setOtp(''); setDevOtp(''); }}>
              Change contact
            </button>
          </>
        )}

        {step === 'result' && <ResultView purpose={purpose} result={result} contact={contact} />}

        <div className="auth-links">
          <Link to="/login">Back to sign in</Link>
        </div>
      </form>
    </div>
  );
}

function ResultView({ purpose, result, contact }) {
  const [accounts] = useState(result?.accounts || []);
  const [selected, setSelected] = useState(null);
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [done, setDone] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const reset = async (e) => {
    e.preventDefault();
    if (newPassword !== confirm) {
      setError('Passwords do not match');
      return;
    }
    setError('');
    setBusy(true);
    try {
      await api('/api/v1/auth/recovery/reset-password', {
        method: 'POST',
        body: {
          recovery_token: result.recovery_token,
          division_id: selected.division_id,
          user_id: selected.user_id,
          new_password: newPassword,
        },
      });
      setDone(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <>
        <div className="error-box" style={{ background: 'var(--bg)', border: '1px solid #34a853', color: '#1a7f37' }}>Password updated successfully.</div>
        <Link className="btn btn-primary btn-block" to="/login">Sign in with new password</Link>
      </>
    );
  }

  if (!accounts.length) {
    return (
      <>
        <div className="error-box">No account was found for this contact.</div>
        <Link className="btn-link btn-block" to="/login">Back to sign in</Link>
      </>
    );
  }

  if (purpose === 'password') {
    return (
      <>
        <div className="field">
          <span className="field-label">Choose the account to reset</span>
          {accounts.map((a) => (
            <label key={`${a.division_id}-${a.username}`} className="recovery-account">
              <input type="radio" name="account" checked={selected?.division_id === a.division_id}
                onChange={() => setSelected(a)} />
              <span>
                <strong>{a.username}</strong>
                <span className="muted" style={{ display: 'block', fontSize: 12 }}>
                  {a.division_name} · {a.division_code}
                </span>
              </span>
            </label>
          ))}
        </div>
        {selected && (
          <>
            <label className="field">
              <span className="field-label">New password</span>
              <input className="input" type="password" value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)} minLength={6} required />
            </label>
            <label className="field">
              <span className="field-label">Confirm password</span>
              <input className="input" type="password" value={confirm}
                onChange={(e) => setConfirm(e.target.value)} minLength={6} required />
            </label>
            <button className="btn btn-primary btn-block" disabled={busy} onClick={reset}>
              {busy ? 'Saving…' : 'Set new password'}
            </button>
          </>
        )}
      </>
    );
  }

  return (
    <div className="field">
      <span className="field-label">{purpose === 'division_code' ? 'Your division code(s)' : 'Your username(s)'} for {contact}</span>
      {accounts.map((a, i) => (
        <div key={i} className="recovery-account">
          <span>
            <strong>{purpose === 'division_code' ? a.division_code : a.username}</strong>
            <span className="muted" style={{ display: 'block', fontSize: 12 }}>
              {a.division_name}{purpose === 'division_code' ? ` · ${a.username}` : ` · ${a.division_code}`}
            </span>
          </span>
        </div>
      ))}
      <p className="muted" style={{ marginTop: 12 }}>
        Head back to sign in and use {purpose === 'division_code' ? 'this division code' : 'your username'}.
      </p>
    </div>
  );
}
