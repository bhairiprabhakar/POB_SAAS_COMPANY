import { useMemo, useState } from 'react';
import { api, getSession } from '../../api';
import { ErrorBox, Field, Modal, PageHeader, SearchBox, Select, StatCard, StatSkeleton, TextInput, toast, useAsync } from '../../ui';

export default function Gifts() {
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const { data, loading, error, run } = useAsync(() => api('/api/v1/gifts'));

  const rows = useMemo(() => {
    let out = data?.items || [];
    if (q) {
      const n = q.toLowerCase();
      out = out.filter((r) => (r.name || '').toLowerCase().includes(n));
    }
    return out;
  }, [data, q]);

  const canManage = (getSession()?.permissions || []).includes('gratification.manage');

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (editing.id) {
        await api(`/api/v1/gifts/${editing.id}`, { method: 'PUT', body: editing });
        toast('Gift updated', 'success');
      } else {
        await api('/api/v1/gifts', { method: 'POST', body: editing });
        toast('Gift created', 'success');
      }
      setEditing(null); run();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const toggle = async (row) => {
    try {
      await api(`/api/v1/gifts/${row.id}`, { method: 'PUT', body: { active: !row.active } });
      toast(row.active ? 'Deactivated' : 'Activated', 'success'); run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const set = (k) => (e) => {
    const v = e.target.value;
    setEditing((p) => ({ ...p, [k]: k === 'active' ? v === 'true' : (v === '' ? null : v) }));
  };

  if (loading) {
    return <div><PageHeader title="Gifts" subtitle="Gratification gift master" /><StatSkeleton n={3} /></div>;
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const total = (data?.items || []).length;
  const live = (data?.items || []).filter((r) => r.active).length;
  const stockValue = (data?.items || []).reduce((s, r) => s + Number(r.cost || 0) * Number(r.stock || 0), 0);

  const num = (v) => Number(v ?? 0);

  return (
    <div>
      <PageHeader title="Gifts"
        subtitle="The gratification catalogue — set costs and stock per gift"
        actions={
          <>
            <SearchBox value={q} onChange={setQ} placeholder="Search gift…" />
            {canManage && <button className="btn btn-primary" onClick={() => setEditing({ active: true, cost: 0, stock: 0 })}>+ Add Gift</button>}
          </>
        } />
      <div className="stats-grid compact">
        <StatCard label="Gift SKUs" value={total} tone="blue" />
        <StatCard label="Active" value={live} tone="green" />
        <StatCard label="Stock value" value={`₹${stockValue.toLocaleString('en-IN')}`} tone="amber" />
      </div>
      {rows.length ? (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th><th>Cost</th><th>Stock</th><th>Stock value</th><th>Status</th><th className="actions-th" />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><strong>{r.name}</strong></td>
                <td>₹{num(r.cost)}</td>
                <td>{num(r.stock)}</td>
                <td className="muted">₹{(num(r.cost) * num(r.stock)).toLocaleString('en-IN')}</td>
                <td><span className={`badge ${r.active ? 'badge-green' : 'badge-gray'}`}>{r.active ? 'active' : 'inactive'}</span></td>
                <td className="actions-th">
                  {canManage && (
                    <span className="row-actions">
                      <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
                      <button className="btn-link" onClick={() => toggle(r)}>{r.active ? 'Deactivate' : 'Activate'}</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="muted">No gifts yet — add your first gratification gift.</p>}

      {editing && (
        <Modal open wide title={editing.id ? 'Edit Gift' : 'New Gift'} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="gift-form" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
          </>}>
          <form id="gift-form" className="grid-2" onSubmit={submit}>
            <Field label="Name" required className="span-2">
              <TextInput value={editing.name || ''} onChange={set('name')} placeholder="e.g. Promotional Water Bottle" />
            </Field>
            <Field label="Cost (₹)">
              <TextInput type="number" min="0" value={editing.cost ?? ''} onChange={set('cost')} />
            </Field>
            <Field label="Stock">
              <TextInput type="number" min="0" value={editing.stock ?? ''} onChange={set('stock')} />
            </Field>
            <Field label="Status" className="span-2">
              <Select value={editing.active ? 'true' : 'false'} onChange={set('active')}
                options={[{ value: 'true', label: 'Active' }, { value: 'false', label: 'Inactive' }]} />
            </Field>
          </form>
        </Modal>
      )}
    </div>
  );
}