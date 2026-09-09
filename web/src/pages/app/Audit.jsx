import { useState } from 'react';
import { api, fmtDateTime } from '../../api';
import { ErrorBox, PageHeader, SearchBox, Spinner, useAsync } from '../../ui';

export default function Audit() {
  const [action, setAction] = useState('');
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  const [limit, setLimit] = useState(50);
  const { data, loading, error, run } = useAsync(
    () => api(`/api/v1/audit/logs?limit=${limit}${action ? `&action=${action}` : ''}${debounced ? `&q=${debounced}` : ''}`),
    [action, debounced, limit],
  );

  const actions = [...new Set((data?.items || []).map((i) => i.action))];

  return (
    <div>
      <PageHeader title="Audit Logs" subtitle="Full trail with request metadata and before/after state" />
      <div className="toolbar">
        <select className="input" value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">All actions</option>
          {actions.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <SearchBox value={q} onChange={(v) => { setQ(v); setTimeout(() => setDebounced(v), 400); }} placeholder="Search actor / action / detail…" />
        <select className="input" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          {[25, 50, 100].map((n) => <option key={n} value={n}>{n} rows</option>)}
        </select>
      </div>
      {loading && <Spinner label="Loading audit trail…" />}
      {error && <ErrorBox error={error} onRetry={run} />}
      {!loading && !error && (
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr>
              <th>When</th><th>Actor</th><th>Action</th><th>Entity</th>
              <th>IP</th><th>User-Agent</th><th>GPS</th><th>Details</th>
            </tr></thead>
            <tbody>
              {(data?.items || []).map((i) => (
                <tr key={i.id}>
                  <td className="nowrap">{fmtDateTime(i.created_at)}</td>
                  <td>{i.actor || '—'}</td>
                  <td><code>{i.action}</code></td>
                  <td className="nowrap">{(i.entity_type || '') + (i.entity_id ? ` #${i.entity_id}` : '') || '—'}</td>
                  <td className="nowrap">{i.ip || '—'}</td>
                  <td className="nowrap">{i.user_agent || '—'}</td>
                  <td className="nowrap">{i.gps_lat != null ? `${i.gps_lat}, ${i.gps_lng ?? ''}` : '—'}</td>
                  <td className="cell-pre">
                    {JSON.stringify({ ...(i.detail || {}),
                      before: i.before_state, after: i.after_state }).slice(0, 140)}
                  </td>
                </tr>
              ))}
              {(data?.items || []).length === 0 && (
                <tr><td colSpan={8} className="empty-state">No audit entries yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
