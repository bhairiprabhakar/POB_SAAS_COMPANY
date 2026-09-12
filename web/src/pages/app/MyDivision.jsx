import { Link } from 'react-router-dom';
import { api, fmtDate } from '../../api';
import {
  DetailHero, ErrorBox, PageHeader, PanelCard, StatCard, StatSkeleton, StatusBadge, useAsync,
} from '../../ui';

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

      <DetailHero
        title={d.name || 'Division'}
        subtitle={d.code ? `Division code ${d.code}` : 'Division profile'}
        badge={<StatusBadge value={d.status || 'active'} />}
        primaryLabel="Active employees"
        primary={d.active_user_count ?? 0}
        secondary={d.user_count != null ? `of ${d.user_count} total` : null}
        facts={[
          ['Code', d.code || '—'],
          ['Campaigns', `${d.active_campaign_count ?? 0} active of ${d.campaign_count ?? 0}`],
          ['Since', d.created_at ? fmtDate(d.created_at) : '—'],
        ]}
        tone={d.status === 'active' ? 'green' : 'amber'}
      />

      <div className="stats-grid">
        {stats.map((s) => <StatCard key={s.label} {...s} />)}
      </div>

      <div style={{ marginTop: 16 }}>
      <PanelCard title="Division details">
        <div className="detail-grid">
          {[['Division name', d.name],
            ['Code', d.code],
            ['Status', d.status || 'active'],
            ['Roster status', d.active_user_count > 0
              ? `${d.active_user_count} active of ${d.user_count} employees`
              : 'No active employees yet'],
          ].map(([k, v]) => (
            <div className="detail-item" key={k}><span className="detail-label">{k}</span><span className="detail-value">{v}</span></div>
          ))}
        </div>
      </PanelCard>
    </div>
    </div>
  );
}