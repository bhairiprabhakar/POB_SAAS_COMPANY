import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../../api';
import {
  Badge, DivisionLink, EmptyState, ErrorBox, PageHeader, SearchBox, Select,
  StatCard, StatSkeleton, Table, TableSkeleton, toast, useAsync,
} from '../../ui';

export default function SuperUsers() {
  const [params] = useSearchParams();
  const [q, setQ] = useState('');
  const [role, setRole] = useState(() => params.get('role') || '');
  const [divFilter, setDivFilter] = useState('');
  const { data, loading, error, run } = useAsync(() => api('/api/v1/superadmin/users'));

  const items = data?.items || [];
  const roleCounts = data?.role_counts || {};
  const statusCounts = data?.status_counts || {};

  const divisions = useMemo(() =>
    [...new Map(items.map((i) => [i.division_id, i.division_name])).entries()]
      .map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name)),
    [items]);

  const roles = useMemo(() => Object.keys(roleCounts).sort(), [roleCounts]);

  const rows = useMemo(() => {
    let out = items;
    if (q) {
      const n = q.toLowerCase();
      out = out.filter((i) => (i.full_name || '').toLowerCase().includes(n)
        || (i.username || '').toLowerCase().includes(n)
        || (i.email || '').toLowerCase().includes(n)
        || (i.mobile || '').includes(n)
        || (i.region || '').toLowerCase().includes(n));
    }
    if (role) out = out.filter((i) => i.role === role);
    if (divFilter) out = out.filter((i) => i.division_id === Number(divFilter));
    return out;
  }, [items, q, role, divFilter]);

  const cols = [
    { key: 'full_name', label: 'Employee', render: (r) =>
      <><strong>{r.full_name}</strong> <span className="muted">@{r.username}</span></> },
    { key: 'division_name', label: 'Division', render: (r) =>
      <DivisionLink id={r.division_id} onClick={(e) => e.stopPropagation()}
        className="chip">{r.division_name}</DivisionLink> },
    { key: 'role', label: 'Role', render: (r) => <Badge tone="blue">{r.role}</Badge> },
    { key: 'region', label: 'Region', render: (r) => r.region || '—' },
    { key: 'status', label: 'Status', render: (r) => <Badge tone={r.status}>{r.status}</Badge> },
    { key: 'email', label: 'Email', render: (r) => r.email || '—' },
    { key: 'mobile', label: 'Mobile', render: (r) => r.mobile || '—' },
  ];

  const header = (
    <PageHeader title="All Employees"
      subtitle="Every employee across every division — the platform-wide user directory."
      actions={<>
        <SearchBox value={q} onChange={setQ} placeholder="Search name, username, email…" />
        <Select placeholder="Role…" value={role} onChange={(e) => setRole(e.target.value)}
          options={roles.map((r) => ({ value: r, label: r }))} />
        <Select placeholder="Division…" value={divFilter} onChange={(e) => setDivFilter(e.target.value)}
          options={divisions.map((d) => ({ value: String(d.id), label: d.name }))} />
      </>} />
  );

  if (loading) return <div>{header}<StatSkeleton n={4} /><TableSkeleton cols={7} rows={8} /></div>;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const managed = Object.keys(roleCounts).length;

  return (
    <div>
      {header}
      <div className="stats-grid compact">
        <StatCard label="Employees" value={items.length} />
        <StatCard label="Active" value={statusCounts.active || 0} tone="green" />
        <StatCard label="Inactive / left" value={(statusCounts.inactive || 0) + (statusCounts.left || 0)} tone="gray" />
        <StatCard label="Distinct roles" value={managed} tone="blue" />
      </div>
      {data?.unreachable?.length > 0 && (
        <p className="muted">Unreachable divisions: {data.unreachable.map((u) => u.name).join(', ')}</p>)}
      {rows.length
        ? <Table cols={cols} rows={rows} keyOf={(r) => `${r.division_id}-${r.user_id}`}
            empty={<EmptyState text="No employees match" />} />
        : <EmptyState text="No employees found" />}
    </div>
  );
}