import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, saRole } from '../../api';
import {
  Badge, EmptyState, ErrorBox, Field, Modal, PageHeader, Select, StatCard,
  StatSkeleton, Table, TableSkeleton, TextArea, toast, useAsync,
} from '../../ui';

const STATUSES = [
  { id: '', label: 'All statuses' },
  { id: 'pending_verification', label: 'Pending verification' },
  { id: 'verified', label: 'Verified' },
  { id: 'needs_review', label: 'Needs review' },
  { id: 'rejected', label: 'Rejected' },
  { id: 'submitted', label: 'Submitted' },
];

export default function PobOperations() {
  const [params] = useSearchParams();
  const [status, setStatus] = useState(() => {
    const t = params.get('status');
    return STATUSES.some((s) => s.id === t) ? t : '';
  });
  const { data, loading, error, run } = useAsync(
    () => api(`/api/v1/superadmin/pob${status ? `?status=${status}` : ''}`), [status]);

  const counts = data?.counts || {};
  const num = (k) => Number(counts[k]) || 0;

  const canOps = !['campaign_admin', 'finance_admin'].includes(saRole());
  const [rejecting, setRejecting] = useState(null);
  const [reason, setReason] = useState('');

  const act = async (path, okMsg, body) => {
    try {
      await api(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined });
      toast(okMsg, 'success');
      run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const doReject = async () => {
    if (!reason.trim()) { toast('A rejection reason is required', 'error'); return; }
    await act(
      `/api/v1/superadmin/divisions/${rejecting.division_id}/verification/${rejecting.verification_id}/reject`,
      `POB #${rejecting.id} rejected`, { reason });
    setRejecting(null); setReason('');
  };

  const cols = [
    { key: 'id', label: 'POB', render: (r) => <strong>#{r.id}</strong> },
    { key: 'division', label: 'Division', render: (r) =>
      <Link to={`/superadmin/divisions/${r.division_id}`} onClick={(e) => e.stopPropagation()}
        className="chip">{r.division_name}</Link> },
    { key: 'user_name', label: 'Submitted by' },
    { key: 'campaign_name', label: 'Campaign' },
    { key: 'chemist_name', label: 'Chemist' },
    { key: 'pob_amount', label: 'Amount', render: (r) => `₹${Number(r.pob_amount || 0).toLocaleString('en-IN')}` },
    { key: 'status', label: 'Status', render: (r) => <Badge tone={r.status}>{String(r.status || '').replaceAll('_', ' ')}</Badge> },
    { key: 'created_at', label: 'Created', render: (r) => String(r.created_at || '').slice(0, 16).replace('T', ' ') },
    {
      key: '_actions', label: '', render: (r) => (
        <div className="row" style={{ gap: 6 }}>
          {canOps && r.verification_id && r.status === 'pending_verification' && (
            <>
              <button className="btn btn-sm btn-primary" onClick={() => act(
                `/api/v1/superadmin/divisions/${r.division_id}/verification/${r.verification_id}/approve`,
                `POB #${r.id} verified`)}>Verify</button>
              <button className="btn btn-sm btn-danger" onClick={() => setRejecting(r)}>Reject</button>
              <button className="btn btn-sm" onClick={() => act(
                `/api/v1/superadmin/divisions/${r.division_id}/verification/${r.verification_id}/duplicate`,
                `POB #${r.id} marked duplicate`)}>Duplicate</button>
            </>
          )}
        </div>
      ),
    },
  ];

  const header = (
    <PageHeader title="POB Operations"
      subtitle="Cross-division view of POB submissions, verification and exceptions."
      actions={
        <Select placeholder="Filter by status…" value={status} onChange={(e) => setStatus(e.target.value)}
          options={STATUSES.map((s) => ({ value: s.id, label: s.label }))} />
      } />
  );

  if (loading) return <div>{header}<StatSkeleton n={4} /><TableSkeleton cols={7} rows={6} /></div>;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      {header}
      <div className="stats-grid compact">
        <StatCard label="Total POBs" value={num('submitted') + num('pending_verification') + num('verified') + num('needs_review') + num('rejected') + num('duplicate')} />
        <StatCard label="Pending verification" value={num('pending_verification')} tone="amber" />
        <StatCard label="Verified" value={num('verified') + num('approved')} tone="green" />
        <StatCard label="Needs review" value={num('needs_review')} tone="amber" />
        <StatCard label="Rejected" value={num('rejected') + num('duplicate')} tone="red" />
      </div>
      {data?.unreachable?.length > 0 && (
        <p className="muted">Unreachable divisions: {data.unreachable.map((u) => u.name).join(', ')}</p>)}
      {data?.recent?.length
        ? <Table cols={cols} rows={data.recent} keyOf={(r) => `${r.division_id}-${r.id}`}
            empty={<EmptyState text="No POB records in this view" />} />
        : <EmptyState text="No POB records yet" />}

      <Modal open={!!rejecting} onClose={() => setRejecting(null)} title={`Reject POB #${rejecting?.id}`}
        footer={<>
          <button className="btn" onClick={() => setRejecting(null)}>Cancel</button>
          <button className="btn btn-danger" onClick={doReject} disabled={!reason.trim()}>Reject POB</button>
        </>}>
        <Field label="Reason" required hint="The submitting user will see this.">
          <TextArea value={reason} onChange={(e) => setReason(e.target.value)} rows={4}
            placeholder="Why is this POB being sent back?" />
        </Field>
      </Modal>
    </div>
  );
}