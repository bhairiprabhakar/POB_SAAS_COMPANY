import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, fmtDateTime } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, SearchBox,
  StatCard, StatSkeleton, Table, TableSkeleton, TextInput, toast, useAsync,
} from '../../ui';

const loginUrl = (code) => (code ? `${window.location.origin}/login/${encodeURIComponent(code)}` : null);

export default function Divisions() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/superadmin/divisions'));
  const nav = useNavigate();
  const [q, setQ] = useState('');
  const [showCreate, setShowCreate] = useState(false);

  const rows = useMemo(() => {
    const items = data?.items || [];
    if (!q) return items;
    const needle = q.toLowerCase();
    return items.filter((c) =>
      c.name?.toLowerCase().includes(needle) || c.code?.toLowerCase().includes(needle));
  }, [data, q]);

  const header = (
    <PageHeader title="Divisions" subtitle="Provision and manage the divisions of your organisation"
      actions={
        <>
          <SearchBox value={q} onChange={setQ} placeholder="Search name / code…" />
          <button className="btn btn-primary" onClick={() => setShowCreate(true)}>+ New division</button>
        </>
      } />
  );

  if (loading) {
    return (
      <div>
        {header}
        <StatSkeleton n={3} />
        <TableSkeleton cols={6} rows={6} />
      </div>
    );
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const act = async (fn, msg) => {
    try { await fn(); toast(msg, 'success'); run(); } catch (e) { toast(e.message, 'error'); }
  };

  const cols = [
    { key: 'code', label: 'Code', render: (r) => <strong>{r.code}</strong> },
    { key: 'name', label: 'Division' },
    { key: 'status', label: 'Status', render: (r) => <Badge tone={r.status}>{r.status}</Badge> },
    { key: 'tenant_db_name', label: 'Tenant DB', render: (r) => r.tenant_db_name ? <code>{r.tenant_db_name}</code> : '—' },
    {
      key: 'login', label: 'Sign-in link', render: (r) => r.code
        ? <a href={loginUrl(r.code)} target="_blank" rel="noreferrer"
            onClick={(e) => e.stopPropagation()}>/login/{r.code}</a>
        : '—',
    },
    { key: 'created_at', label: 'Created', render: (r) => fmtDateTime(r.created_at) },
  ];

  return (
    <div>
      {header}

      <div className="stats-grid compact">
        <StatCard label="Total" value={rows.length} />
        <StatCard label="Active" value={rows.filter((r) => r.status === 'active').length} tone="green" />
        <StatCard label="Provisioned" value={rows.filter((r) => r.tenant_db_name).length} tone="blue" />
      </div>

      <Table cols={cols} rows={rows} keyOf={(r) => r.id}
        onRowClick={(r) => nav(`/superadmin/divisions/${r.id}`)} />

      <CreateDivision open={showCreate} onClose={() => setShowCreate(false)}
        onDone={() => { setShowCreate(false); run(); }} />
    </div>
  );
}

function CreateDivision({ open, onClose, onDone }) {
  const [f, setF] = useState({ provision: true });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!open) { setBusy(false); setError(null); setF({ provision: true }); }
  }, [open]);
  const set = (k) => (e) => {
    const v = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setF((p) => ({ ...p, [k]: v }));
  };
  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (f.provision && (f.admin_password || '').length < 6) {
      const msg = 'Admin password must be at least 6 characters';
      setError(msg); toast(msg, 'error');
      return;
    }
    setBusy(true);
    try {
      await api('/api/v1/superadmin/divisions', { method: 'POST', body: f });
      toast('Division created', 'success');
      onDone();
    } catch (err) {
      const msg = err.message || 'Failed to create division';
      setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };
  return (
    <Modal open={open} onClose={onClose} title="New division" wide
      footer={<><button className="btn" onClick={onClose}>Cancel</button>
        <button type="submit" className="btn btn-primary" form="create-division" disabled={busy}>{busy ? 'Saving…' : 'Create division'}</button></>}>
      <form id="create-division" className="grid-2" onSubmit={submit}>
        {error && <div className="span-2"><ErrorBox error={error} /></div>}
        <Field label="Division name" required><TextInput value={f.name || ''} onChange={set('name')} required /></Field>
        <Field label="Division code" hint="Optional — auto-generated if blank. This is the login slug.">
          <TextInput value={f.code || ''} onChange={set('code')} placeholder="e.g. NORTH1234" />
          {f.code
            ? <small className="field-hint">Sign-in link: <code>{loginUrl(String(f.code).toUpperCase())}</code></small>
            : <small className="field-hint">Leave blank to auto-generate — the sign-in link appears after creating.</small>
          }
        </Field>
        <Field label="Description"><TextInput value={f.description || ''} onChange={set('description')} /></Field>
        <Field label="Contact person"><TextInput value={f.contact_person || ''} onChange={set('contact_person')} /></Field>
        <Field label="Contact email"><TextInput type="email" value={f.contact_email || ''} onChange={set('contact_email')} /></Field>
        <Field label="Contact mobile"><TextInput value={f.contact_mobile || ''} onChange={set('contact_mobile')} /></Field>
        <div className="span-2">
          <label className="check">
            <input type="checkbox" checked={f.provision} onChange={set('provision')} />
            Provision tenant database immediately
          </label>
        </div>
        {f.provision && (
          <>
            <Field label="Admin username" required hint="Division administrator login">
              <TextInput value={f.admin_username || ''} onChange={set('admin_username')} required /></Field>
            <Field label="Admin password" required hint="min 6 characters">
              <TextInput type="password" value={f.admin_password || ''} onChange={set('admin_password')} required /></Field>
            <Field label="Admin full name"><TextInput value={f.admin_full_name || ''} onChange={set('admin_full_name')} /></Field>
            <Field label="Admin email"><TextInput type="email" value={f.admin_email || ''} onChange={set('admin_email')} /></Field>
          </>
        )}
      </form>
    </Modal>
  );
}
