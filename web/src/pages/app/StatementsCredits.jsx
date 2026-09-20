import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, fmtDateTime, uploadFile } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, ProgressBar, Spinner, StatCard,
  Table, TextArea, TextInput, toast, useAsync,
} from '../../ui';

const opLabel = (op) => String(op || '').replaceAll('_', ' ');

export default function StatementsCredits() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/statements/credits'));
  const [requestOpen, setRequestOpen] = useState(false);
  const [amount, setAmount] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);

  const wallet = data?.credits || null;
  const ledger = data?.recent_transactions || [];

  const submitRequest = async () => {
    const n = Number(amount);
    if (!n || n <= 0) { toast('Enter a valid credit amount', 'error'); return; }
    setBusy(true);
    try {
      const r = await uploadFile('/api/v1/statements/credits/request', null, {
        credits_requested: String(n), message,
      });
      toast(r.message || 'Credit request submitted', 'success');
      setRequestOpen(false);
      setAmount('');
      setMessage('');
      run();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  const cols = [
    { key: 'id', label: 'Tx', render: (r) => <strong>#{r.id}</strong> },
    { key: 'when', label: 'When', render: (r) => fmtDateTime(r.created_at) },
    { key: 'op', label: 'Operation', render: (r) => opLabel(r.operation_type) },
    { key: 'user', label: 'User', render: (r) => r.user_name || r.division_name || '—' },
    { key: 'detail', label: 'Detail', render: (r) => <span className="muted">{r.detail || '—'}</span> },
    { key: 'credits', label: 'Credits', render: (r) => (
      r.operation_type === 'request_approved'
        ? <strong className="pos">+{r.credits_used}</strong>
        : <span className="neg">−{Math.abs(r.credits_used)}</span>
    ), thClass: 'num' },
    { key: 'balance', label: 'Balance', render: (r) => r.balance_after ?? '—', thClass: 'num' },
  ];

  return (
    <div>
      <PageHeader title="Statement Credits"
        subtitle="Your division's wallet for AI statement extraction."
        actions={<>
          <Link className="btn" to="/app/statements">← Back to statements</Link>
          <button className="btn btn-primary" onClick={() => setRequestOpen(true)}>+ Request credits</button>
        </>} />

      {loading ? <Spinner /> : error ? <ErrorBox error={error} onRetry={run} /> : (
        <div>
          <div className="stats-grid">
            <StatCard label="Total credits" value={wallet.total_credits ?? 0} icon="◉" tone="blue" />
            <StatCard label="Used" value={wallet.used_credits ?? 0} icon="↳" tone="amber" />
            <StatCard label="Remaining" value={wallet.remaining ?? 0} icon="✓" tone="green" />
            <StatCard label="Plan" value={wallet.plan || 'demo'} icon="◎" tone="gray" />
          </div>

          <div className="card" style={{ marginTop: 12, padding: 14 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
              <strong>Consumption</strong>
              <span className="muted">{wallet.used_credits} of {wallet.total_credits} credits used</span>
            </div>
            <ProgressBar value={wallet.total_credits ? Math.min(100, wallet.used_credits / wallet.total_credits * 100) : 0} tone="amber" />
            <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
              One credit is consumed per statement extracted. If you run out, file a request — your company admin (or the platform team) will review it.
            </p>
          </div>

          <h4 className="section-title" style={{ marginTop: 18 }}>Recent transactions</h4>
          <Table cols={cols} rows={ledger} keyOf={(r) => r.id}
            empty="No credit transactions yet" />
        </div>
      )}

      <Modal open={requestOpen} title="Request more credits" onClose={() => setRequestOpen(false)}
        footer={<>
          <button className="btn" onClick={() => setRequestOpen(false)}>Cancel</button>
          <button className="btn btn-primary" disabled={busy} onClick={submitRequest}>
            {busy ? 'Submitting…' : 'Submit request'}
          </button>
        </>}>
        <Field label="Credits requested" required hint="How many statement extractions do you need?">
          <TextInput type="number" min={1} value={amount} onChange={(e) => setAmount(e.target.value)}
            placeholder="e.g. 100" />
        </Field>
        <Field label="Note for the reviewer (optional)">
          <TextArea rows={3} value={message} onChange={(e) => setMessage(e.target.value)}
            placeholder="Why do you need more credits?" />
        </Field>
        {wallet && (
          <p className="muted" style={{ fontSize: 12 }}>
            Wallet: <strong>{wallet.remaining}</strong> remaining of {wallet.total_credits}.
          </p>
        )}
      </Modal>
    </div>
  );
}