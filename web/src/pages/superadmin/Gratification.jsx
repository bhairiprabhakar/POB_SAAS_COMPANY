import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, saRole } from '../../api';
import {
  Badge, DivisionLink, EmptyState, ErrorBox, PageHeader, StatCard, StatSkeleton,
  Table, TableSkeleton, Tabs, toast, useAsync,
} from '../../ui';

const STATUS_LABELS = {
  eligible: 'Eligible', approved: 'Approved', generated: 'Generated',
  paid: 'Paid', dispatched: 'Dispatched', sent: 'Sent', delivered: 'Delivered',
  acknowledged: 'Acknowledged', completed: 'Completed', redeemed: 'Redeemed',
};

export default function Gratification() {
  const [params] = useSearchParams();
  const [tab, setTab] = useState(() => {
    const t = params.get('status');
    return Object.keys(STATUS_LABELS).includes(t) ? t : '';
  });
  const { data, loading, error, run } = useAsync(
    () => api(`/api/v1/superadmin/gratification${tab ? `?status=${tab}` : ''}`), [tab]);

  const counts = data?.counts || {};
  const types = data?.types || {};
  const num = (k) => Number(counts[k]) || 0;

  const tabs = useMemo(() => {
    const order = ['eligible', 'approved', 'generated', 'paid', 'dispatched', 'delivered', 'completed'];
    return [{ id: '', label: 'All', badge: order.reduce((s, k) => s + num(k), 0) }]
      .concat(order.map((s) => ({ id: s, label: STATUS_LABELS[s] || s, badge: num(s) })));
  }, [counts]);

  const canOps = !['campaign_admin', 'verification_admin'].includes(saRole());

  const act = async (path, okMsg) => {
    try {
      await api(path, { method: 'POST' });
      toast(okMsg, 'success');
      run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const cols = [
    { key: 'id', label: 'Grant', render: (r) => <strong>G#{r.id}</strong> },
    { key: 'division', label: 'Division', render: (r) =>
      <DivisionLink id={r.division_id} onClick={(e) => e.stopPropagation()}
        className="chip">{r.division_name}</DivisionLink> },
    { key: 'user_name', label: 'User' },
    { key: 'campaign_name', label: 'Campaign' },
    { key: 'type_code', label: 'Type', render: (r) => <Badge tone="blue">{String(r.type_code || '').replaceAll('_', ' ')}</Badge> },
    { key: 'scheme_value', label: 'Value', render: (r) => `₹${Number(r.scheme_value || 0).toLocaleString('en-IN')}` },
    { key: 'status', label: 'Status', render: (r) => <Badge tone={r.status}>{STATUS_LABELS[r.status] || String(r.status || '').replaceAll('_', ' ')}</Badge> },
    { key: 'created_at', label: 'Created', render: (r) => String(r.created_at || '').slice(0, 16).replace('T', ' ') },
    {
      key: '_actions', label: '', render: (r) => (
        <div className="row" style={{ gap: 6 }}>
          {canOps && ['cashback', 'upi', 'reward_points'].includes(r.type_code) && r.status === 'eligible' && (
            <button className="btn btn-sm btn-primary" onClick={() => act(
              `/api/v1/superadmin/divisions/${r.division_id}/gratification/${r.id}/approve`,
              `Grant G#${r.id} approved`)}>Approve</button>
          )}
          {canOps && ['cashback', 'upi', 'reward_points'].includes(r.type_code) && r.status === 'approved' && (
            <button className="btn btn-sm btn-primary" onClick={() => act(
              `/api/v1/superadmin/divisions/${r.division_id}/gratification/${r.id}/pay`,
              `Grant G#${r.id} marked paid`)}>Pay</button>
          )}
        </div>
      ),
    },
  ];

  const header = (
    <PageHeader title="Gratification"
      subtitle="Cross-division pipeline: what has been earned, processed and delivered."
      actions={<Tabs items={tabs} active={tab} onChange={setTab} />} />
  );

  if (loading) return <div>{header}<StatSkeleton n={4} /><TableSkeleton cols={8} rows={6} /></div>;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      {header}
      <div className="stats-grid compact">
        <StatCard label="Eligible" value={num('eligible')} tone="amber" />
        <StatCard label="Approved / paid" value={num('approved') + num('paid')} tone="green" />
        <StatCard label="Dispatched / delivered" value={num('dispatched') + num('delivered')} tone="blue" />
        <StatCard label="Completed" value={num('completed')} tone="green" />
      </div>
      {Object.keys(types).length > 0 && (
        <p className="muted">By type: {Object.entries(types).map(([t, n]) => `${t}: ${n}`).join(' · ')}</p>)}
      {data?.unreachable?.length > 0 && (
        <p className="muted">Unreachable divisions: {data.unreachable.map((u) => u.name).join(', ')}</p>)}
      {data?.recent?.length
        ? <Table cols={cols} rows={data.recent} keyOf={(r) => `${r.division_id}-${r.id}`}
            empty={<EmptyState text="No gratification records in this view" />} />
        : <EmptyState text="No gratification records yet" />}
    </div>
  );
}