import { useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { api } from '../api';
import { TextInput } from '../ui';

export default function ChangePassword() {
  const nav = useNavigate();
  const { state } = useLocation();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const token = state?.password_change_token;
  const username = state?.username || '';
  const division = state?.division;

  const submit = async (e) => {
    e.preventDefault();
    if (newPassword !== confirm) {
      setError('Passwords do not match');
      return;
    }
    if (newPassword.length < 6) {
      setError('Password must be at least 6 characters');
      return;
    }
    setError('');
    setBusy(true);
    try {
      await api('/api/v1/auth/change-password', {
        method: 'POST',
        body: {
          username,
          password_change_token: token,
          current_password: currentPassword,
          new_password: newPassword,
        },
      });
      const dest = division?.code ? `/login/${encodeURIComponent(division.code)}` : '/login';
      nav(dest, { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!token || !username) {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <div className="auth-brand">
            <span className="brand-mark lg">F</span>
            <h1>Set a new password</h1>
          </div>
          <div className="error-box">
            This page can only be reached right after signing in with a temporary
            password. Please sign in again with your temporary password.
          </div>
          <Link className="btn btn-primary btn-block" to="/login">Back to sign in</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">
          <span className="brand-mark lg">F</span>
          <h1>Set a new password</h1>
          <p className="muted">
            You are signed in with a temporary password for <strong>{username}</strong>.
            Choose a password only you know.
          </p>
        </div>
        {error && <div className="error-box">{error}</div>}
        <label className="field">
          <span className="field-label">Temporary password</span>
          <TextInput className="input" type="password" value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)} autoFocus required />
        </label>
        <label className="field">
          <span className="field-label">New password</span>
          <TextInput className="input" type="password" value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)} minLength={6} required />
        </label>
        <label className="field">
          <span className="field-label">Confirm new password</span>
          <TextInput className="input" type="password" value={confirm}
            onChange={(e) => setConfirm(e.target.value)} minLength={6} required />
        </label>
        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy ? 'Saving…' : 'Set new password'}
        </button>
        <div className="auth-links">
          <Link to="/login">Back to sign in</Link>
        </div>
      </form>
    </div>
  );
}