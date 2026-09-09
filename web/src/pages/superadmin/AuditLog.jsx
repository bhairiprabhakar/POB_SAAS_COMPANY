import { api, fmtDateTime } from '../../api';
import { Badge, ErrorBox, PageHeader, Spinner, Table, useAsync } from '../../ui';

export default function AuditLog() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/superadmin/audit-logs'));
  if (loading) return <Spinner label="Loading audit log…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const cols = [
    { key: 'id', label: '#' },
    { key: 'actor', label: 'Actor' },
    { key: 'action', label: 'Action', render: (r) => <code>{r.action}</code> },
    { key: 'entity_type', label: 'Entity', render: (r) => r.entity_type || '—' },
    { key: 'entity_id', label: 'Entity ID' },
    { key: 'detail', label: 'Detail', render: (r) => {
      if (!r.detail) return '—';
      try { return <code>{JSON.stringify(r.detail)}</code>; } catch { return '—'; }
    } },
    { key: 'ip', label: 'IP' },
    { key: 'created_at', label: 'When', render: (r) => fmtDateTime(r.created_at) },
  ];

  return (
    <div>
      <PageHeader title="Audit Log" subtitle="Super-admin actions on the control plane" />
      <div className="table-wrap">
        <table className="data-table">
          <thead><tr>{cols.map((c) => <th key={c.key}>{c.label}</th>)}</tr></thead>
          <tbody>
            {(data?.items || []).map((r) => (
              <tr key={r.id}>
                {cols.map((c) => <td key={c.key}>{c.render ? c.render(r) : r[c.key] ?? '—'}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">Showing latest {data?.items?.length || 0} of {data?.total || 0} entries.</p>
    </div>
  );
}
