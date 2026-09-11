import { Link } from 'react-router-dom';
import { api } from '../../api';
import { ErrorBox, PageHeader, PanelCard, StatCard, StatSkeleton, toast, useAsync } from '../../ui';

export default function MyDivision() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/my-division'));
  if (loading) {
    return <div><PageHeader title="My Division" subtitle="Your division profile &amp; snapshot" /><StatSkeleton n={5} /></div>;
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;
  const d = data?.division || {};

  const stats = [
    { label: 'Employees', value: d.user_count, sub: `${d.active_user_count ?? 0} active`, tone: 'blue' },
    { label: 'Campaigns', value: d.campaign_count, sub: `${d.active_campaign_count ?? 0} active`, tone: 'teal' },
    { label: 'Chemists', value: d.chemist_count, tone: 'amber' },
    { label: 'POBs', value: d.pob_count, sub: `${d.verified_pob_count ?? 0} verified`, tone: 'green' },
    { label: 'Gratifications', value: d.gratification_count, tone: 'purple' },
  ];

  return (
    <div>
      <PageHeader title="My Division"
        subtitle={d.description || 'Your division profile &amp; snapshot'}
        actions={<Link className="btn" to="/app/admin">Open dashboard</Link>} />
      <div className="stats-grid">
        {stats.map((s) => <StatCard key={s.label} {...s} />)}
      </div>
      <PanelCard title="Division details">
        <div className="grid two">
          <span className="muted">Division name</span><strong>{d.name}</strong>
          <span className="muted">Code</span><span>{d.code || '—'}</span>
          <span className="muted">Status</span><span>{d.status || 'active'}</span>
          <span className="muted">Roster status</span>
          <span>{d.active_user_count > 0 ? `${d.active_user_count} active employees` : 'No active employees yet'}</span>
        </div>
      </PanelCard>
    </div>
  );
}