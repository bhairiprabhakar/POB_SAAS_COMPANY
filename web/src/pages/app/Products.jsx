import { useMemo, useState } from 'react';
import { api, downloadFile, fmtMoney, uploadFile } from '../../api';
import { ErrorBox, Field, Modal, PageHeader, SearchBox, Select, StatCard, StatusBadge, Table, TextInput, toast, useAsync } from '../../ui';

function BulkModal({ onClose, onDone }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const upload = async (e) => {
    e.preventDefault();
    if (!file) { toast('Choose an Excel file first', 'error'); return; }
    setBusy(true);
    try {
      const r = await uploadFile('/api/v1/products/bulk-upload', file);
      setResult(r);
      if (!r.errors?.length) {
        toast(`${r.created} product(s) imported`, 'success');
        onDone();
      } else {
        toast(`${r.created} imported, ${r.errors.length} failed`, 'error');
      }
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };
  return (
    <Modal open title="Bulk import products" onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Close</button>
          <button className="btn btn-primary" form="bulk-form" disabled={busy || !file}>
            {busy ? 'Uploading...' : 'Upload'}
          </button>
        </>
      }>
      <form id="bulk-form" onSubmit={upload}>
        <p className="muted">
          Use the template with one product per row. Columns: brand (must already
          exist in your division), name (required), sku, strength, pack, ptr, pts,
          mrp, min_quantity, min_pob, max_pob, scheme_eligibility, status.
        </p>
        <button type="button" className="btn-link" onClick={() => downloadFile('/api/v1/products/bulk-template', 'products_template.xlsx')}>
          Download template
        </button>
        <Field label="Excel file (.xlsx)">
          <TextInput type="file" accept=".xlsx" onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </Field>
      </form>
      {result && (
        <div className="card" style={{ marginTop: 12 }}>
          <p><strong>{result.created}</strong> product(s) imported, <strong>{result.errors?.length || 0}</strong> error(s).</p>
          {result.errors?.length > 0 && (
            <ul className="err-list">{result.errors.map((m, i) => <li key={i}>{m}</li>)}</ul>
          )}
        </div>
      )}
    </Modal>
  );
}

export default function Products() {
  const [q, setQ] = useState('');
  const { data, loading, error, run } = useAsync(() => api('/api/v1/products'));
  const brands = useAsync(() => api('/api/v1/brands'));
  const [editing, setEditing] = useState(null);
  const [bulk, setBulk] = useState(false);
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => {
    let out = data?.items || [];
    if (q) {
      const n = q.toLowerCase();
      out = out.filter((r) => (r.name || '').toLowerCase().includes(n)
        || (r.sku || '').toLowerCase().includes(n)
        || (r.brand_name || '').toLowerCase().includes(n)
        || (r.strength || '').toLowerCase().includes(n));
    }
    return out;
  }, [data, q]);

  if (loading) {
    return <div><PageHeader title="Products" subtitle="Your division's master product catalogue" /><div className="stats-grid compact"><StatCard label="Products" value="…" tone="blue" /><StatCard label="Active" value="…" tone="green" /><StatCard label="Brands" value="…" tone="amber" /></div></div>;
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const total = (data?.items || []).length;
  const active = (data?.items || []).filter((r) => r.status === 'active').length;
  const brandSet = new Set((data?.items || []).map((r) => r.brand_name).filter(Boolean)).size;

  const set = (k) => (e) => {
    const v = e.target.value;
    setEditing((p) => ({ ...p, [k]: v === '' ? null : v }));
  };

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const payload = { ...editing };
      delete payload.campaign_id;
      if (editing.id) await api(`/api/v1/products/${editing.id}`, { method: 'PUT', body: payload });
      else await api('/api/v1/products', { method: 'POST', body: payload });
      toast(editing.id ? 'Product updated' : 'Product created', 'success');
      setEditing(null);
      run();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const del = async (row) => {
    if (!window.confirm(`Delete "${row.name}"?`)) return;
    try { await api(`/api/v1/products/${row.id}`, { method: 'DELETE' }); toast('Deleted', 'success'); run(); }
    catch (err) { toast(err.message, 'error'); }
  };

  const brandOpts = (brands.data?.items || []).map((b) => ({ value: b.id, label: b.name }));

  const num = (v) => (v === '' || v === null || v === undefined ? null : Number(v));
  const setNum = (k) => (e) => setEditing((p) => ({ ...p, [k]: num(e.target.value) }));

  const cols = [
    { key: 'name', label: 'Product', render: (r) => (
      <span className="cell-link"><strong>{r.name}</strong>
        {[r.strength, r.pack].filter(Boolean).length > 0 &&
          <span className="muted cell-sub">{[r.strength, r.pack].filter(Boolean).join(' · ')}</span>}
      </span>
    ) },
    { key: 'sku', label: 'SKU', render: (r) => <code>{r.sku || '—'}</code> },
    { key: 'brand_name', label: 'Brand', render: (r) => r.brand_name
      ? <strong>{r.brand_name}</strong>
      : <span className="muted">—</span> },
    ...((data?.items || []).some((r) => r.division_name) ? [{
      key: 'division_name', label: 'Division', render: (r) => r.division_name
        ? <span>{r.division_name}</span>
        : <span className="muted">—</span>,
    }] : []),
    { key: 'ptr', label: 'PTR', thClass: 'num', tdClass: 'num', render: (r) => r.ptr ? <strong>{fmtMoney(r.ptr)}</strong> : <span className="muted">—</span> },
    { key: 'mrp', label: 'MRP', thClass: 'num', tdClass: 'num', render: (r) => r.mrp ? <span>{fmtMoney(r.mrp)}</span> : <span className="muted">—</span> },
    { key: 'min_quantity', label: 'Min qty', thClass: 'num', tdClass: 'num', render: (r) => r.min_quantity ?? '—' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
        <button className="btn-link danger" onClick={() => del(r)}>Delete</button>
      </span>
    ) },
  ];

  return (
    <div>
      <PageHeader title="Products"
        subtitle="Your division's master product catalogue — add once, then pick them while drafting a campaign"
        actions={<>
          <SearchBox value={q} onChange={setQ} placeholder="Search name / SKU / brand…" />
          <button className="btn" onClick={() => setBulk(true)}>⬆ Import Excel</button>
          <button className="btn btn-primary" onClick={() => setEditing({ status: 'active', scheme_eligibility: true })}>+ Add product</button>
        </>} />
      <div className="stats-grid compact">
        <StatCard label="Products" value={total} tone="blue" />
        <StatCard label="Active" value={active} tone="green" />
        <StatCard label="Brands" value={brandSet} tone="amber" />
      </div>
      <Table cols={cols} rows={rows} keyOf={(r) => r.id}
        empty="No products yet — add your division's products here, or import them from Excel." />

      {editing && (
        <Modal open wide title={editing.id ? 'Edit Product' : 'New Product'} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="product-form" disabled={busy}>{busy ? 'Saving...' : 'Save'}</button>
          </>}>
          <form id="product-form" className="grid-2" onSubmit={submit}>
            <Field label="Brand" hint="Optional — attaches the product to a brand so name changes stay in sync">
              <Select value={editing.brand_id || ''} onChange={set('brand_id')} placeholder="No brand"
                options={brandOpts} />
            </Field>
            <Field label="Product name" required><TextInput value={editing.name || ''} onChange={set('name')} required placeholder="e.g. Allegra 120mg Strip" /></Field>
            <Field label="SKU"><TextInput value={editing.sku || ''} onChange={set('sku')} placeholder="e.g. ALEG-120-10" /></Field>
            <Field label="Strength"><TextInput value={editing.strength || ''} onChange={set('strength')} placeholder="e.g. 120 mg" /></Field>
            <Field label="Pack"><TextInput value={editing.pack || ''} onChange={set('pack')} placeholder="e.g. 10×10" /></Field>
            <Field label="PTR (₹)"><TextInput type="number" step="0.01" min="0" value={editing.ptr ?? ''} onChange={setNum('ptr')} /></Field>
            <Field label="PTS (₹)"><TextInput type="number" step="0.01" min="0" value={editing.pts ?? ''} onChange={setNum('pts')} /></Field>
            <Field label="MRP (₹)"><TextInput type="number" step="0.01" min="0" value={editing.mrp ?? ''} onChange={setNum('mrp')} /></Field>
            <Field label="Min qty"><TextInput type="number" min="1" value={editing.min_quantity ?? 1} onChange={setNum('min_quantity')} /></Field>
            <Field label="Min POB"><TextInput type="number" step="0.01" min="0" value={editing.min_pob ?? 0} onChange={setNum('min_pob')} /></Field>
            <Field label="Max POB"><TextInput type="number" step="0.01" min="0" value={editing.max_pob ?? ''} onChange={setNum('max_pob')} /></Field>
            <Field label="Scheme eligible">
              <label className="check">
                <input type="checkbox" checked={!!editing.scheme_eligibility}
                  onChange={(e) => setEditing((p) => ({ ...p, scheme_eligibility: e.target.checked }))} />
                Eligible for scheme
              </label>
            </Field>
            <Field label="Status">
              <Select value={editing.status || 'active'} onChange={set('status')}
                options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} />
            </Field>
          </form>
        </Modal>
      )}
      {bulk && <BulkModal onClose={() => setBulk(false)} onDone={() => { setBulk(false); run(); }} />}
    </div>
  );
}