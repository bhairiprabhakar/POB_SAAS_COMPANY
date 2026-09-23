import { Link } from 'react-router-dom';
import { api, fmtDate } from '../../api';
import { Badge, ErrorBox, Icon, PageHeader, saRoleLabel, Spinner, useAsync } from '../../ui';

export default function SuperAdminProfile() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/auth/superadmin/me'));

  if (loading) return <Spinner label="Loading profile…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const u = data.user || {};
  const initials = (u.full_name || u.username || '?')
    .split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();

  return (
    <div>
      <PageHeader title="My Profile" subtitle="Your platform console account" />

      <div className="card profile-hero">
        <div className="profile-avatar">{initials}</div>
        <div className="profile-id">
          <h2>{u.full_name}</h2>
          <p className="muted">@{u.username}</p>
          <div className="profile-badges">
            <Badge tone={u.status || 'active'}>{u.status || 'active'}</Badge>
            <Badge tone="blue">{saRoleLabel(data.role)}</Badge>
            {u.owner && <Badge tone="red">Founder</Badge>}
          </div>
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
          </div>
        </div>
        <div className="card profile-panel">
          <div className="panel-head">
            <span className="panel-head-icon" data-tone="amber"><Icon name="lock" size={15} /></span>
            <h4>Account</h4>
          </div>
          <div className="kv-grid">
            <span>Member since<strong>{fmtDate(u.created_at)}</strong></span>
            <span>Role<strong>{saRoleLabel(data.role)}</strong></span>
          </div>
        </div>
      </div>

      {['owner', 'co_owner'].includes(data.role) && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            Manage other platform admins from <Link to="/superadmin/admins">Platform Admins</Link>.
          </p>
        </div>
      )}
    </div>
  );
}
