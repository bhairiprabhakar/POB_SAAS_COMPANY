import { Link } from 'react-router-dom';
import { api, fmtDate, fmtDateTime, getSession } from '../../api';
import { Badge, ErrorBox, Icon, PageHeader, roleLabel, Spinner, useAsync } from '../../ui';

export default function Profile() {
  const session = getSession();
  const { data, loading, error, run } = useAsync(() => api('/api/v1/auth/me'));
  const perms = session?.permissions || [];

  if (loading) return <Spinner label="Loading profile…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const u = data.user || {};
  const level = data.hierarchy_level || {};
  const manager = data.manager || null;
  const initials = (u.full_name || u.username || '?')
    .split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();

  return (
    <div>
      <PageHeader title="My Profile" subtitle="Your account, territory and access details" />

      <div className="card profile-hero">
        <div className="profile-avatar">{initials}</div>
        <div className="profile-id">
          <h2>{u.full_name}</h2>
          <p className="muted">@{u.username}{u.employee_id ? ` · Emp ${u.employee_id}` : ''}</p>
          <div className="profile-badges">
            <Badge tone={u.status || 'active'}>{u.status || 'active'}</Badge>
            <Badge tone="blue">{roleLabel(data.role)}</Badge>
            {level.name && <Badge tone="gray">{level.name}</Badge>}
          </div>
        </div>
        <div className="profile-actions">
          {perms.includes('apikey.view') && (
            <Link className="btn" to="/app/security"><Icon name="lock" size={15} /> Security</Link>
          )}
        </div>
      </div>

      <div className="stats-grid">
        <div className="card profile-panel">
          <div className="panel-head">
            <span className="panel-head-icon" data-tone="blue"><Icon name="mail" size={15} /></span>
            <h4>Contact</h4>
          </div>
          <div className="kv-grid">
            <span>Email<strong>{u.email || '—'}</strong></span>
            <span>Mobile<strong>{u.mobile || '—'}</strong></span>
          </div>
        </div>
        <div className="card profile-panel">
          <div className="panel-head">
            <span className="panel-head-icon" data-tone="green"><Icon name="map-pin" size={15} /></span>
            <h4>Territory</h4>
          </div>
          <div className="kv-grid">
            <span>Region<strong>{u.region || '—'}</strong></span>
            <span>Area<strong>{u.area || '—'}</strong></span>
            <span>Territory<strong>{u.territory || '—'}</strong></span>
          </div>
        </div>
        <div className="card profile-panel">
          <div className="panel-head">
            <span className="panel-head-icon" data-tone="indigo"><Icon name="building" size={15} /></span>
            <h4>Organization</h4>
          </div>
          <div className="kv-grid">
            <span>Role<strong>{roleLabel(data.role)}</strong></span>
            <span>Division<strong>{u.division_name || u.division || '—'}</strong></span>
            <span>Hierarchy level<strong>{level.name || '—'}{level.rank ? ` (L${level.rank})` : ''}</strong></span>
            <span>Reporting manager<strong>{manager ? manager.full_name : '—'}</strong></span>
            <span>Employee ID<strong>{u.employee_id || '—'}</strong></span>
          </div>
        </div>
        <div className="card profile-panel">
          <div className="panel-head">
            <span className="panel-head-icon" data-tone="amber"><Icon name="lock" size={15} /></span>
            <h4>Account</h4>
          </div>
          <div className="kv-grid">
            <span>Member since<strong>{fmtDate(u.created_at)}</strong></span>
            <span>Last login<strong>{fmtDateTime(u.last_login)}</strong></span>
            <span>Two-factor auth<strong>{u.mfa_enabled ? 'Enabled' : 'Off'}</strong></span>
          </div>
        </div>
      </div>

      <div className="card">
        <h4>Permissions ({perms.length})</h4>
        <div className="perm-chip-wrap">
          {perms.map((p) => <span key={p} className="perm-chip">{p}</span>)}
          {perms.length === 0 && <span className="muted">No permissions granted</span>}
        </div>
      </div>
    </div>
  );
}
