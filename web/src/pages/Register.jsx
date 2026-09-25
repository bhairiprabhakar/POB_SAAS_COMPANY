import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, setSession } from '../api';
import { TextInput } from '../ui';

const ownerDesignations = ['Owner', 'Director', 'Administrator'];

export default function Register() {
  const nav = useNavigate();
  const [loading, setLoading] = useState(true);
  const [regOpen, setRegOpen] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [brand, setBrand] = useState({ platform_name: 'FieldNet', has_logo: false });

  const [company, setCompany] = useState({});
  const [owner, setOwner] = useState({ designation: 'Owner' });

  const set = (obj, key) => (e) => {
    setError('');
    obj === company
      ? setCompany({ ...company, [key]: e.target.value })
      : setOwner({ ...owner, [key]: e.target.value });
  };

  useEffect(() => {
    api('/api/v1/auth/platform-branding').then(setBrand).catch(() => {});
    api('/api/v1/auth/register-status')
      .then((d) => {
        setRegOpen(Boolean(d.registration_open));
        if (!d.registration_open) nav('/superadmin-login', { replace: true });
      })
      .catch(() => setRegOpen(false))
      .finally(() => setLoading(false));
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      const d = await api('/api/v1/auth/register', {
        method: 'POST',
        body: { company, owner },
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

  if (loading || regOpen === null) return null;

  if (!regOpen) {
    return (
      <div className="auth-page">
        <form className="auth-card" onSubmit={(e) => e.preventDefault()}>
          <div className="auth-brand">
            {brand.has_logo
              ? <img src="/api/v1/auth/platform-logo" alt={brand.platform_name} className="auth-logo" />
              : <span className="brand-mark lg">C</span>}
            {!brand.has_logo && <h1>{brand.platform_name}</h1>}
          </div>
          <p>This platform is already registered to a company. First-run registration is closed.</p>
          <Link className="btn btn-primary btn-block" to="/superadmin-login">Go to sign in</Link>
        </form>
      </div>
    );
  }

  return (
    <div className="auth-page">
      <form className="auth-card auth-card-wide" onSubmit={submit}>
        <div className="auth-brand">
          {brand.has_logo
            ? <img src="/api/v1/auth/platform-logo" alt={brand.platform_name} className="auth-logo" />
            : <span className="brand-mark lg">C</span>}
          {!brand.has_logo && <h1>{brand.platform_name}</h1>}
          <p>Set up your company</p>
        </div>
        {error && <div className="error-box">{error}</div>}

        <fieldset className="fieldset">
          <legend>Company profile</legend>
          <label className="field">
            <span className="field-label">Legal name *</span>
            <input className="input" value={company.legal_name || ''}
              onChange={set(company, 'legal_name')} required />
          </label>
          <label className="field">
            <span className="field-label">Display name *</span>
            <input className="input" value={company.display_name || ''}
              onChange={set(company, 'display_name')} required placeholder="Shown in the console" />
          </label>
          <div className="field-row">
            <label className="field">
              <span className="field-label">Company code</span>
              <input className="input" value={company.code || ''}
                onChange={set(company, 'code')} placeholder="Short identifier" />
            </label>
            <label className="field">
              <span className="field-label">Official email</span>
              <input className="input" type="email" value={company.official_email || ''}
                onChange={set(company, 'official_email')} />
            </label>
          </div>
          <div className="field-row">
            <label className="field">
              <span className="field-label">Contact number</span>
              <input className="input" value={company.contact_number || ''}
                onChange={set(company, 'contact_number')} />
            </label>
            <label className="field">
              <span className="field-label">GSTIN</span>
<input className="input" value={company.gstin || ''}
              onChange={set(company, 'gstin')} />
            </label>
          </div>
          <label className="field">
            <span className="field-label">Address</span>
            <input className="input" value={company.address || ''}
              onChange={set(company, 'address')} />
          </label>
          <div className="field-row">
            <label className="field">
              <span className="field-label">City</span>
              <input className="input" value={company.city || ''}
                onChange={set(company, 'city')} />
            </label>
            <label className="field">
              <span className="field-label">State</span>
              <input className="input" value={company.state || ''}
                onChange={set(company, 'state')} />
            </label>
            <label className="field">
              <span className="field-label">Pincode</span>
              <input className="input" value={company.pincode || ''}
                onChange={set(company, 'pincode')} />
            </label>
          </div>
          <label className="field">
            <span className="field-label">Website</span>
            <input className="input" value={company.website || ''}
              onChange={set(company, 'website')} placeholder="Optional" />
          </label>
        </fieldset>

        <fieldset className="fieldset">
          <legend>Company owner account</legend>
          <div className="field-row">
            <label className="field">
              <span className="field-label">Full name *</span>
              <input className="input" value={owner.full_name || ''}
                onChange={set(owner, 'full_name')} required />
            </label>
            <label className="field">
              <span className="field-label">Designation</span>
              <select className="input" value={owner.designation || 'Owner'}
                onChange={set(owner, 'designation')}>
                {ownerDesignations.map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </label>
          </div>
          <div className="field-row">
            <label className="field">
              <span className="field-label">Email *</span>
              <input className="input" type="email" value={owner.email || ''}
                onChange={set(owner, 'email')} required />
            </label>
            <label className="field">
              <span className="field-label">Mobile</span>
              <input className="input" value={owner.mobile || ''}
                onChange={set(owner, 'mobile')} />
            </label>
          </div>
          <label className="field">
            <span className="field-label">Username *</span>
            <input className="input" value={owner.username || ''}
              onChange={set(owner, 'username')} required autoComplete="off" />
          </label>
          <label className="field">
            <span className="field-label">Password *</span>
            <TextInput className="input" type="password" value={owner.password || ''}
              onChange={set(owner, 'password')} required minLength={6} autoComplete="new-password" />
          </label>
          <p className="field-hint">The owner is the first super admin. You can add more super admins later.</p>
        </fieldset>

        <button className="btn btn-primary btn-block" disabled={busy}>
          {busy ? 'Setting up…' : 'Create company'}
        </button>
        <div className="auth-links">
          <Link to="/superadmin-login">Already set up? Sign in</Link>
        </div>
      </form>
    </div>
  );
}