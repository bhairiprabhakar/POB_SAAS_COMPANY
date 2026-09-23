import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '../../api';
import { ErrorBox, PageHeader, SearchBox, StatCard, StatSkeleton, roleLabel, toast, useAsync } from '../../ui';

export default function Regions() {
  const [params, setParams] = useSearchParams();
  const view = params.get('view') === 'territory' ? 'territory' : 'region';
  const [q, setQ] = useState('');
  const { data, loading, error, run } = useAsync(() => api('/api/v1/users'));

  const rows = useMemo(() => {
    const users = data?.items || [];
    const groups = new Map();
    for (const u of users) {
      const key = view === 'region' ? (u.region || '') : (u.territory || '');
      const slot = groups.get(key) || { key, region: u.region, area: u.area, territory: u.territory, count: 0, designations: new Set(), users: [] };
      slot.count += 1;
      if (u.role_name) slot.designations.add(u.role_name);
      slot.users.push(u.full_name || u.username);
      groups.set(key, slot);
    }
    let out = [...groups.values()].map((s) => ({ ...s, designations: [...s.designations] }))
      .filter((s) => s.key)
      .sort((a, b) => (a.key || '').localeCompare(b.key || ''));
    if (q) {
      const n = q.toLowerCase();
      out = out.filter((r) => (r.key || '').toLowerCase().includes(n)
        || (r.region || '').toLowerCase().includes(n)
        || (r.area || '').toLowerCase().includes(n)
        || (r.territory || '').toLowerCase().includes(n)
        || (r.users.some((u) => u.toLowerCase().includes(n))));
    }
    return out;
  }, [data, q, view]);

  const regionCount = new Set((data?.items || []).map((u) => u.region).filter(Boolean)).size;
  const areaCount = new Set((data?.items || []).map((u) => u.area).filter(Boolean)).size;
  const territoryCount = new Set((data?.items || []).map((u) => u.territory).filter(Boolean)).size;

  const switchView = (v) => {
    const next = new URLSearchParams(params);
    next.set('view', v);
    setParams(next, { replace: false });
    toast(v === 'region' ? 'Showing regions' : 'Showing territories', 'info');
  };

  if (loading) {
    return <div><PageHeader title={view === 'region' ? 'Regions' : 'Territories'} subtitle="Coverage drawn from your employee roster" /><StatSkeleton n={3} /></div>;
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const capped = rows.slice(0, 200);

  return (
    <div>
      <PageHeader title={view === 'region' ? 'Regions' : 'Territories'}
        subtitle={view === 'region' ? 'Where your field team is active — region level coverage' : 'Territory level split of your field coverage'}
        actions={<SearchBox value={q} onChange={setQ} placeholder="Search region / area / employee…" />} />
      <div className="stats-grid compact">
        <StatCard label="Regions" value={regionCount} tone="blue" />
        <StatCard label="Areas" value={areaCount} tone="teal" />
        <StatCard label="Territories" value={territoryCount} tone="green" />
      </div>
      <div className="segmented" role="tablist">
        <button className={`seg ${view === 'region' ? 'active' : ''}`} onClick={() => switchView('region')}>Regions</button>
        <button className={`seg ${view === 'territory' ? 'active' : ''}`} onClick={() => switchView('territory')}>Territories</button>
      </div>
      {capped.length ? (
        <table className="table">
          <thead>
            <tr>
              {view === 'territory' && <th>Region</th>}
              {view === 'territory' && <th>Area</th>}
              <th>{view === 'region' ? 'Region' : 'Territory'}</th>
              <th>Employees</th>
              <th>Designations</th>
            </tr>
          </thead>
          <tbody>
            {capped.map((r, i) => (
              <tr key={i}>
                {view === 'territory' && <td>{r.region || '—'}</td>}
                {view === 'territory' && <td>{r.area || '—'}</td>}
                <td><strong>{r.key}</strong></td>
                <td>{r.count}</td>
                <td className="muted">{r.designations.map(roleLabel).join(', ') || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="muted">No coverage yet — set region/area/territory on employees to build territories.</p>}
    </div>
  );
}