import { useMemo, useState } from 'react';
import { api, downloadFile, fmtDateTime, uploadFile } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, SearchBox, Select, Spinner, StatusBadge,
  Table, TextInput, toast, useAsync,
} from '../../ui';

export default function Users({ base = '/api/v1' }) {
  const { data, loading, error, run } = useAsync(() => api(`${base}/users`));
  const roles = useAsync(() => api(`${base}/roles`));
  const levels = useAsync(() => api(`${base}/hierarchy/levels`));
  const divisions = useAsync(() => api(`${base}/divisions`));
  const [q, setQ] = useState('');
  const [divFilter, setDivFilter] = useState('');
  const [editing, setEditing] = useState(null);
  const [showUpload, setShowUpload] = useState(false);

  const rows = useMemo(() => {
    let items = data?.items || [];
    if (divFilter) {
      items = divFilter === '_none'
        ? items.filter((r) => !r.division_id)
        : items.filter((r) => String(r.division_id) === String(divFilter));
    }
    if (!q) return items;
    const n = q.toLowerCase();
    return items.filter((r) =>
      r.full_name?.toLowerCase().includes(n) || r.username?.toLowerCase().includes(n) ||
      r.email?.toLowerCase().includes(n) || r.mobile?.toLowerCase().includes(n));
  }, [data, q, divFilter]);

  const divs = divisions.data?.items || [];
  const unassigned = (data?.items || []).filter((r) => !r.division_id).length;
  const singleDivision = divisions.data ? divs.length <= 1 : false;

  if (loading) return <Spinner label="Loading users…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'full_name', label: 'Name', render: (r) => <span><strong>{r.full_name}</strong><br /><small className="muted">{r.username}</small></span> },
    { key: 'role_name', label: 'Role', render: (r) => <Badge tone="blue">{r.role_name || '—'}</Badge> },
    ...(singleDivision ? [] : [{ key: 'division', label: 'Division', render: (r) => (r.division_id
      ? <Badge tone="green">{r.division_name || r.division || '—'}</Badge>
      : <span className="muted" title="This user cannot sign in through any division link">No division</span>) }]),
    { key: 'hierarchy_level_name', label: 'Level' },
    { key: 'parent_name', label: 'Reports to' },
    { key: 'email', label: 'Email' },
    { key: 'mobile', label: 'Mobile' },
    { key: 'region', label: 'Region', render: (r) => `${r.region || ''}${r.area ? ` / ${r.area}` : ''}${r.territory ? ` / ${r.territory}` : ''}` || '—' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: 'last_login', label: 'Last login', render: (r) => fmtDateTime(r.last_login) },
  ];

  return (
    <div>
      <PageHeader title="Users"
        subtitle={`Company team · ${data?.total || 0} total${divFilter ? ` · ${rows.length} shown` : ''}`}
        actions={
          <>
            <SearchBox value={q} onChange={setQ} />
            {!singleDivision && (
              <Select value={divFilter} onChange={(e) => setDivFilter(e.target.value)}
                placeholder="All divisions"
                options={[...divs.map((d) => ({ value: d.id, label: d.name })),
                          { value: '_none', label: 'No division' }]} />
            )}
            <button className="btn" onClick={() => setShowUpload(true)}>Bulk upload</button>
            <button className="btn btn-primary" onClick={() => setEditing({})}>+ New user</button>
          </>
        } />
      {!singleDivision && unassigned > 0 && (
        <div className="scope-note">
          <span>{unassigned} user{unassigned > 1 ? 's have' : ' has'} no division</span>
          <strong>they cannot sign in through a division link</strong>
        </div>
      )}
      <Table cols={cols} rows={rows} keyOf={(r) => r.id} empty="No users match"
        onRowClick={(r) => setEditing({ ...r })} />

      {editing && (
        <UserModal editing={editing} base={base} roles={roles.data?.items || []} levels={levels.data?.items || []}
          divisions={divisions.data?.items || []} allUsers={data?.items || []} isEdit={!!editing.id}
          onClose={() => setEditing(null)} onDone={() => { setEditing(null); run(); }} />
      )}
      <BulkUpload base={base} open={showUpload} onClose={() => setShowUpload(false)} onDone={() => { setShowUpload(false); run(); }} />
    </div>
  );
}

function UserModal({ editing, base, roles, levels, divisions, allUsers, isEdit, onClose, onDone }) {
  const [f, setF] = useState(editing);
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [resetResult, setResetResult] = useState(null);
  const [resetBusy, setResetBusy] = useState(false);
  const singleDivision = divisions.length <= 1;
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));
  const assignableRoles = (!f.role_id ? roles : roles.filter((r) => !r.global || r.id === f.role_id));

  const resetPassword = async () => {
    if (!window.confirm(`Reset ${editing.full_name}'s password? They'll get a temporary password and must change it at next sign-in.`)) return;
    setResetBusy(true); setError(null);
    try {
      const r = await api(`${base}/users/${editing.id}/reset-password`, { method: 'POST' });
      setResetResult(r);
      toast('Password reset — share the temporary password', 'success');
    } catch (err) {
      const msg = err.message || 'Reset failed';
      setError(msg); toast(msg, 'error');
    } finally { setResetBusy(false); }
  };

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (password && password.length < 6) {
      const msg = 'Password must be at least 6 characters';
      setError(msg); toast(msg, 'error');
      return;
    }
    setBusy(true);
    try {
      const body = { ...f };
      if (isEdit) {
        if (password) body.password = password;
        await api(`${base}/users/${editing.id}`, { method: 'PUT', body });
      } else {
        await api(`${base}/users`, { method: 'POST', body });
      }
      toast(isEdit ? 'User updated' : 'User created', 'success');
      onDone();
    } catch (err) {
      const msg = err.message || 'Save failed';
      setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };

  const deactivate = async () => {
    if (!window.confirm(`Deactivate ${editing.full_name}?`)) return;
    try { await api(`${base}/users/${editing.id}`, { method: 'DELETE' }); toast('User deactivated', 'success'); onDone(); }
    catch (err) { toast(err.message, 'error'); }
  };

  return (
    <Modal open wide title={isEdit ? `Edit ${editing.full_name}` : 'New user'} onClose={onClose}
      footer={<>
        {isEdit && <button className="btn btn-danger" onClick={deactivate}>Deactivate</button>}
        {isEdit && <button className="btn" onClick={resetPassword} disabled={resetBusy || busy}>{resetBusy ? 'Resetting…' : 'Reset password'}</button>}
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="user-form" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
      </>}>
      <form id="user-form" className="grid-2" onSubmit={submit}>
        {error && <div className="span-2"><ErrorBox error={error} /></div>}
        {resetResult && (
          <div className="card span-2" style={{ marginBottom: 8 }}>
            <h5 className="form-section">Temporary password</h5>
            <p className="muted">Share this once — it isn't shown again. {editing.full_name} must change it at next sign-in.
              Existing sessions were revoked.</p>
            <code className="mono" style={{ fontSize: 15 }}>{resetResult.temp_password}</code>{' '}
            <button className="btn-link" onClick={() => {
              navigator.clipboard?.writeText(resetResult.temp_password);
              toast('Temporary password copied', 'success');
            }}>Copy</button>
          </div>
        )}
        <Field label="Username" required><TextInput value={f.username || ''} onChange={set('username')} required /></Field>
        <Field label="Full name" required><TextInput value={f.full_name || ''} onChange={set('full_name')} required /></Field>
        {!isEdit ? (
          <Field label="Password" required hint="min 6 characters"><TextInput type="password" value={f.password || ''} onChange={set('password')} required /></Field>
        ) : (
          <Field label="Set new password" hint="Leave blank to keep the current password. min 6 characters.">
            <TextInput type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" autoComplete="new-password" />
          </Field>
        )}
        <Field label="Email"><TextInput type="email" value={f.email || ''} onChange={set('email')} /></Field>
        <Field label="Mobile"><TextInput value={f.mobile || ''} onChange={set('mobile')} /></Field>
        <Field label="Employee ID"><TextInput value={f.employee_id || ''} onChange={set('employee_id')} /></Field>
        {!singleDivision && (
          <Field label="Division"><Select value={f.division_id || ''} onChange={(e) => setF((p) => {
            const id = e.target.value;
            const d = divisions.find((x) => String(x.id) === String(id));
            return { ...p, division_id: id ? Number(id) : null, division: d ? d.name : '' };
          })} options={divisions.map((d) => ({ value: d.id, label: d.name }))} /></Field>
        )}
        <Field label="Role"><Select value={f.role_id || ''} onChange={set('role_id')}
          options={assignableRoles.map((r) => ({ value: r.id, label: r.name }))} /></Field>
        <Field label="Hierarchy level"><Select value={f.hierarchy_level_id || ''} onChange={set('hierarchy_level_id')}
          options={levels.map((l) => ({ value: l.id, label: `${l.name} (${l.label})` }))} /></Field>
        <Field label="Reports to"><Select value={f.parent_id || ''} onChange={set('parent_id')}
          options={allUsers.filter((u) => u.id !== editing.id).map((u) => ({ value: u.id, label: u.full_name }))} /></Field>
        <Field label="Status"><Select value={f.status || 'active'} onChange={set('status')}
          options={['active', 'inactive', 'left'].map((o) => ({ value: o, label: o }))} /></Field>
        <Field label="Region"><TextInput value={f.region || ''} onChange={set('region')} /></Field>
        <Field label="Area"><TextInput value={f.area || ''} onChange={set('area')} /></Field>
        <Field label="Territory"><TextInput value={f.territory || ''} onChange={set('territory')} /></Field>
      </form>
    </Modal>
  );
}

function BulkUpload({ base, open, onClose, onDone }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    try {
      const r = await uploadFile(`${base}/users/bulk-upload`, file);
      setResult(r);
      if (r.created > 0) toast(`Created ${r.created} users`, 'success');
      if (r.errors?.length) toast(`${r.errors.length} row(s) failed`, 'error');
      if (!r.generated?.length) { try { onDone(); } catch (e) { /* ignore */ } }
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };
  const finish = () => { setFile(null); setResult(null); onDone(); };
  return (
    <Modal open={open} title="Bulk upload users" onClose={onClose} footer={
      result ? (
        <button className="btn btn-primary" onClick={finish}>Done</button>
      ) : (
        <>
          <button className="btn" onClick={() => { setFile(null); onClose(); }} disabled={false}>Cancel</button>
          <button className="btn btn-primary" form="bulk-upload-form" disabled={busy || !file}>{busy ? 'Uploading…' : 'Upload'}</button>
        </>
      )
    }>
      {result ? (
        <div>
          <p><Badge tone={result.generated?.length ? 'green' : 'gray'}>
            {result.created} created{result.errors?.length ? ` · ${result.errors.length} failed` : ''}
          </Badge></p>
          {result.generated?.length > 0 && (
            <div className="card" style={{ marginTop: 10 }}>
              <h5 className="form-section">Temporary passwords</h5>
              <p className="muted">Rows with a blank password got a random one. Share each with the user
                — they aren't shown again after this screen closes.</p>
              <table className="data-table">
                <tbody>
                  {result.generated.map((g) => (
                    <tr key={g.username}>
                      <td><strong>{g.full_name || g.username}</strong><div className="muted">@{g.username}</div></td>
                      <td><code className="mono">{g.temp_password}</code></td>
                      <td>
                        <button className="btn-link" onClick={() => {
                          navigator.clipboard?.writeText(g.temp_password);
                          toast(`Password copied for ${g.username}`, 'success');
                        }}>Copy</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {result.errors?.length > 0 && (
            <div className="card" style={{ marginTop: 10 }}>
              <h5 className="form-section">Rows skipped</h5>
              <ul className="muted" style={{ fontSize: 12, margin: 0, paddingLeft: 18 }}>
                {result.errors.map((m, i) => <li key={i}>{m}</li>)}
              </ul>
            </div>
          )}
        </div>
      ) : (
        <form id="bulk-upload-form" onSubmit={submit}>
          <p className="muted">Expected columns: username, full_name, password, email, mobile, employee_id,
            division, hierarchy_level, role, parent_username, region, area, territory.
            Leave <strong>password</strong> blank and a secure random temp password is generated and shown here.</p>
          <button type="button" className="btn btn-sm" onClick={() =>
            downloadFile(`${base}/hierarchy/bulk-template`, 'users_template.xlsx')}>Download template</button>
          <div style={{ margin: '12px 0' }}>
            <input type="file" accept=".xlsx,.xls" onChange={(e) => setFile(e.target.files[0])} required />
          </div>
        </form>
      )}
    </Modal>
  );
}
