import { useState } from 'react';
import { api, fmtDate, fmtMoney } from '../../api';
import {
  ErrorBox, Field, Modal, PageHeader, SearchBox, Select, Spinner, StatCard,
  Table, TextArea, TextInput, toast, useAsync,
} from '../../ui';

export default function Visits({ demo }) {
  const list = useAsync(() => api('/api/v1/visits'));
  const summary = useAsync(() => api('/api/v1/visits/summary'));
  const due = useAsync(() => api('/api/v1/visits/due'));
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState(null);
  const [showDue, setShowDue] = useState(false);

  if (demo) return <VisitsDemo />;

  const rows = (list.data?.items || []).filter((r) =>
    !q || [r.chemist_name, r.shop_name, r.city, r.recorded_by, r.campaign_name]
      .some((v) => String(v || '').toLowerCase().includes(q.toLowerCase())),
  );

  const s = summary.data;
  const dueItems = due.data?.items || [];

  return (
    <div>
      <PageHeader title="Chemist Visits"
        subtitle="15-day follow-up calls and stock liquidation tracking for chemists with verified POB activity"
        actions={
          <>
            <button className="btn" onClick={() => setShowDue(true)}>📅 Due ({dueItems.length})</button>
            <button className="btn btn-primary" onClick={() => setEditing({})}>+ Record visit</button>
          </>
        } />
      <div className="stats-grid">
        <StatCard label="Visits recorded" value={s?.visits ?? '—'} icon="📅" />
        <StatCard label="Chemists visited" value={s?.chemists_visited ?? '—'} icon="▣" />
        <StatCard label="Stock placed" value={s ? fmtMoney(s.opening_stock) : '—'} icon="📦" />
        <StatCard label="Sold / liquidated" value={s ? fmtMoney(s.quantity_sold) : '—'} icon="✓" tone="green" />
        <StatCard label="Liquidation" value={s ? `${s.liquidation_pct ?? 0}%` : '—'} icon="↻" tone="green" />
        <StatCard label="Due for follow-up" value={s?.due_chemists ?? '—'} icon="⏰" tone="amber" />
        <StatCard label="Overdue" value={s?.overdue_chemists ?? '—'} icon="✕" tone="red" />
        <StatCard label="Fresh purchase" value={s ? fmtMoney(s.fresh_purchase) : '—'} icon="＋" tone="blue" />
      </div>

      <div className="toolbar">
        <SearchBox value={q} onChange={setQ} placeholder="Search chemist / shop / city / PSR…" />
      </div>

      {list.loading && <Spinner label="Loading visits…" />}
      {list.error && <ErrorBox error={list.error} onRetry={list.run} />}
      {!list.loading && !list.error && (
        <Table cols={[
          { key: 'id', label: '#', render: (r) => <strong>#{r.id}</strong> },
          { key: 'visit_date', label: 'Date', render: (r) => fmtDate(r.visit_date) },
          { key: 'chemist_name', label: 'Chemist', render: (r) => <>{r.chemist_name}<small className="muted"> {r.shop_name || ''}</small></> },
          { key: 'city', label: 'City', render: (r) => `${r.city || '—'} · ${r.state || ''}` },
          { key: 'recorded_by', label: 'PSR' },
          { key: 'campaign_name', label: 'Campaign', render: (r) => r.campaign_name || '—' },
          { key: 'opening_stock', label: 'Opening', render: (r) => fmtMoney(r.opening_stock) },
          { key: 'quantity_sold', label: 'Sold', render: (r) => fmtMoney(r.quantity_sold) },
          { key: 'current_stock', label: 'Current', render: (r) => fmtMoney(r.current_stock) },
          { key: 'fresh_purchase', label: 'Fresh', render: (r) => fmtMoney(r.fresh_purchase) },
          { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
            <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
          ) },
        ]} rows={rows} keyOf={(r) => r.id} empty="No visits recorded yet" />
      )}

      {showDue && (
        <Modal open wide title="Chemists due for follow-up" onClose={() => setShowDue(false)}
          footer={<button className="btn" onClick={() => setShowDue(false)}>Close</button>}>
          {due.loading && <Spinner label="Loading due list…" />}
          {due.error && <ErrorBox error={due.error} onRetry={due.run} />}
          {!due.loading && !due.error && (
            <Table cols={[
              { key: 'name', label: 'Chemist', render: (r) => <>{r.name}<small className="muted"> {r.shop_name || ''}</small></> },
              { key: 'city', label: 'City', render: (r) => `${r.city || '—'} · ${r.state || ''}` },
              { key: 'mobile', label: 'Mobile' },
              { key: 'pobs', label: 'POBs' },
              { key: 'amount', label: 'Value', render: (r) => fmtMoney(r.amount) },
              { key: 'last_pob_date', label: 'Last POB', render: (r) => fmtDate(r.last_pob_date) },
              { key: 'last_visit_date', label: 'Last visit', render: (r) => fmtDate(r.last_visit_date) },
              { key: 'days_due', label: 'Days due', render: (r) => (
                <strong className={r.overdue ? 'text-danger' : ''}>{r.days_due}d</strong>
              ) },
              { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
                <button className="btn btn-sm" onClick={() => { setShowDue(false); setEditing({ chemist_id: r.chemist_id }); }}>
                  Record visit
                </button>
              ) },
            ]} rows={dueItems} keyOf={(r) => r.chemist_id} empty="No chemists due — great!" />
          )}
        </Modal>
      )}

      {editing && <VisitModal key={editing.id ?? 'new'} row={editing}
        onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); list.run(); summary.run(); due.run(); }} />}
    </div>
  );
}

function VisitsDemo() {
  const [editing, setEditing] = useState(null);
  return (
    <div>
      <div className="scope-banner" data-tone="amber">
        <span className="scope-dot" />
        <div>
          <strong>Test mode — how your team records a chemist visit</strong>
          <small>Live preview. The visit is not saved — use the 'Record visit' button to try it.</small>
        </div>
      </div>
      <PageHeader title="Chemist Visits"
        subtitle="15-day follow-up calls and stock liquidation tracking for chemists with verified POB activity"
        actions={<button className="btn btn-primary" onClick={() => setEditing({})}>+ Record visit (test)</button>} />
      <div className="card">
        <h4 className="section-title">What happens here</h4>
        <ul className="kv-list" style={{ listStyle: 'disc', paddingLeft: 18, gap: 4 }}>
          <li><span>When a PSR verifies a POB for a chemist, the chemist becomes eligible for a follow-up visit after 15 days.</span></li>
          <li><span>The PSR records the visit: stock placed at the last call, sold / liquidated, current stock in hand and fresh purchase.</span></li>
          <li><span>The liquidation rate is computed automatically and used to trigger further POB business.</span></li>
          <li><span>Try the form below — click 'Record visit', fill it in and press Record. Nothing is saved in test mode.</span></li>
        </ul>
      </div>
      {editing && <VisitModal key="demo" row={editing} demo
        onClose={() => setEditing(null)} onSaved={() => setEditing(null)} />}
    </div>
  );
}

function VisitModal({ row, onClose, onSaved, demo }) {
  const isEdit = !!row.id;
  const chemists = useAsync(() => api('/api/v1/chemists?status=active'));
  const campaigns = useAsync(() => api('/api/v1/campaigns'));
  const [f, setF] = useState({
    chemist_id: row.chemist_id ?? '',
    campaign_id: row.campaign_id ?? '',
    visit_date: row.visit_date || new Date().toISOString().slice(0, 10),
    opening_stock: row.opening_stock ?? '',
    quantity_sold: row.quantity_sold ?? '',
    current_stock: row.current_stock ?? '',
    fresh_purchase: row.fresh_purchase ?? '',
    remarks: row.remarks ?? '',
  });
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    if (!f.chemist_id) { toast('Chemist is required', 'error'); return; }
    setBusy(true);
    try {
      if (demo) {
        await new Promise((r) => setTimeout(r, 400));
        toast('Test mode: visit details are valid and would be recorded. Nothing was saved.', 'success');
        onClose();
        return;
      }
      await api(`/api/v1/visits${isEdit ? `/${row.id}` : ''}`, { method: isEdit ? 'PUT' : 'POST', body: f });
      toast(isEdit ? 'Visit updated' : 'Visit recorded', 'success');
      onSaved();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const opening = Number(f.opening_stock) || 0;
  const sold = Number(f.quantity_sold) || 0;
  const current = Number(f.current_stock) || 0;
  const liquidation = opening ? Math.round(sold / opening * 100) : 0;

  return (
    <Modal open wide title={isEdit ? `Edit visit #${row.id}` : 'Record chemist visit'} onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" form="visit-form" disabled={busy}>
            {busy ? 'Saving…' : isEdit ? 'Save changes' : 'Record visit'}
          </button>
        </>
      }>
      <form id="visit-form" className="grid-2" onSubmit={submit}>
        <Field label="Chemist" required>
          <Select value={f.chemist_id} onChange={set('chemist_id')} required
            options={(chemists.data?.items || []).map((c) => ({
              value: c.id, label: `${c.name}${c.shop_name ? ` — ${c.shop_name}` : ''}${c.city ? ` (${c.city})` : ''}`,
            }))} />
        </Field>
        <Field label="Campaign" hint="Optional">
          <Select value={f.campaign_id} onChange={set('campaign_id')}
            options={(campaigns.data?.items || []).map((c) => ({ value: c.id, label: c.name }))} />
        </Field>
        <Field label="Visit date"><TextInput type="date" value={f.visit_date} onChange={set('visit_date')} /></Field>
        <Field label="Remarks"><TextArea value={f.remarks} onChange={set('remarks')} /></Field>
        <Field label="Stock placed at last call" hint="Opening stock (units)">
          <TextInput type="number" min="0" step="0.01" value={f.opening_stock} onChange={set('opening_stock')} />
        </Field>
        <Field label="Sold / liquidated"><TextInput type="number" min="0" step="0.01" value={f.quantity_sold} onChange={set('quantity_sold')} /></Field>
        <Field label="Current stock in hand"><TextInput type="number" min="0" step="0.01" value={f.current_stock} onChange={set('current_stock')} /></Field>
        <Field label="Fresh purchase (units)"><TextInput type="number" min="0" step="0.01" value={f.fresh_purchase} onChange={set('fresh_purchase')} /></Field>
      </form>
      {opening > 0 && (
        <div className="card" style={{ marginTop: 12 }}>
          <p className="muted">
            Liquidation rate: <strong>{liquidation}%</strong> of opening stock sold;
            {current > 0 && <> balance to clear: <strong>{fmtMoney(current)}</strong> units.</>}
          </p>
        </div>
      )}
    </Modal>
  );
}
