import { useCallback, useEffect, useState } from 'react';
import { api, downloadFile, fmtMoney, uploadFile } from '../../api';
import { ErrorBox, Field, Modal, PageHeader, SearchBox, Select, StatCard, StatusBadge, Table, TextInput, toast, useAsync } from '../../ui';

const PAGE_SIZE = 25;

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
          exist in your division), name (required), sku, composition, strength,
          dosage_form, pack, ptr, pts, mrp, gst, status.
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

const DOSAGE_FORMS = ['Tablet', 'Capsule', 'Syrup', 'Injection', 'Cream', 'Drops', 'Other']
  .map((o) => ({ value: o, label: o }));

export default function Products() {
  const [q, setQ] = useState('');
  const [search, setSearch] = useState('');
  const [brandId, setBrandId] = useState('');
  const [status, setStatus] = useState('');
  const [page, setPage] = useState(0);
  const brands = useAsync(() => api('/api/v1/brands'));
  const [editing, setEditing] = useState(null);
  const [bulk, setBulk] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => { setSearch(q); setPage(0); }, 300);
    return () => clearTimeout(t);
  }, [q]);

  const setFilter = (k) => (e) => {
    if (k === 'brandId') setBrandId(e.target.value);
    if (k === 'status') setStatus(e.target.value);
    setPage(0);
  };

  const loader = useCallback(() => {
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE });
    if (search) params.set('q', search);
    if (brandId) params.set('brand_id', brandId);
    if (status) params.set('status', status);
    return api(`/api/v1/products?${params.toString()}`);
  }, [search, brandId, status, page]);
  const { data, loading, error, run } = useAsync(loader, [search, brandId, status, page]);

  if (loading) {
    return <div><PageHeader title="Products" subtitle="Your division's master product catalogue" /><div className="stats-grid compact"><StatCard label="Products" value="…" tone="blue" /><StatCard label="Brands" value="…" tone="amber" /></div></div>;
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const total = data?.total || 0;
  const rows = data?.items || [];
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const set = (k) => (e) => {
    const v = e.target.value;
    setEditing((p) => ({ ...p, [k]: v === '' ? null : v }));
  };

  const saveProduct = (id, payload) => (id
    ? api(`/api/v1/products/${id}`, { method: 'PUT', body: payload })
    : api('/api/v1/products', { method: 'POST', body: payload }));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    const payload = { ...editing };
    delete payload.campaign_id;
    if (!editing.id && !payload.brand_id) {
      toast('Brand is required', 'error');
      setBusy(false);
      return;
    }
    try {
      try {
        await saveProduct(editing.id, payload);
      } catch (err) {
        // Pricing on a product used by an ACTIVE campaign is blocked unless
        // the admin explicitly confirms the commercial change (backend
        // guard in routers/masters.py::update_product).
        if (editing.id && err.status === 409 && /active campaign/i.test(err.message)
            && window.confirm(`${err.message}\n\nUpdate the pricing anyway?`)) {
          await saveProduct(editing.id, { ...payload, confirm_price_change: true });
        } else {
          throw err;
        }
      }
      toast(editing.id ? 'Product updated' : 'Product created', 'success');
      setEditing(null);
      run();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const toggleActive = async (row) => {
    const next = row.status === 'active' ? 'inactive' : 'active';
    try {
      await api(`/api/v1/products/${row.id}`, { method: 'PUT', body: { status: next } });
      toast(next === 'active' ? 'Activated' : 'Deactivated', 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const del = async (row) => {
    if (!window.confirm(`Delete "${row.name}"?`)) return;
    try {
      await api(`/api/v1/products/${row.id}`, { method: 'DELETE' });
      toast('Deleted', 'success');
      run();
    } catch (err) {
      if (err.status === 409) {
        toast('Product is in use — opening it for deactivation instead of delete', 'error');
        setEditing({ ...row, status: 'inactive' });
      } else {
        toast(err.message, 'error');
      }
    }
  };

  const brandOpts = (brands.data?.items || []).map((b) => ({ value: b.id, label: b.name }));

  const num = (v) => (v === '' || v === null || v === undefined ? null : Number(v));
  const setNum = (k) => (e) => setEditing((p) => ({ ...p, [k]: num(e.target.value) }));

  const cols = [
    { key: 'name', label: 'Product', render: (r) => (
      <span className="cell-link"><strong>{r.name}</strong>
        {[r.strength, r.dosage_form, r.pack].filter(Boolean).length > 0 &&
          <span className="muted cell-sub">{[r.strength, r.dosage_form, r.pack].filter(Boolean).join(' · ')}</span>}
      </span>
    ) },
    { key: 'sku', label: 'SKU', render: (r) => <code>{r.sku || '—'}</code> },
    { key: 'brand_name', label: 'Brand', render: (r) => r.brand_name
      ? <strong>{r.brand_name}</strong>
      : <span className="muted">—</span> },
    ...((rows).some((r) => r.division_name) ? [{
      key: 'division_name', label: 'Division', render: (r) => r.division_name
        ? <span>{r.division_name}</span>
        : <span className="muted">—</span>,
    }] : []),
    { key: 'ptr', label: 'PTR', thClass: 'num', tdClass: 'num', render: (r) => r.ptr ? <strong>{fmtMoney(r.ptr)}</strong> : <span className="muted">—</span> },
    { key: 'pts', label: 'PTS', thClass: 'num', tdClass: 'num', render: (r) => r.pts ? <span>{fmtMoney(r.pts)}</span> : <span className="muted">—</span> },
    { key: 'mrp', label: 'MRP', thClass: 'num', tdClass: 'num', render: (r) => r.mrp ? <span>{fmtMoney(r.mrp)}</span> : <span className="muted">—</span> },
    { key: 'gst', label: 'GST', thClass: 'num', tdClass: 'num', render: (r) => r.gst ? <span>{r.gst}%</span> : <span className="muted">—</span> },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
        {r.has_usage
          ? <button className="btn-link" title="In use — deactivate instead of deleting" onClick={() => toggleActive(r)}>
              {r.status === 'active' ? 'Deactivate' : 'Activate'}
            </button>
          : <button className="btn-link danger" onClick={() => del(r)}>Delete</button>}
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
          <button className="btn btn-primary" onClick={() => setEditing({ status: 'active' })}>+ Add product</button>
        </>} />
      <div className="stats-grid compact">
        <StatCard label="Products" value={total} tone="blue" />
        <StatCard label="Brands" value={(brands.data?.items || []).length} tone="amber" />
      </div>
      <div className="toolbar">
        <Select value={brandId} onChange={setFilter('brandId')} placeholder="All brands"
          options={brandOpts} />
        <Select value={status} onChange={setFilter('status')} placeholder="All statuses"
          options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} />
        <span className="muted" style={{ marginLeft: 'auto' }}>
          {total > 0 && <>{page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total}</>}
        </span>
      </div>
      <Table cols={cols} rows={rows} keyOf={(r) => r.id}
        empty="No products yet — add your division's products here, or import them from Excel." />
      {pages > 1 && (
        <div className="toolbar" style={{ justifyContent: 'flex-end' }}>
          <button className="btn" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>‹ Prev</button>
          <span className="muted">Page {page + 1} of {pages}</span>
          <button className="btn" disabled={page >= pages - 1} onClick={() => setPage((p) => p + 1)}>Next ›</button>
        </div>
      )}

      {editing && (
        <Modal open wide title={editing.id ? 'Edit Product' : 'New Product'} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="product-form" disabled={busy}>{busy ? 'Saving...' : 'Save'}</button>
          </>}>
          <form id="product-form" onSubmit={submit}>
            <h4 className="section-title">Product</h4>
            <div className="grid-2">
              <Field label="Brand" required hint={editing.id && editing.has_usage
                ? 'Locked — this product has campaign/POB history, so its brand cannot change'
                : 'Required — a product belongs to a brand in your division'}>
                <Select value={editing.brand_id || ''} onChange={set('brand_id')} placeholder="Select brand…"
                  options={brandOpts} disabled={editing.id && editing.has_usage} />
              </Field>
              <Field label="Product name" required><TextInput value={editing.name || ''} onChange={set('name')} required placeholder="e.g. Allegra 120mg Strip" /></Field>
              <Field label="SKU"><TextInput value={editing.sku || ''} onChange={set('sku')} placeholder="e.g. ALEG-120-10" /></Field>
              <Field label="Strength"><TextInput value={editing.strength || ''} onChange={set('strength')} placeholder="e.g. 120 mg" /></Field>
              <Field label="Dosage form">
                <Select value={editing.dosage_form || ''} onChange={set('dosage_form')} placeholder="Select…"
                  options={DOSAGE_FORMS} />
              </Field>
              <Field label="Pack"><TextInput value={editing.pack || ''} onChange={set('pack')} placeholder="e.g. 10×10" /></Field>
              <Field label="Composition"><TextInput value={editing.composition || ''} onChange={set('composition')} placeholder="e.g. Fexofenadine HCl 120 mg" /></Field>
            </div>
            <h4 className="section-title" style={{ marginTop: 18 }}>Commercial</h4>
            {editing.id && editing.has_usage && (
              <p className="muted" style={{ margin: '0 0 10px', fontSize: 12 }}>
                This product has campaign/POB history — if it's linked to an active campaign,
                changing PTR/PTS/MRP will ask for confirmation before updating the commercial basis.
              </p>
            )}
            <div className="grid-2">
              <Field label="PTR (₹)"><TextInput type="number" step="0.01" min="0" value={editing.ptr ?? ''} onChange={setNum('ptr')} /></Field>
              <Field label="PTS (₹)"><TextInput type="number" step="0.01" min="0" value={editing.pts ?? ''} onChange={setNum('pts')} /></Field>
              <Field label="MRP (₹)"><TextInput type="number" step="0.01" min="0" value={editing.mrp ?? ''} onChange={setNum('mrp')} /></Field>
              <Field label="GST (%)"><TextInput type="number" step="0.01" min="0" value={editing.gst ?? ''} onChange={setNum('gst')} /></Field>
            </div>
            <h4 className="section-title" style={{ marginTop: 18 }}>Status</h4>
            <div className="grid-2">
              <Field label="Status">
                <Select value={editing.status || 'active'} onChange={set('status')}
                  options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} />
              </Field>
            </div>
          </form>
        </Modal>
      )}
      {bulk && <BulkModal onClose={() => setBulk(false)} onDone={() => { setBulk(false); run(); }} />}
    </div>
  );
}