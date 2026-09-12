import { useMemo, useState } from 'react';
import { api } from '../../api';
import {
  Badge, EmptyState, ErrorBox, Field, Modal, PageHeader, SearchBox,
  Select, StatCard, StatSkeleton, Table, TableSkeleton, TextInput, toast, useAsync,
} from '../../ui';
import { saRole } from '../../api';

const ROLE_LABELS = {
  owner: 'Owner',
  full: 'Super Admin · full access',
  campaign_admin: 'Campaign Admin · campaign approvals',
  finance_admin: 'Finance Admin · gratification & payments',
  verification_admin: 'Verification Admin · POB verification',
  division_admin: 'Division Admin · create & manage divisions',
};

const ROLE_OPTIONS = [
  { value: 'full', label: 'Full access' },
  { value: 'campaign_admin', label: 'Campaign Admin' },
  { value: 'finance_admin', label: 'Finance Admin' },
  { value: 'verification_admin', label: 'Verification Admin' },
  { value: 'division_admin', label: 'Division Admin' },
];

const TONES = { full: 'blue', campaign_admin: 'teal', finance_admin: 'green', verification_admin: 'amber', division_admin: 'teal' };

export default function PlatformAdmins() {
  const me = saRole();
  const { data, loading, error, run } = useAsync(() => api('/api/v1/superadmin/platform-admins'));
  const [q, setQ] = useState('');

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({ role: 'full', status: 'active', username: '', password: '', full_name: '', email: '' });
  const [busy, setBusy] = useState(false);

  const resetForm = () => setForm({ role: 'full', status: 'active', username: '', password: '', full_name: '', email: '' });

  const startCreate = () => { setEditing(null); resetForm(); setOpen(true); };
  const startEdit = (r) => {
    setEditing(r);
    setForm({
      role: r.role, status: r.status, username: r.username,
      password: '', full_name: r.full_name || '', email: r.email || '',
    });
    setOpen(true);
  };

  const save = async () => {
    if (!form.username.trim()) { toast('Username is required', 'error'); return; }
    if (!editing && form.password.length < 6) { toast('Password must be at least 6 characters', 'error'); return; }
    setBusy(true);
    try {
      if (editing) {
        await api(`/api/v1/superadmin/platform-admins/${editing.id}`, { method: 'PUT', body: form });
        toast('Platform admin updated', 'success');
      } else {
        await api('/api/v1/superadmin/platform-admins', { method: 'POST', body: form });
        toast('Platform admin created', 'success');
      }
      setOpen(false); run();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  const rows = useMemo(() => {
    const items = data?.items || [];
    if (!q) return items;
    const n = q.toLowerCase();
    return items.filter((a) =>
      (a.username || '').toLowerCase().includes(n) ||
      (a.full_name || '').toLowerCase().includes(n) || a.id === Number(n));
  }, [data, q]);

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'username', label: 'Username' },
    { key: 'full_name', label: 'Name', render: (r) => r.full_name || '—' },
    { key: 'email', label: 'Email', render: (r) => r.email || '—' },
    { key: 'role', label: 'Role', render: (r) =>
      <Badge tone={TONES[r.role] || 'gray'}>{ROLE_LABELS[r.role] || r.role}</Badge> },
    { key: 'status', label: 'Status', render: (r) =>
      <Badge tone={r.status === 'active' ? 'green' : 'red'}>{r.status}</Badge> },
    { key: 'last_login', label: 'Last login', render: (r) => r.last_login ? String(r.last_login).slice(0, 16).replace('T', ' ') : '—' },
    {
      key: '_actions', label: '', render: (r) => r.role === 'owner' ? (
        <span className="muted">—</span>
      ) : (
        <button className="btn btn-sm" onClick={() => startEdit(r)}>Edit</button>
      ),
    },
  ];

  const header = (
    <PageHeader title="Platform Admins"
      subtitle="People who can sign into this platform console and what they are allowed to administer."
      actions={
        me === 'owner'
          ? <button className="btn btn-primary" onClick={startCreate}>+ Add admin</button>
          : <SearchBox value={q} onChange={setQ} placeholder="Search admins…" />
      } />
  );

  if (loading) return <div>{header}<StatSkeleton n={3} /><TableSkeleton cols={7} rows={5} /></div>;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const owners = (data?.items || []).filter((a) => a.role === 'owner').length;

  return (
    <div>
      {header}
      <div className="stats-grid compact">
        <StatCard label="Total admins" value={(data?.items || []).length} />
        <StatCard label="Owners" value={owners} tone="red" />
        <StatCard label="Specialised roles" value={(data?.items || []).filter((a) => !['owner', 'full'].includes(a.role)).length} tone="blue" />
      </div>
      {rows.length
        ? <Table cols={cols} rows={rows} keyOf={(r) => r.id}
            empty={<EmptyState text="No platform admins match this view" />} />
        : <EmptyState text="No platform admins yet" />}

      <Modal open={open} onClose={() => setOpen(false)} title={editing ? `Edit admin ${editing.username}` : 'Add a platform admin'}
        footer={<>
          <button className="btn" onClick={() => setOpen(false)}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={busy}>
            {busy ? 'Saving…' : editing ? 'Save changes' : 'Create admin'}
          </button>
        </>}>
        <Field label="Username" required hint="Used to sign in to the platform console.">
          <TextInput value={form.username} disabled={!!editing} placeholder="e.g. finance.ops"
            onChange={(e) => setForm({ ...form, username: e.target.value })} />
        </Field>
        <Field label="Full name">
          <TextInput value={form.full_name} placeholder="e.g. Rajesh Sharma"
            onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
        </Field>
        <Field label="Email">
          <TextInput type="email" value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </Field>
        <Field label="Role" hint="Specialised roles only see their own area of the console. Owners and full-access admins see everything.">
          <Select value={form.role} disabled={!!editing && form.role === 'owner'}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
            options={ROLE_OPTIONS.map((o) => ({ ...o }))} />
        </Field>
        {!editing && (
          <Field label="Password" required hint="At least 6 characters. The admin can log in with this immediately.">
            <TextInput type="password" value={form.password} placeholder="••••••"
              onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </Field>
        )}
        {editing && (
          <Field label="Reset password" hint="Leave blank to keep the current password.">
            <TextInput type="password" value={form.password} placeholder="New password (optional)"
              onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </Field>
        )}
      </Modal>
    </div>
  );
}