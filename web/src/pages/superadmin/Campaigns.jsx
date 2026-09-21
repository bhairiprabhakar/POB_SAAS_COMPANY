import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../../api';
import {
  Badge, EmptyState, ErrorBox, Field, Modal, PageHeader, SearchBox,
  StatCard, StatSkeleton, Table, TableSkeleton, Tabs, TextArea, toast, useAsync,
} from '../../ui';

const TABS = [
  { id: '', label: 'All' },
  { id: 'pending_approval', label: 'Pending Approval' },
  { id: 'changes_required', label: 'Changes Requested' },
  { id: 'active', label: 'Active' },
  { id: 'completed', label: 'Completed' },
  { id: 'draft', label: 'Draft' },
  { id: 'rejected', label: 'Rejected' },
];

export default function SuperCampaigns() {
  const [params] = useSearchParams();
  const [tab, setTab] = useState(() => {
    const t = params.get('status');
    return TABS.some((x) => x.id === t) ? t : '';
  });
  const [q, setQ] = useState('');
  const { data, loading, error, run } = useAsync(
    () => api(`/api/v1/superadmin/campaigns${tab ? `?status=${tab}` : ''}`), [tab]);

  const [rejecting, setRejecting] = useState(null);
  const [reason, setReason] = useState('');
  const [requestingChanges, setRequestingChanges] = useState(null);
  const [changesReason, setChangesReason] = useState('');
  const counts = data?.counts || {};

  const rows = useMemo(() => {
    const items = data?.items || [];
    if (!q) return items;
    const n = q.toLowerCase();
    return items.filter((c) =>
      (c.name || '').toLowerCase().includes(n) ||
      (c.division_name || '').toLowerCase().includes(n) ||
      String(c.id).includes(n));
  }, [data, q]);

  const approve = async (r) => {
    try {
      await api(`/api/v1/superadmin/divisions/${r.division_id}/campaigns/${r.id}/approve`, { method: 'POST' });
      toast(`Campaign #${r.id} approved and scheduled`, 'success');
      run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const doReject = async () => {
    if (!reason.trim()) { toast('A rejection reason is required', 'error'); return; }
    try {
      await api(`/api/v1/superadmin/divisions/${rejecting.division_id}/campaigns/${rejecting.id}/reject`,
        { method: 'POST', body: JSON.stringify({ reason }) });
      toast(`Campaign #${rejecting.id} rejected`, 'success');
      setRejecting(null); setReason(''); run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const doRequestChanges = async () => {
    if (!changesReason.trim()) { toast('A reason is required', 'error'); return; }
    try {
      await api(`/api/v1/superadmin/divisions/${requestingChanges.division_id}/campaigns/${requestingChanges.id}/request-changes`,
        { method: 'POST', body: JSON.stringify({ reason: changesReason }) });
      toast(`Campaign #${requestingChanges.id} sent back for changes`, 'success');
      setRequestingChanges(null); setChangesReason(''); run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const summary = (r) => r.assignment || null;

  // Custom field labels are per-division -- fetched lazily, once per
  // division actually shown, so resolving them here never blocks the list.
  const [templatesByDiv, setTemplatesByDiv] = useState({});
  useEffect(() => {
    const dids = [...new Set((data?.items || []).map((r) => r.division_id)
      .filter((d) => d && !(d in templatesByDiv)))];
    if (!dids.length) return;
    dids.forEach((did) => {
      api(`/api/v1/superadmin/divisions/${did}/campaign-field-templates`)
        .then((res) => setTemplatesByDiv((p) => ({ ...p, [did]: res.items || [] })))
        .catch(() => setTemplatesByDiv((p) => ({ ...p, [did]: [] })));
    });
  }, [data]);

  const customFieldsSummary = (r) => {
    const cf = r.custom_fields || {};
    const keys = Object.keys(cf);
    if (!keys.length) return null;
    const tpls = templatesByDiv[r.division_id] || [];
    return keys.map((k) => {
      const t = tpls.find((x) => x.field_key === k);
      const v = cf[k];
      const disp = v && typeof v === 'object' ? (v.filename || `${v.from || ''} → ${v.to || ''}`) : String(v ?? '');
      return `${t?.label || k}: ${disp}`;
    });
  };

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'name', label: 'Campaign', render: (r) =>
      <Link to={`/superadmin/divisions/${r.division_id}`} onClick={(e) => e.stopPropagation()}
        title="Open the division console">
        {r.name}<br /><span className="muted">{r.brand_names?.length ? r.brand_names.join(', ') : (r.brand_name || '')}</span>
      </Link> },
    { key: 'division_name', label: 'Division', render: (r) =>
      <Link to={`/superadmin/divisions/${r.division_id}`} onClick={(e) => e.stopPropagation()}
        className="chip">{r.division_name}</Link> },
    { key: 'status', label: 'Status', render: (r) => <Badge tone={r.status}>{r.status.replaceAll('_', ' ')}</Badge> },
    { key: 'product_count', label: 'Products', render: (r) => r.product_count ?? '—' },
    { key: 'pob_count', label: 'POBs', render: (r) => r.pob_count ?? '—' },
    { key: 'execution', label: 'Execution', render: (r) => {
      const s = summary(r);
      if (!s) return '—';
      if (s.open) return 'Everyone (open)';
      return `${s.mode}: ${s.assigned_count ?? 0} user(s)`; 
    } },
    {
      key: 'dates', label: 'Window', render: (r) =>
        <>
          <div>{r.start_date || '—'} → {r.end_date || '…'}</div>
          {r.submitted_at && <small className="muted">submitted {String(r.submitted_at).slice(0, 10)}</small>}
        </>,
    },
    {
      key: 'custom_fields', label: 'Custom fields', render: (r) => {
        const lines = customFieldsSummary(r);
        if (!lines) return <span className="muted">—</span>;
        return <span className="chip" title={lines.join('\n')}>{lines.length} field{lines.length > 1 ? 's' : ''}</span>;
      },
    },
    {
      key: '_actions', label: '', render: (r) => r.status === 'pending_approval' ? (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap', maxWidth: 220 }}>
          <button className="btn btn-sm btn-primary" onClick={() => approve(r)}>Approve</button>
          <button className="btn btn-sm" title="Send this campaign back to the division admin for changes"
            onClick={() => setRequestingChanges(r)}>Send Back</button>
          <button className="btn btn-sm btn-danger" onClick={() => setRejecting(r)}>Reject</button>
        </div>
      ) : r.status === 'rejected' ? (
        <small className="muted" title={r.rejection_note || ''}>{r.rejection_note || 'Rejected'}</small>
      ) : r.status === 'changes_required' ? (
        <small className="muted" title={r.changes_required_note || ''}>{r.changes_required_note || 'Changes requested'}</small>
      ) : <span className="muted">—</span>,
    },
  ];

  const total = ['draft', 'pending_approval', 'changes_required', 'scheduled', 'active', 'completed', 'paused', 'rejected']
    .reduce((s, k) => s + (Number(counts[k]) || 0), 0);

  const header = (
    <PageHeader title="All Campaigns"
      subtitle="Every campaign across your divisions, including the approval queue."
      actions={
        <>
          <SearchBox value={q} onChange={setQ} placeholder="Search name / division…" />
          <Tabs items={TABS.map((t) => ({ ...t, value: t.id, badge: counts[t.id] }))} active={tab} onChange={setTab} />
        </>
      } />
  );

  if (loading) {
    return <div>{header}<StatSkeleton n={4} /><TableSkeleton cols={8} rows={6} /></div>;
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      {header}
      <div className="stats-grid compact">
        <StatCard label="Total campaigns" value={total} />
        <StatCard label="Pending approval" value={counts.pending_approval || 0} tone="amber" />
        <StatCard label="Changes requested" value={counts.changes_required || 0} tone="amber" />
        <StatCard label="Active" value={(counts.active || 0) + (counts.scheduled || 0)} tone="green" />
        <StatCard label="Rejected" value={counts.rejected || 0} tone="red" />
      </div>
      {data?.unreachable?.length > 0 && (
        <p className="muted">Unreachable divisions: {data.unreachable.map((u) => u.name).join(', ')}</p>)}
      {rows.length
        ? <Table cols={cols} rows={rows} keyOf={(r) => `${r.division_id}-${r.id}`}
            empty={<EmptyState text="No campaigns match this view" />} />
        : <EmptyState text="No campaigns in this view yet" />}

      <Modal open={!!rejecting} onClose={() => setRejecting(null)} title={`Reject campaign #${rejecting?.id}`}
        footer={<>
          <button className="btn" onClick={() => setRejecting(null)}>Cancel</button>
          <button className="btn btn-danger" onClick={doReject} disabled={!reason.trim()}>Reject campaign</button>
        </>}>
        <Field label="Reason" required hint="The division admin will see this and can resubmit after fixing.">
          <TextArea value={reason} onChange={(e) => setReason(e.target.value)} rows={4} placeholder="Why is this campaign being sent back?" />
        </Field>
      </Modal>

      <Modal open={!!requestingChanges} onClose={() => setRequestingChanges(null)}
        title={`Send back campaign #${requestingChanges?.id} for changes`}
        footer={<>
          <button className="btn" onClick={() => setRequestingChanges(null)}>Cancel</button>
          <button className="btn btn-primary" onClick={doRequestChanges} disabled={!changesReason.trim()}>Send back</button>
        </>}>
        <Field label="Reason" required hint="The division admin will see this, fix it, and resubmit for approval — this doesn't count as a rejection.">
          <TextArea value={changesReason} onChange={(e) => setChangesReason(e.target.value)} rows={4} placeholder="What needs to change before this can be approved?" />
        </Field>
      </Modal>
    </div>
  );
}