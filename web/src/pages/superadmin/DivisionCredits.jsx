import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api, fmtDateTime } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, ProgressBar, Select, Spinner,
  StatCard, Table, TextInput, toast, useAsync,
} from '../../ui';

const opLabel = (op) => String(op || '').replaceAll('_', ' ');
const PLANS = [
  { value: 'demo', label: 'Demo' },
  { value: 'growth', label: 'Growth' },
  { value: 'enterprise', label: 'Enterprise' },
  { value: 'custom', label: 'Custom' },
];

export default function DivisionCredits() {
  const { did } = useParams();
  return (
    <div>
      <CreditsPanel did={Number(did)} header />
    </div>
  );
}

// Shared panel: also mounted as the "Statement Credits" tab inside
// DivisionDetail so both entry points stay identical.
export function CreditsPanel({ did, header }) {
  const div = useAsync(() => api(`/api/v1/superadmin/divisions/${did}`), [did]);
  const data = useAsync(() => api(`/api/v1/superadmin/divisions/${did}/credits`), [did]);
  const [allocOpen, setAllocOpen] = useState(false);
  const [alloc, setAlloc] = useState({ amount: '', plan: 'demo' });
  const [busy, setBusy] = useState(false);

  const c = div.data;
  const wallet = data.data?.credits || null;
  const requests = data.data?.pending_requests || [];
  const ledger = data.data?.ledger || [];
  const usage = data.data?.usage_summary || {};

  const act = async (fn, msg) => {
    setBusy(true);
    try { const r = await fn(); toast(r.message || msg, 'success'); data.run(); }
    catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  const allocate = async () => {
    const n = Number(alloc.amount);
    if (!n || n <= 0) { toast('Enter a valid amount', 'error'); return; }
    setBusy(true);
    try {
      const r = await api(`/api/v1/superadmin/divisions/${did}/credits/allocate`, {
        method: 'POST', body: { amount: n, plan: alloc.plan } });
      toast(r.message || 'Credits allocated', 'success');
      setAllocOpen(false);
      setAlloc({ amount: '', plan: 'demo' });
      data.run();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  const approve = (rid, amt) => act(() => api(`/api/v1/superadmin/divisions/${did}/credits/requests/${rid}/approve`, {
    method: 'POST', body: {} }), 'Request approved');
  const reject = (rid) => act(() => api(`/api/v1/superadmin/divisions/${did}/credits/requests/${rid}/reject`, {
    method: 'POST', body: {} }), 'Request rejected');

  if (data.loading) return <Spinner label="Loading credits…" />;
  if (data.error) return <ErrorBox error={data.error} onRetry={data.run} />;

  return (
    <div>
      {header && (
        <PageHeader title={`Statement Credits — ${c?.name || `Division #${did}`}`}
          subtitle={c ? <><Badge tone={c.status}>{c.status}</Badge> · {c.code}</> : undefined}
          actions={<>
            <button className="btn btn-primary" onClick={() => setAllocOpen(true)}>+ Allocate credits</button>
            <Link className="btn" to={`/superadmin/divisions/${did}`}>← Back to division</Link>
          </>} />
      )}

      <div className="stats-grid">
        <StatCard label="Total credits" value={wallet?.total_credits ?? 0} icon="◉" tone="blue" />
        <StatCard label="Used" value={wallet?.used_credits ?? 0} icon="↳" tone="amber" />
        <StatCard label="Remaining" value={wallet?.remaining ?? 0} icon="✓" tone="green" />
        <StatCard label="Plan" value={wallet?.plan || 'demo'} icon="◎" tone="gray" />
      </div>

      {wallet && (
        <div className="card" style={{ marginTop: 12, padding: 14 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <strong>Consumption</strong>
            <span className="muted">{wallet.used_credits} of {wallet.total_credits} credits used</span>
          </div>
          <ProgressBar value={wallet.total_credits ? Math.min(100, wallet.used_credits / wallet.total_credits * 100) : 0} tone="amber" />
        </div>
      )}

      {/* Usage summary */}
      {Object.keys(usage).length > 0 && (
        <div className="card" style={{ marginTop: 12, padding: 16 }}>
          <h4 className="section-title" style={{ margin: 0 }}>Usage by operation</h4>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 10, marginTop: 10 }}>
            {Object.entries(usage).map(([op, v]) => (
              <div key={op} className="card" style={{ padding: 12 }}>
                <strong>{opLabel(op)}</strong>
                <div>{v.credits || 0} credits · {v.count || 0} operation{v.count === 1 ? '' : 's'}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Pending requests */}
      <h4 className="section-title" style={{ marginTop: 18 }}>Pending credit requests</h4>
      <Table cols={[
        { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
        { key: 'who', label: 'Requester', render: (r) => (
          <div className="cell-link"><strong>{r.requester_name || '—'}</strong>
            <span className="muted cell-sub">{r.employee_id || ''}</span></div>
        ) },
        { key: 'amount', label: 'Credits', render: (r) => <strong>{r.credits_requested}</strong>, thClass: 'num' },
        { key: 'msg', label: 'Message', render: (r) => <span className="muted">{r.message || '—'}</span> },
        { key: 'when', label: 'Requested', render: (r) => fmtDateTime(r.created_at) },
        { key: 'actions', label: '', render: (r) => (
          <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
            <button className="btn btn-sm btn-danger" disabled={busy} onClick={() => reject(r.id)}>Reject</button>
            <button className="btn btn-sm btn-primary" disabled={busy} onClick={() => approve(r.id)}>Approve</button>
          </div>
        ) },
      ]} rows={requests} keyOf={(r) => r.id} empty="No pending credit requests" />

      {/* Ledger */}
      <h4 className="section-title" style={{ marginTop: 18 }}>Ledger (recent 50)</h4>
      <Table cols={[
        { key: 'id', label: 'Tx', render: (r) => <strong>#{r.id}</strong> },
        { key: 'when', label: 'When', render: (r) => fmtDateTime(r.created_at) },
        { key: 'op', label: 'Operation', render: (r) => opLabel(r.operation_type) },
        { key: 'who', label: 'User', render: (r) => r.user_name || r.division_name || '—' },
        { key: 'detail', label: 'Detail', render: (r) => <span className="muted">{r.detail || '—'}</span> },
        { key: 'credits', label: 'Credits', render: (r) => (
          r.operation_type === 'request_approved'
            ? <strong className="pos">+{r.credits_used}</strong>
            : <span className="neg">−{Math.abs(r.credits_used)}</span>
        ), thClass: 'num' },
        { key: 'balance', label: 'Balance', render: (r) => r.balance_after ?? '—', thClass: 'num' },
      ]} rows={ledger} keyOf={(r) => r.id} empty="No credit transactions yet" />

      <Modal open={allocOpen} title="Allocate credits" onClose={() => setAllocOpen(false)}
        footer={<>
          <button className="btn" onClick={() => setAllocOpen(false)}>Cancel</button>
          <button className="btn btn-primary" disabled={busy} onClick={allocate}>
            {busy ? 'Allocating…' : 'Allocate'}
          </button>
        </>}>
        <Field label="Amount" required>
          <TextInput type="number" min={1} value={alloc.amount}
            onChange={(e) => setAlloc((a) => ({ ...a, amount: e.target.value }))} placeholder="e.g. 500" />
        </Field>
        <Field label="Plan">
          <Select options={PLANS} value={alloc.plan}
            onChange={(e) => setAlloc((a) => ({ ...a, plan: e.target.value }))} />
        </Field>
      </Modal>
    </div>
  );
}