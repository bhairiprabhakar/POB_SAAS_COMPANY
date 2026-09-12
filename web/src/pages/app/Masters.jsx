import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api, getSession } from '../../api';
import {
  ErrorBox, Field, Modal, PageHeader, SearchBox, Select, Spinner, StatusBadge,
  Table, TextArea, TextInput, toast, useAsync,
} from '../../ui';

const CONFIGS = {
  divisions: {
    title: 'Divisions', subtitle: 'Business divisions for POB campaigns',
    endpoint: '/api/v1/divisions',
    perm: 'campaign.view',
    readonly: true,
    search: (r, q) => (r.name + r.code + r.description || '').toLowerCase().includes(q),
    cols: [
      { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
      { key: 'name', label: 'Name' },
      { key: 'code', label: 'Code' },
      { key: 'campaign_count', label: 'Campaigns' },
      { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
      { key: 'description', label: 'Description' },
    ],
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'code', label: 'Code' },
      { name: 'description', label: 'Description', type: 'textarea' },
      { name: 'status', label: 'Status', type: 'select', options: ['active', 'inactive'] },
    ],
  },
  brands: {
    title: 'Brands', subtitle: 'Pharma brands used across campaigns',
    endpoint: '/api/v1/brands',
    perm: 'brand.view',
    readonly: true,
    search: (r, q) => (r.name + r.code + r.description || '').toLowerCase().includes(q),
    cols: [
      { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
      { key: 'name', label: 'Name' },
      { key: 'code', label: 'Code' },
      { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
      { key: 'description', label: 'Description' },
    ],
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'code', label: 'Code' },
      { name: 'description', label: 'Description', type: 'textarea' },
      { name: 'status', label: 'Status', type: 'select', options: ['active', 'inactive'] },
    ],
  },
  products: {
    title: 'Products', subtitle: "Your division's master product catalogue",
    endpoint: '/api/v1/products',
    perm: 'product.view',
    search: (r, q) => (r.name + r.sku + (r.brand_name || '')).toLowerCase().includes(q),
    cols: [
      { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
      { key: 'name', label: 'Name' },
      { key: 'sku', label: 'SKU' },
      { key: 'brand_name', label: 'Brand' },
      { key: 'ptr', label: 'PTR', render: (r) => `₹${r.ptr || 0}` },
      { key: 'min_quantity', label: 'Min qty' },
      { key: 'max_pob', label: 'Max POB' },
      { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    ],
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'brand_id', label: 'Brand', type: 'select', ref: '/api/v1/brands' },
      { name: 'sku', label: 'SKU' },
      { name: 'strength', label: 'Strength' },
      { name: 'pack', label: 'Pack' },
      { name: 'ptr', label: 'PTR (₹)', type: 'number' },
      { name: 'mrp', label: 'MRP (₹)', type: 'number' },
      { name: 'min_quantity', label: 'Min quantity', type: 'number' },
      { name: 'min_pob', label: 'Min POB (₹)', type: 'number' },
      { name: 'max_pob', label: 'Max POB (₹)', type: 'number' },
      { name: 'status', label: 'Status', type: 'select', options: ['active', 'inactive'] },
    ],
  },
  chemists: {
    title: 'Chemists', subtitle: 'Retail chemists / pharmacies in your territory network',
    endpoint: '/api/v1/chemists',
    perm: 'chemist.manage',
    search: (r, q) => (r.name + r.shop_name + r.mobile + r.city + r.gst || '').toLowerCase().includes(q),
    cols: [
      { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
      { key: 'name', label: 'Name' },
      { key: 'shop_name', label: 'Shop' },
      { key: 'mobile', label: 'Mobile' },
      { key: 'city', label: 'City' },
      { key: 'state', label: 'State' },
      { key: 'gst', label: 'GST' },
      { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    ],
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'shop_name', label: 'Shop name' },
      { name: 'owner_name', label: 'Owner' },
      { name: 'mobile', label: 'Mobile' },
      { name: 'alternate_mobile', label: 'Alternate mobile' },
      { name: 'email', label: 'Email' },
      { name: 'gst', label: 'GST' },
      { name: 'dl_number', label: 'Drug licence no.' },
      { name: 'address', label: 'Address' },
      { name: 'city', label: 'City' },
      { name: 'district', label: 'District' },
      { name: 'state', label: 'State' },
      { name: 'pin', label: 'PIN' },
      { name: 'ocid', label: 'OCID' },
      { name: 'doctor_name', label: 'Doctor' },
      { name: 'category', label: 'Category' },
      { name: 'status', label: 'Status', type: 'select', options: ['active', 'inactive'] },
    ],
  },
  gifts: {
    title: 'Gifts', subtitle: 'Physical gift inventory for gratification dispatch',
    endpoint: '/api/v1/gifts',
    perm: 'gratification.manage',
    search: (r, q) => (r.name || '').toLowerCase().includes(q),
    cols: [
      { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
      { key: 'name', label: 'Name' },
      { key: 'cost', label: 'Cost', render: (r) => `₹${r.cost || 0}` },
      { key: 'stock', label: 'Stock', render: (r) => <strong>{r.stock}</strong> },
      { key: 'active', label: 'Active', render: (r) => <StatusBadge value={r.active ? 'active' : 'inactive'} /> },
    ],
    fields: [
      { name: 'name', label: 'Name', required: true },
      { name: 'cost', label: 'Cost (₹)', type: 'number' },
      { name: 'stock', label: 'Stock', type: 'number' },
    ],
  },
};

export default function Masters() {
  const { kind } = useParams();
  const cfg = CONFIGS[kind];
  if (!cfg) return <ErrorBox error="Unknown master type" />;
  return <CrudPage cfg={cfg} />;
}

function CrudPage({ cfg }) {
  const { data, loading, error, run } = useAsync(() => api(cfg.endpoint));
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState(null); // null | {} | row
  const [refData, setRefData] = useState({});
  const [busy, setBusy] = useState(false);
  const canManage = (getSession()?.permissions || []).includes(cfg.perm) && !cfg.readonly;

  const refs = useMemo(() => {
    const seen = new Set();
    cfg.fields.filter((f) => f.type === 'select' && f.ref).forEach((f) => seen.add(f.ref));
    return [...seen];
  }, [cfg]);

  useMemo(() => {
    Promise.all(refs.map((r) => api(r).then((d) => [r, d.items || []])))
      .then((entries) => setRefData(Object.fromEntries(entries)))
      .catch(() => {});
  }, [refs.join('|')]);

  if (loading) return <Spinner label="Loading…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const rows = (data?.items || []).filter((r) => !q || cfg.search(r, q.toLowerCase()));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (editing.id) {
        await api(`${cfg.endpoint}/${editing.id}`, { method: 'PUT', body: editing });
        toast('Updated', 'success');
      } else {
        await api(cfg.endpoint, { method: 'POST', body: editing });
        toast('Created', 'success');
      }
      setEditing(null); run();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const del = async (row) => {
    if (!window.confirm(`Delete ${row.name || row[cfg.fields[0].name]}?`)) return;
    try {
      await api(`${cfg.endpoint}/${row.id}`, { method: 'DELETE' });
      toast('Deleted', 'success'); run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const set = (k) => (e) => {
    const v = e.target.value;
    setEditing((p) => ({ ...p, [k]: v === '' ? null : v }));
  };

  return (
    <div>
      <PageHeader title={cfg.title} subtitle={cfg.subtitle}
        actions={
          <>
            <SearchBox value={q} onChange={setQ} />
            {canManage && <button className="btn btn-primary" onClick={() => setEditing({})}>+ Add</button>}
          </>
        } />
      <Table cols={[...cfg.cols,
        ...(canManage ? [{ key: '_a', label: '', thClass: 'actions-th',
          render: (r) => (
            <span className="row-actions">
              <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
              <button className="btn-link danger" onClick={() => del(r)}>Delete</button>
            </span>
          ) }] : []),
      ]} rows={rows} keyOf={(r) => r.id} empty="No records" />

      {editing && (
        <Modal open wide title={editing.id ? `Edit ${cfg.title}` : `New ${cfg.title}`} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="crud-form" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
          </>}>
          <form id="crud-form" className="grid-2" onSubmit={submit}>
            {cfg.fields.map((f) => {
              if (f.type === 'textarea') {
                return (
                  <Field key={f.name} label={f.label} required={f.required} className="span-2">
                    <TextArea rows={3} value={editing[f.name] || ''} onChange={set(f.name)} />
                  </Field>
                );
              }
              if (f.type === 'select' && f.ref) {
                const opts = (refData[f.ref] || []).map((x) => ({
                  value: x.id, label: x.name || `${x.brand_name}`, _x: x,
                })).filter((o) => o.label);
                return (
                  <Field key={f.name} label={f.label} required={f.required}>
                    <Select value={editing[f.name] || ''} onChange={set(f.name)} options={opts} />
                  </Field>
                );
              }
              if (f.type === 'select') {
                return (
                  <Field key={f.name} label={f.label} required={f.required}>
                    <Select value={editing[f.name] || ''} onChange={set(f.name)}
                      options={(f.options || []).map((o) => ({ value: o, label: o }))} />
                  </Field>
                );
              }
              return (
                <Field key={f.name} label={f.label} required={f.required}>
                  <TextInput type={f.type || 'text'} value={editing[f.name] ?? ''} onChange={set(f.name)} />
                </Field>
              );
            })}
          </form>
        </Modal>
      )}
    </div>
  );
}
