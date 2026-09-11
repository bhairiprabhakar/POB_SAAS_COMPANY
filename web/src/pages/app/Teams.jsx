import { useMemo, useState } from 'react';
import { api } from '../../api';
import {
  Badge, EmptyState, ErrorBox, PageHeader, SearchBox, StatCard, StatSkeleton, toast, useAsync,
} from '../../ui';

export default function Teams() {
  const [q, setQ] = useState('');
  const { data, loading, error, run } = useAsync(() => api('/api/v1/teams'));

  const teams = useMemo(() => {
    const rows = data?.teams || [];
    if (!q) return rows;
    const n = q.toLowerCase();
    return rows.filter((t) =>
      (t.manager_name || '').toLowerCase().includes(n) ||
      t.members.some((m) => (m.full_name || '').toLowerCase().includes(n)
        || (m.username || '').toLowerCase().includes(n)));
  }, [data, q]);

  if (loading) {
    return (
      <div>
        <PageHeader title="Teams" subtitle="Manager-led teams across your hierarchy" />
        <StatSkeleton n={3} />
        <div className="admin-grid">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="card" style={{ height: 130 }} />)}</div>
      </div>
    );
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const c = data?.counts || {};

  return (
    <div>
      <PageHeader title="Teams"
        subtitle={`${(c.teams || 0)} teams · ${(c.managed || 0)} managed · ${(c.members || 0)} members`}
        actions={<SearchBox value={q} onChange={setQ} placeholder="Search team / member…" />} />
      <div className="stats-grid compact">
        <StatCard label="Teams" value={c.teams || 0} />
        <StatCard label="Managed teams" value={c.managed || 0} tone="green" />
        <StatCard label="Members" value={c.members || 0} tone="blue" />
      </div>
      {teams.length ? (
        <div className="admin-grid">
          {teams.map((t) => (
            <div className="card" key={t.manager_id} style={{ padding: 14 }}>
              <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                <strong>{t.manager_name}</strong>
                <Badge tone="blue">{t.role}</Badge>
              </div>
              <div className="muted" style={{ marginBottom: 10 }}>@{t.manager_username} · {t.size} member{t.size === 1 ? '' : 's'}</div>
              <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                {t.members.map((m) => (
                  <span key={m.id} className="chip">{m.full_name || m.username}</span>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : <EmptyState text="No teams match — set parent/manager on employees to build teams" />}
    </div>
  );
}