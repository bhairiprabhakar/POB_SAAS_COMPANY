import { useMemo, useState } from 'react';
import { api } from '../../api';
import {
  Badge, EmptyState, ErrorBox, Field, Modal, PageHeader, SearchBox,
  Select, StatCard, StatSkeleton, Table, TableSkeleton, TextInput, toast, useAsync,
} from '../../ui';
import { saRole } from '../../api';

const ROLE_LABELS = {
  owner: 'Owner',
  full: 'Super Admin · full access (legacy)',
  campaign_admin: 'Campaign Admin · campaign approvals',
  finance_admin: 'Finance Admin · gratification & payments',
  verification_admin: 'Verification Admin · POB verification',
  platform_division_admin: 'Platform Division Admin · create & manage divisions',
  co_owner: 'Co-Owner · full access, founder-managed',
};

// 'full' is intentionally left out here -- it's a legacy unrestricted role the
// console no longer offers for new admins (see ROLE_LABELS.full and the
// editing-only fallback in the role Select below, which keeps it visible only
// for an admin who already has it).
const ROLE_OPTIONS = [
  { value: 'campaign_admin', label: 'Campaign Admin' },
  { value: 'finance_admin', label: 'Finance Admin' },
  { value: 'verification_admin', label: 'Verification Admin' },
  { value: 'platform_division_admin', label: 'Platform Division Admin (create & manage divisions)' },
];

const TONES = { full: 'blue', campaign_admin: 'teal', finance_admin: 'green', verification_admin: 'amber', platform_division_admin: 'teal', co_owner: 'red' };
const MAX_CO_OWNERS = 3;

export default function PlatformAdmins() {
  const me = saRole();
  const { data, loading, error, run } = useAsync(() => api('/api/v1/superadmin/platform-admins'));
  const [q, setQ] = useState('');

  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({ role: 'campaign_admin', status: 'active', username: '', password: '', full_name: '', email: '' });
  const [busy, setBusy] = useState(false);
  const [promoting, setPromoting] = useState(null);
  const [revoking, setRevoking] = useState(null);

  const resetForm = () => setForm({ role: 'campaign_admin', status: 'active', username: '', password: '', full_name: '', email: '' });

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

  const toggleStatus = async (r) => {
    const next = r.status === 'active' ? 'suspended' : 'active';
    const verb = next === 'suspended' ? 'Suspend' : 'Reactivate';
    if (!window.confirm(`${verb} ${r.full_name || r.username}?`)) return;
    try {
      await api(`/api/v1/superadmin/platform-admins/${r.id}`, { method: 'PUT', body: { status: next } });
      toast(`${r.full_name || r.username} ${next === 'suspended' ? 'suspended' : 'reactivated'}`, 'success');
      run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const rows = useMemo(() => {
    const items = data?.items || [];
    if (!q) return items;
    const n = q.toLowerCase();
    return items.filter((a) =>
      (a.username || '').toLowerCase().includes(n) ||
      (a.full_name || '').toLowerCase().includes(n) || a.id === Number(n));
  }, [data, q]);

  const coOwnerCount = (data?.items || []).filter((a) => a.role === 'co_owner').length;

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
      key: '_actions', label: '', render: (r) => {
        if (r.role === 'owner') return <span className="muted">—</span>;
        // Only the founder may edit/suspend a co-owner row (matches the
        // backend's require_founder guard) -- a co-owner viewing another
        // co-owner (or their own row) gets no controls here, avoiding a
        // dead-end 403 click.
        if (r.role === 'co_owner' && me !== 'owner') return <span className="muted">—</span>;
        return (
          <span className="row-actions">
            <button className="btn btn-sm" onClick={() => startEdit(r)}>Edit</button>
            <button className={`btn btn-sm ${r.status === 'active' ? 'btn-danger' : ''}`} onClick={() => toggleStatus(r)}>
              {r.status === 'active' ? 'Suspend' : 'Reactivate'}
            </button>
            {me === 'owner' && r.role === 'co_owner' && (
              <button className="btn btn-sm" onClick={() => setRevoking(r)}>Revoke co-owner</button>
            )}
            {me === 'owner' && r.role !== 'co_owner' && coOwnerCount < MAX_CO_OWNERS && (
              <button className="btn btn-sm" onClick={() => setPromoting(r)}>Make co-owner</button>
            )}
          </span>
        );
      },
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
        <StatCard label="Co-owners" value={`${coOwnerCount} / ${MAX_CO_OWNERS}`} tone="red" />
        <StatCard label="Specialised roles" value={(data?.items || []).filter((a) => !['owner', 'full', 'co_owner'].includes(a.role)).length} tone="blue" />
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
        <Field label="Role" hint="Specialised roles only see their own area of the console.">
          <Select value={form.role} disabled={!!editing && form.role === 'owner'}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
            options={editing?.role === 'full'
              ? [{ value: 'full', label: ROLE_LABELS.full }, ...ROLE_OPTIONS]
              : ROLE_OPTIONS} />
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

      <PromoteCoOwnerModal admin={promoting} onClose={() => setPromoting(null)}
        onDone={() => { setPromoting(null); run(); }} />
      <RevokeCoOwnerModal admin={revoking} onClose={() => setRevoking(null)}
        onDone={() => { setRevoking(null); run(); }} />
    </div>
  );
}

function PromoteCoOwnerModal({ admin, onClose, onDone }) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  if (!admin) return null;
  const confirm = async () => {
    if (!password) { toast('Enter your current password to confirm', 'error'); return; }
    setBusy(true);
    try {
      await api(`/api/v1/superadmin/platform-admins/${admin.id}/co-owner`, { method: 'POST', body: { password } });
      toast(`${admin.full_name || admin.username} is now a co-owner`, 'success');
      setPassword(''); onDone();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };
  return (
    <Modal open onClose={onClose} title={`Make ${admin.full_name || admin.username} a co-owner`}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" onClick={confirm} disabled={busy}>{busy ? 'Confirming…' : 'Promote to co-owner'}</button>
      </>}>
      <p className="muted">
        A co-owner gets full platform access, including managing the other specialised admins —
        exactly like you, except only you (the founder) can ever edit, suspend, or revoke a
        co-owner. Confirm with your own password to continue.
      </p>
      <Field label="Your password" required>
        <TextInput type="password" value={password} placeholder="••••••"
          onChange={(e) => setPassword(e.target.value)} />
      </Field>
    </Modal>
  );
}

function RevokeCoOwnerModal({ admin, onClose, onDone }) {
  const [password, setPassword] = useState('');
  const [fallbackRole, setFallbackRole] = useState('campaign_admin');
  const [busy, setBusy] = useState(false);
  if (!admin) return null;
  const confirm = async () => {
    if (!password) { toast('Enter your current password to confirm', 'error'); return; }
    setBusy(true);
    try {
      await api(`/api/v1/superadmin/platform-admins/${admin.id}/co-owner/revoke`,
        { method: 'POST', body: { password, fallback_role: fallbackRole } });
      toast(`Co-owner access revoked for ${admin.full_name || admin.username}`, 'success');
      setPassword(''); onDone();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };
  return (
    <Modal open onClose={onClose} title={`Revoke co-owner access for ${admin.full_name || admin.username}`}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-danger" onClick={confirm} disabled={busy}>{busy ? 'Confirming…' : 'Revoke co-owner'}</button>
      </>}>
      <Field label="Fall back to role" required>
        <Select value={fallbackRole} onChange={(e) => setFallbackRole(e.target.value)} options={ROLE_OPTIONS} placeholder={false} />
      </Field>
      <Field label="Your password" required hint="Confirm with your own password to continue.">
        <TextInput type="password" value={password} placeholder="••••••"
          onChange={(e) => setPassword(e.target.value)} />
      </Field>
    </Modal>
  );
}