import { useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api, fmtDateTime } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, SearchBox, Select, Skeleton, StatCard,
  StatSkeleton, StatusBadge, Table, TableSkeleton, Tabs, TextArea, TextInput, toast, useAsync,
} from '../../ui';

const TABS = [
  { value: 'overview', label: 'Overview' },
  { value: 'users', label: 'Users' },
  { value: 'roles', label: 'Roles & hierarchy' },
];

const loginUrl = (code) => (code ? `${window.location.origin}/login/${encodeURIComponent(code)}` : null);
const initialsOf = (a) => (a.full_name || a.username || '?').trim()
  .split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();

export default function DivisionDetail() {
  const { did } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get('tab');
  const [tab, setTab] = useState(TABS.some((t) => t.value === requested) ? requested : 'overview');
  const [editOpen, setEditOpen] = useState(false);
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/superadmin/divisions/${did}`), [did]);

  const changeTab = (v) => {
    setTab(v);
    setSearchParams(v === 'overview' ? {} : { tab: v }, { replace: true });
  };

  if (loading) {
    return (
      <div>
        <PageHeader title="Division" subtitle="Loading details…"
          actions={<Link className="btn" to="/superadmin/divisions">← Back to divisions</Link>} />
        <StatSkeleton n={3} />
        <div className="card" style={{ marginTop: 12 }}>
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} style={{ marginBottom: 14 }}><Skeleton h={14} w="100%" /></div>
          ))}
        </div>
        <div className="card" style={{ marginTop: 12 }}><Skeleton h={120} w="100%" /></div>
        <div className="card" style={{ marginTop: 12 }}><Skeleton h={70} w="100%" /></div>
      </div>
    );
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const c = data;
  const act = async (fn, msg) => {
    try { await fn(); toast(msg, 'success'); run(); } catch (e) { toast(e.message, 'error'); }
  };
  const post = (path, msg) => act(() => api(`/api/v1/superadmin/divisions/${c.id}${path}`, { method: 'POST' }), msg);
  const provisionDb = async () => {
    if (!window.confirm('Provision the tenant database for this division?')) return;
    act(async () => {
      const d = await api(`/api/v1/superadmin/divisions/${c.id}/provision`,
        { method: 'POST', body: { admin_username: 'division_admin' } });
      if (d.temp_password) window.alert(`Temporary admin password for first login:\n\n${d.temp_password}`);
    }, 'Division provisioned');
  };

  return (
    <div>
      <PageHeader title={`${c.name} (${c.code})`}
        subtitle={c.tenant_db_name
          ? <><Badge tone={c.status}>{c.status}</Badge> · Tenant DB: <code>{c.tenant_db_name}</code></>
          : <><Badge tone={c.status}>{c.status}</Badge> · Tenant DB not provisioned</>}
        actions={
          <>
            <button className="btn" onClick={() => setEditOpen(true)}>✎ Edit division</button>
            {!c.tenant_db_name && c.status === 'draft' && (
              <button className="btn btn-primary" onClick={provisionDb}>Provision tenant DB</button>
            )}
            {c.status === 'draft' && (
              <button className="btn" onClick={() => post('/activate', 'Division activated')}>Activate</button>
            )}
            {c.status === 'active' && (
              <>
                <button className="btn" onClick={() => post('/suspend', 'Division suspended')}>Suspend</button>
                <button className="btn btn-danger" onClick={() => post('/deactivate', 'Division deactivated')}>Deactivate</button>
              </>
            )}
            {c.status === 'suspended' && (
              <>
                <button className="btn" onClick={() => post('/resume', 'Division resumed')}>Resume</button>
                <button className="btn btn-danger" onClick={() => post('/deactivate', 'Division deactivated')}>Deactivate</button>
              </>
            )}
            {c.status === 'inactive' && (
              <>
                <button className="btn" onClick={() => post('/activate', 'Division activated')}>Activate</button>
                <button className="btn btn-danger" onClick={() => post('/archive', 'Division archived')}>Archive</button>
              </>
            )}
            {c.status === 'archived' && (
              <span className="muted">Archived — no further actions</span>
            )}
            <Link className="btn" to="/superadmin/divisions">← Back to divisions</Link>
          </>
        } />

      <Tabs items={TABS} active={tab} onChange={changeTab} />

      {tab === 'overview' && (
        <>
          <div className="stats-grid">
            <StatCard label="Users" value={c.user_count ?? '—'} icon="👥" />
            <StatCard label="POBs" value={c.pob_count ?? '—'} icon="📄" />
            <StatCard label="Campaigns" value={c.campaign_count ?? '—'} icon="◎" />
          </div>

          <div className="card" style={{ marginTop: 12 }}>
            <h4 className="section-title">Division details</h4>
            <table className="detail-table">
              <tbody>
                <tr>
                  <th>Status</th>
                  <td><Badge tone={c.status}>{c.status}</Badge></td>
                </tr>
                <tr>
                  <th>Tenant database</th>
                  <td>{c.tenant_db_name ? <code>{c.tenant_db_name}</code> : 'Not provisioned'}</td>
                </tr>
                <tr>
                  <th>Sign-in link</th>
                  <td>
                    {c.code
                      ? <><a href={loginUrl(c.code)} target="_blank" rel="noreferrer">/login/{c.code}</a>{' '}
                          <button className="btn-link" onClick={() => { navigator.clipboard?.writeText(loginUrl(c.code)); toast('Sign-in link copied', 'success'); }}>Copy</button></>
                      : '—'}
                  </td>
                </tr>
                <tr>
                  <th>Created</th>
                  <td>{fmtDateTime(c.created_at)}</td>
                </tr>
                <tr>
                  <th>Description</th>
                  <td>{c.description || '—'}</td>
                </tr>
                <tr>
                  <th>Contact</th>
                  <td>{c.contact_person || '—'}{c.contact_email ? ` (${c.contact_email})` : ''}{c.contact_mobile ? ` · ${c.contact_mobile}` : ''}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="card" style={{ marginTop: 12 }}>
            <h4 className="section-title">Division branding</h4>
            <p className="muted">This identity is shown on the division's sign-in page and inside the app sidebar.</p>
            <div className="branding-grid">
              <div className="branding-preview">
                <div className="branding-screen">
                  <div className="branding-login-card">
                    {c.logo_path
                      ? <img src={`/api/v1/auth/division-logo/${c.id}`} alt="Division logo" className="branding-logo-img" />
                      : <span className="branding-mark">{(c.name || 'D').charAt(0)}</span>}
                    <div className="branding-name">{c.name}</div>
                    <div className="branding-fields">
                      <span className="branding-field" />
                      <span className="branding-field" />
                    </div>
                    <div className="branding-signin">Sign in</div>
                  </div>
                </div>
                <div className="branding-caption">Sign-in page preview — /login/{c.code || '…'}</div>
              </div>
              <LogoUpload division={c} onDone={run} />
            </div>
          </div>

          <div className="card" style={{ marginTop: 12 }}>
            <h4 className="section-title">Division admins</h4>
            <AdminsCard did={c.id} />
          </div>
        </>
      )}

      {tab === 'users' && <UsersTab did={c.id} />}
      {tab === 'roles' && <RolesTab did={c.id} />}

      {editOpen && (
        <DivisionEditModal division={c}
          onClose={() => setEditOpen(false)}
          onDone={() => { setEditOpen(false); run(); }} />
      )}
    </div>
  );
}

function DivisionEditModal({ division, onClose, onDone }) {
  const [f, setF] = useState({
    name: division.name || '',
    description: division.description || '',
    contact_person: division.contact_person || '',
    contact_email: division.contact_email || '',
    contact_mobile: division.contact_mobile || '',
  });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (!f.name.trim()) {
      const msg = 'Division name is required';
      setError(msg); toast(msg, 'error');
      return;
    }
    setBusy(true);
    try {
      await api(`/api/v1/superadmin/divisions/${division.id}`, {
        method: 'PUT',
        body: { ...f, name: f.name.trim() },
      });
      toast('Division updated', 'success');
      onDone();
    } catch (err) {
      const msg = err.message || 'Update failed';
      setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };

  return (
    <Modal open wide title={`Edit division — ${division.name}`} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="div-edit-form" disabled={busy}>{busy ? 'Saving…' : 'Save changes'}</button>
      </>}>
      <form id="div-edit-form" className="grid-2" onSubmit={submit}>
        {error && <div className="span-2"><ErrorBox error={error} /></div>}
        <Field label="Division name" required hint="Shown on the sign-in page and inside the app sidebar.">
          <TextInput value={f.name} onChange={set('name')} required />
        </Field>
        <Field label="Division code" hint="Cannot be changed — used in the sign-in URL and tenant database.">
          <TextInput value={division.code || ''} disabled />
        </Field>
        <div className="span-2">
          <Field label="Description">
            <TextArea rows={3} value={f.description || ''} onChange={set('description')} />
          </Field>
        </div>
        <Field label="Contact person"><TextInput value={f.contact_person} onChange={set('contact_person')} /></Field>
        <Field label="Contact email"><TextInput type="email" value={f.contact_email} onChange={set('contact_email')} /></Field>
        <Field label="Contact mobile"><TextInput value={f.contact_mobile} onChange={set('contact_mobile')} /></Field>
        <p className="span-2 muted" style={{ fontSize: 12 }}>
          Users already signed in to this division will see the new name after their next sign-in.
        </p>
      </form>
    </Modal>
  );
}

function AdminsCard({ did }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/superadmin/divisions/${did}/admins`));
  const [editing, setEditing] = useState(null);
  const [resetting, setResetting] = useState(null);
  if (loading) return <div style={{ padding: '8px 0' }}><Skeleton h={40} w="100%" /></div>;
  if (error) return <ErrorBox error={error} onRetry={run} />;
  const admins = data?.items || [];

  const toggle = async (a) => {
    if (!window.confirm(`${a.status === 'active' ? 'Deactivate' : 'Activate'} ${a.full_name || a.username}?`)) return;
    try {
      await api(`/api/v1/superadmin/divisions/${did}/users/${a.id}`, {
        method: 'PUT', body: { status: a.status === 'active' ? 'inactive' : 'active' },
      });
      toast(a.status === 'active' ? 'Admin deactivated' : 'Admin activated', 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
  };

  return (
    <div>
      {admins.length === 0 && (
        <div className="admin-empty">
          <p>No admin users yet</p>
          <p className="muted">The division's own administrator manages its users, roles and hierarchy.</p>
        </div>
      )}
      <div className="admin-list">
        {admins.map((a) => (
          <div key={a.id} className="admin-row">
            <span className="admin-avatar">{initialsOf(a)}</span>
            <div className="admin-id">
              <strong>{a.full_name || a.username}</strong>
              <span>@{a.username}</span>
            </div>
            <div className="admin-contact">
              <span>{a.email || '—'}</span>
              <span>{a.mobile || '—'}</span>
            </div>
            <div className="admin-badges">
              <Badge tone="blue">{a.role_name || 'division_admin'}</Badge>
              <StatusBadge value={a.status} />
            </div>
            <div className="admin-actions">
              <button className="btn btn-sm" onClick={() => setEditing({ ...a })}>Edit</button>
              <button className="btn btn-sm" onClick={() => setResetting({ ...a })}>Reset password</button>
              <button className={`btn btn-sm${a.status === 'active' ? ' btn-danger' : ''}`}
                onClick={() => toggle(a)}>
                {a.status === 'active' ? 'Deactivate' : 'Activate'}
              </button>
            </div>
          </div>
        ))}
      </div>
      {editing && (
        <AdminEditModal admin={editing} did={did}
          onClose={() => setEditing(null)} onDone={() => { setEditing(null); run(); }} />
      )}
      {resetting && (
        <ResetAdminPasswordModal admin={resetting} did={did}
          onClose={() => setResetting(null)} onDone={() => setResetting(null)} />
      )}
    </div>
  );
}

function AdminEditModal({ admin, did, onClose, onDone }) {
  const [f, setF] = useState({
    full_name: admin.full_name || '', email: admin.email || '', mobile: admin.mobile || '',
    employee_id: admin.employee_id || '', status: admin.status || 'active',
  });
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (password && password.length < 6) {
      const msg = 'New password must be at least 6 characters';
      setError(msg); toast(msg, 'error');
      return;
    }
    setBusy(true);
    try {
      const body = { ...f };
      if (password) body.password = password;
      await api(`/api/v1/superadmin/divisions/${did}/users/${admin.id}`, { method: 'PUT', body });
      toast('Admin updated', 'success');
      onDone();
    } catch (err) {
      const msg = err.message || 'Update failed';
      setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };

  return (
    <Modal open wide title={`Edit ${admin.full_name || admin.username}`} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="admin-edit-form" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
      </>}>
      <form id="admin-edit-form" onSubmit={submit}>
        {error && <ErrorBox error={error} />}
        <div className="admin-editor-head">
          <span className="admin-avatar lg">{initialsOf(admin)}</span>
          <div>
            <strong>{admin.full_name || admin.username}</strong>
            <span>@{admin.username} · <Badge tone="blue">{admin.role_name || 'division_admin'}</Badge></span>
          </div>
        </div>

        <h5 className="form-section">Profile</h5>
        <div className="grid-2">
          <Field label="Full name" required>
            <TextInput value={f.full_name || ''} onChange={set('full_name')} required />
          </Field>
          <Field label="Username" hint="Cannot be changed">
            <TextInput value={admin.username || ''} disabled />
          </Field>
          <Field label="Email" hint="Used for sign-in and notifications">
            <TextInput type="email" value={f.email || ''} onChange={set('email')} />
          </Field>
          <Field label="Mobile">
            <TextInput value={f.mobile || ''} onChange={set('mobile')} />
          </Field>
          <Field label="Employee ID">
            <TextInput value={f.employee_id || ''} onChange={set('employee_id')} />
          </Field>
          <Field label="Status">
            <Select value={f.status} onChange={set('status')}
              options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} />
          </Field>
        </div>

        <h5 className="form-section">Password</h5>
        <div className="grid-2">
          <Field label="Set new password" hint="Leave blank to keep the current one — min 6 characters.">
            <TextInput type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••" autoComplete="new-password" />
          </Field>
        </div>
      </form>
    </Modal>
  );
}

function ResetAdminPasswordModal({ admin, did, onClose, onDone }) {
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (password.length < 6) {
      const msg = 'Password must be at least 6 characters';
      setError(msg); toast(msg, 'error');
      return;
    }
    setBusy(true);
    try {
      await api(`/api/v1/superadmin/divisions/${did}/users/${admin.id}`, {
        method: 'PUT', body: { password },
      });
      toast(`Password reset for ${admin.full_name || admin.username}`, 'success');
      onDone();
    } catch (err) {
      const msg = err.message || 'Reset failed';
      setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };

  return (
    <Modal open title={`Reset password — ${admin.full_name || admin.username}`} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="admin-reset-form" disabled={busy}>{busy ? 'Resetting…' : 'Reset password'}</button>
      </>}>
      <form id="admin-reset-form" onSubmit={submit}>
        {error && <ErrorBox error={error} />}
        <Field label="New password" required hint="min 6 characters">
          <TextInput type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="new-password" />
        </Field>
        <p className="muted">The admin will need this password for their next sign-in.</p>
      </form>
    </Modal>
  );
}

function LogoUpload({ division, onDone }) {
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [pending, setPending] = useState(null);
  const inputRef = useRef(null);

  const hasLogo = !!division.logo_path;
  const currentUrl = hasLogo ? `/api/v1/auth/division-logo/${division.id}` : null;
  const shown = pending || currentUrl;

  const upload = async (file) => {
    if (!file || busy) return;
    if (!/^image\//.test(file.type)) { toast('Please choose an image file (PNG, JPG or WebP)', 'error'); return; }
    setBusy(true);
    setPending(URL.createObjectURL(file));
    try {
      const fd = new FormData();
      fd.append('file', file);
      await api(`/api/v1/superadmin/divisions/${division.id}/logo`, {
        method: 'POST', body: fd,
      });
      toast('Logo uploaded', 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); }
    finally { setBusy(false); setPending(null); }
  };

  const remove = async () => {
    if (busy) return;
    if (!window.confirm('Remove this logo? The sign-in page will fall back to the default brand mark.')) return;
    setBusy(true);
    try {
      await api(`/api/v1/superadmin/divisions/${division.id}/logo`, { method: 'DELETE' });
      toast('Logo removed', 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); }
    finally { setBusy(false); }
  };

  return (
    <div className="branding-controls">
      <div className="branding-label-row">
        <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <strong>Division logo</strong>
          <span className="muted">PNG / JPG / WebP, max 2 MB</span>
        </span>
        {hasLogo && (
          <button className="btn btn-danger btn-sm" onClick={remove} disabled={busy}>Remove</button>
        )}
      </div>

      <div className={`dropzone logo-dropzone branding-dropzone${shown ? ' has-file' : ''}${dragging ? ' dragging' : ''}`}
        role="button" tabIndex={0} aria-label={shown ? 'Replace division logo' : 'Upload division logo'}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click(); }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); upload(e.dataTransfer.files?.[0]); }}>
        <input ref={inputRef} type="file" accept="image/png,image/jpeg,image/webp" hidden
          onChange={(e) => upload(e.target.files?.[0])} />
        {shown ? (
          <>
            <img src={shown} alt="Division logo preview" className="logo-dropzone-preview" />
            <strong>{busy ? 'Uploading…' : 'Replace logo'}</strong>
          </>
        ) : (
          <>
            <span className="dropzone-icon">🖼</span>
            <strong>{busy ? 'Uploading…' : 'Upload a logo'}</strong>
            <span className="muted" style={{ fontSize: 12 }}>Click to browse or drag &amp; drop</span>
          </>
        )}
      </div>

      <p className="muted branding-hint">
        The logo appears on the division's sign-in page and inside the app sidebar. A transparent PNG looks best.
      </p>
    </div>
  );
}

// ── Drill-down tabs: read-only visibility into the division tenant ─────────

function UsersTab({ did }) {
  const [q, setQ] = useState('');
  const { data, loading, error, run } = useAsync(
    () => api(`/api/v1/superadmin/divisions/${did}/users?q=${encodeURIComponent(q)}`),
    [did, q]);
  if (loading) return <TableSkeleton cols={7} rows={6} />;
  if (error) return <ErrorBox error={error} onRetry={run} />;
  const users = data?.items || [];
  const cols = [
    {
      key: 'name', label: 'User',
      render: (r) => (<><strong>{r.full_name || '—'}</strong><div className="muted">@{r.username}</div></>),
    },
    { key: 'role_name', label: 'Role', render: (r) => <Badge tone="blue">{r.role_name || '—'}</Badge> },
    { key: 'hierarchy_level_name', label: 'Level' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: 'email', label: 'Email' },
    { key: 'mobile', label: 'Mobile' },
    { key: 'employee_id', label: 'Emp ID' },
    { key: 'last_login', label: 'Last login', render: (r) => r.last_login ? fmtDateTime(r.last_login) : '—' },
  ];
  return (
    <div className="card">
      <div className="toolbar">
        <SearchBox value={q} onChange={setQ} placeholder="Search name / username / email…" />
        <span className="muted">{data?.total ?? users.length} user{data?.total === 1 ? '' : 's'}</span>
      </div>
      <Table cols={cols} rows={users} keyOf={(r) => r.id} empty="No users found" />
    </div>
  );
}

function RolesTab({ did }) {
  const roles = useAsync(() => api(`/api/v1/superadmin/divisions/${did}/roles`));
  const tree = useAsync(() => api(`/api/v1/superadmin/divisions/${did}/hierarchy/tree`));
  if (roles.loading || tree.loading) return <TableSkeleton cols={4} rows={5} />;
  if (roles.error) return <ErrorBox error={roles.error} onRetry={roles.run} />;
  const cols = [
    { key: 'name', label: 'Role', render: (r) => <strong>{r.name}</strong> },
    { key: 'description', label: 'Description' },
    { key: 'perm_count', label: 'Permissions', render: (r) => <>{r.perm_count ?? r.permissions?.length ?? 0}</> },
    { key: 'data_entry', label: 'Data entry', render: (r) => r.data_entry ? <Badge tone="green">yes</Badge> : '—' },
  ];
  return (
    <>
      <div className="card">
        <h4 className="section-title">Roles</h4>
        <Table cols={cols} rows={roles.data?.items || []} keyOf={(r) => r.id} empty="No roles defined" />
      </div>
      <div className="card" style={{ marginTop: 12 }}>
        <h4 className="section-title">Reporting hierarchy</h4>
        {tree.error
          ? <ErrorBox error={tree.error} onRetry={tree.run} />
          : (tree.data?.items?.length
              ? tree.data.items.map((n) => <ReadOnlyNode key={n.id} node={n} />)
              : <p className="muted">No hierarchy levels defined.</p>)}
      </div>
    </>
  );
}

function ReadOnlyNode({ node, depth = 0 }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="tree-node" style={{ paddingLeft: depth * 22 }}>
      <div className="tree-row">
        <button className={`tree-toggle${open ? ' open' : ''}`} onClick={() => setOpen(!open)}
          disabled={!node.children?.length}
          aria-label={`${open ? 'Collapse' : 'Expand'} ${node.name}`}>
          {node.children?.length ? (
            <span className="acc-chev" aria-hidden="true">
              <svg viewBox="0 0 24 24" focusable="false">
                <path fill="currentColor" d="M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z" />
              </svg>
            </span>
          ) : <span className="tree-leaf">·</span>}
        </button>
        <span className={`tree-dot rank-${node.rank}`} />
        <strong>{node.name}</strong>
        <span className="muted">{node.label} · rank {node.rank}</span>
        {node.users?.length > 0 && (
          <span className="muted"> — {node.users.map((u) => u.full_name || u.username).join(', ')}</span>
        )}
      </div>
      {open && (node.children || []).map((ch) => (
        <ReadOnlyNode key={ch.id} node={ch} depth={depth + 1} />
      ))}
    </div>
  );
}

