import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, fmtDateTime } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, SearchBox,
  StatCard, StatSkeleton, Table, TableSkeleton, TextInput, toast, useAsync,
} from '../../ui';

const loginUrl = (code) => (code ? `${window.location.origin}/login/${encodeURIComponent(code)}` : null);

export default function Divisions() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/superadmin/divisions'));
  const nav = useNavigate();
  const [params] = useSearchParams();
  const [q, setQ] = useState('');
  const [showCreate, setShowCreate] = useState(() => params.get('new') === '1');

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

const parseRegions = (s) => s.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

const stepTitles = ['Division details', 'Division admin', 'Review & provision'];

function CreateDivision({ open, onClose, onDone }) {
  const nav = useNavigate();
  const [step, setStep] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [created, setCreated] = useState(null);
  const [f, setF] = useState({ provision: true, covered_regions: [] });
  useEffect(() => {
    if (!open) {
      setBusy(false); setError(null); setStep(1); setCreated(null);
      setF({ provision: true, covered_regions: [] });
    }
  }, [open]);
  const set = (k) => (e) => {
    const v = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setF((p) => ({ ...p, [k]: v }));
  };
  const setRegions = (e) => {
    setF((p) => ({ ...p, covered_regions: parseRegions(e.target.value) }));
  };
  const next = () => {
    setError(null);
    if (step === 1) {
      if (!(f.name || '').trim()) { setError('Division name is required'); return; }
      setStep(f.provision ? 2 : 3);
      return;
    }
    if (step === 2) {
      if (!(f.admin_username || '').trim()) { setError('Admin username is required'); return; }
      const pw = (f.admin_password || '').trim();
      if (pw && pw.length < 6) {
        const msg = 'Admin password must be at least 6 characters (or leave blank to auto-generate a temporary password)';
        setError(msg); toast(msg, 'error');
        return;
      }
      setStep(3);
    }
  };
  const back = () => { setError(null); setStep((s) => Math.max(1, s - 1)); };
  const submit = async (e) => {
    e.preventDefault();
    setError(null); setBusy(true);
    try {
      const row = await api('/api/v1/superadmin/divisions', { method: 'POST', body: f });
      setCreated(row);
      setStep(4);
      toast('Division created', 'success');
    } catch (err) {
      const msg = err.message || 'Failed to create division';
      setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };

  const goConfigureHierarchy = () => {
    onDone();
    nav(`/superadmin/divisions/${created.id}`);
  };

  const footer = (
    <>
      {step === 4
        ? <>
          <button className="btn" onClick={onClose}>Done</button>
          {created?.tenant_db_name && (
            <button className="btn btn-primary" onClick={goConfigureHierarchy}>Configure hierarchy</button>
          )}
        </>
        : <>
          <button className="btn" onClick={step === 1 ? onClose : back}>{step === 1 ? 'Cancel' : 'Back'}</button>
          {step < 3
            ? <button className="btn btn-primary" onClick={next} disabled={busy}>Next</button>
            : <button type="submit" className="btn btn-primary" form="create-division" disabled={busy}>{busy ? 'Creating…' : 'Create division'}</button>}
        </>}
    </>
  );

  return (
    <Modal open={open} onClose={onClose} title="New division" wide footer={footer}>
      <form id="create-division" onSubmit={submit}>
        {error && step < 4 && <div className="span-2"><ErrorBox error={error} /></div>}
        {step < 4 && (
          <div className="stepper">
            {stepTitles.map((t, i) => (
              <span key={t} className={`stepper-item ${i + 1 === step ? 'active' : ''} ${i + 1 < step ? 'done' : ''}`}>
                <i>{i + 1}</i>{t}
              </span>
            ))}
          </div>
        )}

        {step === 1 && (
          <div className="grid-2">
            <Field label="Division name" required><TextInput value={f.name || ''} onChange={set('name')} required /></Field>
            <Field label="Division code" hint="Optional — auto-generated if blank. This is the login slug.">
              <TextInput value={f.code || ''} onChange={set('code')} placeholder="e.g. CARDIO" />
              <div className="warning-text">Choose carefully — this becomes the division's permanent sign-in link and cannot be changed later.</div>
              {f.code
                ? <small className="field-hint">Sign-in link: <code>{loginUrl(String(f.code).toUpperCase())}</code></small>
                : <small className="field-hint">Leave blank to auto-generate — the sign-in link appears after creating.</small>}
            </Field>
            <div className="span-2">
              <Field label="Description"><TextInput value={f.description || ''} onChange={set('description')} /></Field>
            </div>
            <Field label="Division head"><TextInput value={f.contact_person || ''} onChange={set('contact_person')} /></Field>
            <Field label="Contact email"><TextInput type="email" value={f.contact_email || ''} onChange={set('contact_email')} /></Field>
            <Field label="Contact mobile"><TextInput value={f.contact_mobile || ''} onChange={set('contact_mobile')} /></Field>
            <div className="span-2">
              <Field label="States / regions covered" hint="Comma or newline separated, e.g. Maharashtra, Karnataka">
                <textarea className="input" rows={2} value={f.covered_regions.join(', ')}
                  onChange={setRegions} placeholder="e.g. Maharashtra, Karnataka" />
              </Field>
            </div>
            <div className="span-2">
              <label className="check">
                <input type="checkbox" checked={f.provision} onChange={set('provision')} />
                Provision tenant database immediately
              </label>
              <small className="field-hint">Status is set automatically — inactive on creation, active once provisioned.</small>
            </div>
          </div>
        )}

        {step === 2 && f.provision && (
          <div className="grid-2">
            <div className="span-2 field-hint">The system creates the Division <strong>{f.name || ''}</strong> and its administrator ({f.code || 'auto code'}) together when you provision.</div>
            <Field label="Admin username" required hint="Division administrator login">
              <TextInput value={f.admin_username || ''} onChange={set('admin_username')} required /></Field>
            <Field label="Admin password" hint="min 6 chars, or leave blank to auto-generate a temp password">
              <TextInput type="password" value={f.admin_password || ''} onChange={set('admin_password')} /></Field>
            <Field label="Admin full name"><TextInput value={f.admin_full_name || ''} onChange={set('admin_full_name')} /></Field>
            <Field label="Admin email"><TextInput type="email" value={f.admin_email || ''} onChange={set('admin_email')} /></Field>
          </div>
        )}

        {step === 3 && (
          <div className="review-list">
            <h4>Division</h4>
            <p><strong>{f.name}</strong> {f.code ? `(${String(f.code).toUpperCase()})` : '(auto code)'}</p>
            <div className="warning-text">This code is permanent — double-check it before creating.</div>
            {f.description && <p>{f.description}</p>}
            {(f.contact_person || f.contact_email || f.contact_mobile) && (
              <p className="muted">{f.contact_person || '—'} · {f.contact_email || '—'} · {f.contact_mobile || '—'}</p>
            )}
            {f.covered_regions.length > 0 && <p className="muted">Regions: {f.covered_regions.join(', ')}</p>}
            {f.provision && (
              <>
                <h4>Division admin</h4>
                <p><strong>{f.admin_username}</strong>
                  {f.admin_full_name ? ` — ${f.admin_full_name}` : ''} {f.admin_email ? `(${f.admin_email})` : ''}</p>
                <p className="field-hint">A tenant database will be created (named from the division) and provisioned automatically along with the admin account.</p>
              </>
            )}
          </div>
        )}

        {step === 4 && (
          <div className="review-list">
            <h4>Division created</h4>
            <p>{created?.name} ({created?.code}) is now <Badge tone={created?.status}>{created?.status}</Badge>.</p>
            {created?.tenant_db_name
              ? <p className="field-hint">Database <code>{created.tenant_db_name}</code> provisioned. Your division administrator signs in via <code>{loginUrl(created?.code)}</code>; on first login they set a new password, enroll 2FA and complete their profile.</p>
              : <p className="field-hint">No database was provisioned yet. Open the division to provision it.</p>}
            {created?.temp_password && (
              <div className="temporary-creds">
                <h4>Temporary admin credentials</h4>
                <p className="field-hint">Username <code>{f.admin_username}</code></p>
                <p className="field-hint">One-time password <code>{created.temp_password}</code></p>
                <p className="field-hint">Won’t be shown again — pass it to the administrator securely.</p>
              </div>
            )}
          </div>
        )}
      </form>
    </Modal>
  );
}
