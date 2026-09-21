import { useMemo, useState } from 'react';
import { api } from '../../api';
import {
  Badge, EmptyState, ErrorBox, Field, Modal, PageHeader, SearchBox, Select,
  StatCard, StatSkeleton, Table, TableSkeleton, Tabs, TextInput, toast, useAsync,
} from '../../ui';

const TABS = [
  { value: '', label: 'All' },
  { value: 'active', label: 'Active' },
  { value: 'inactive', label: 'Inactive' },
];

const initialsOf = (a) => (a.full_name || a.username || '?').trim()
  .split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();

function genPassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
  let out = '';
  for (let i = 0; i < 12; i++) out += chars[Math.floor(Math.random() * chars.length)];
  return out;
}

export default function VerificationAgents() {
  const [tab, setTab] = useState('');
  const [q, setQ] = useState('');
  // Always fetch the full cross-division list (unfiltered) so the stat tiles
  // and tab badges reflect the true total regardless of which tab is active
  // -- filtering by tab/search happens client-side, same as the search box.
  const { data, loading, error, run } = useAsync(
    () => api('/api/v1/superadmin/verification-agents'));
  const divisions = useAsync(() => api('/api/v1/superadmin/divisions'));

  const [adding, setAdding] = useState(false);
  const [detail, setDetail] = useState(null);

  const rows = useMemo(() => {
    let items = data?.items || [];
    if (tab) items = items.filter((a) => a.status === tab);
    if (!q) return items;
    const n = q.toLowerCase();
    return items.filter((a) =>
      (a.full_name || '').toLowerCase().includes(n) ||
      (a.username || '').toLowerCase().includes(n) ||
      (a.division_name || '').toLowerCase().includes(n));
  }, [data, tab, q]);

  const counts = useMemo(() => {
    const items = data?.items || [];
    return {
      active: items.filter((a) => a.status === 'active').length,
      inactive: items.filter((a) => a.status !== 'active').length,
    };
  }, [data]);

  const toggle = async (a) => {
    const next = a.status === 'active' ? 'inactive' : 'active';
    if (!window.confirm(`${next === 'active' ? 'Activate' : 'Deactivate'} ${a.full_name || a.username}?`)) return;
    try {
      await api(`/api/v1/superadmin/verification-agents/${a.division_id}/${a.id}`,
        { method: 'PUT', body: { status: next } });
      toast(next === 'active' ? 'Agent activated' : 'Agent deactivated', 'success');
      run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const openDetail = async (a) => {
    setDetail({ loading: true, agent: a });
    try {
      const full = await api(`/api/v1/superadmin/verification-agents/${a.division_id}/${a.id}`);
      setDetail({ loading: false, agent: { ...a, ...full } });
    } catch (e) {
      toast(e.message, 'error');
      setDetail(null);
    }
  };

  const approvalRate = (a) => {
    const decided = (a.approved || 0) + (a.rejected || 0);
    return decided ? Math.round((a.approved / decided) * 100) : null;
  };

  const cols = [
    { key: 'agent', label: 'Agent', render: (a) => (
      <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span className="admin-avatar">{initialsOf(a)}</span>
        <span>
          <strong>{a.full_name || a.username}</strong>
          <br /><span className="muted">@{a.username}</span>
        </span>
      </span>
    ) },
    { key: 'division_name', label: 'Division', render: (a) => <Badge tone="blue">{a.division_name}</Badge> },
    { key: 'status', label: 'Status', render: (a) => <Badge tone={a.status === 'active' ? 'green' : 'gray'}>{a.status}</Badge> },
    { key: 'verified_total', label: 'Verified', render: (a) => a.verified_total ?? 0 },
    { key: 'approval_rate', label: 'Approval rate', render: (a) => {
      const r = approvalRate(a);
      return r == null ? <span className="muted">—</span> : `${r}%`;
    } },
    { key: 'avg_tat_hours', label: 'Avg TAT', render: (a) => a.avg_tat_hours != null ? `${a.avg_tat_hours} h` : <span className="muted">—</span> },
    { key: '_actions', label: '', render: (a) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => openDetail(a)}>Activity</button>
        <button className="btn-link" onClick={() => toggle(a)}>{a.status === 'active' ? 'Deactivate' : 'Activate'}</button>
      </span>
    ) },
  ];

  const header = (
    <PageHeader title="Verification Agents"
      subtitle="Add, assign and monitor the division-wise agents who verify POBs — normal verification decisions stay with them, not this console."
      actions={
        <>
          <SearchBox value={q} onChange={setQ} placeholder="Search agent / division…" />
          <Tabs items={TABS.map((t) => ({ ...t, badge: counts[t.value] }))} active={tab} onChange={setTab} />
          <button className="btn btn-primary" onClick={() => setAdding(true)}>+ Add Agent</button>
        </>
      } />
  );

  if (loading) return <div>{header}<StatSkeleton n={3} /><TableSkeleton cols={7} rows={6} /></div>;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const total = (data?.items || []).length;

  return (
    <div>
      {header}
      <div className="stats-grid compact">
        <StatCard label="Total agents" value={total} />
        <StatCard label="Active" value={counts.active} tone="green" />
        <StatCard label="Inactive" value={counts.inactive} tone="gray" />
      </div>
      {data?.unreachable?.length > 0 && (
        <p className="muted">Unreachable divisions: {data.unreachable.map((u) => u.name).join(', ')}</p>)}
      {rows.length
        ? <Table cols={cols} rows={rows} keyOf={(a) => `${a.division_id}-${a.id}`}
            empty={<EmptyState text="No agents match this view" />} />
        : <EmptyState text="No verification agents yet — add one to get started" />}

      {adding && (
        <AddAgentModal divisions={divisions.data?.items || []}
          onClose={() => setAdding(false)} onDone={() => { setAdding(false); run(); }} />
      )}
      {detail && (
        <AgentDetailModal state={detail} onClose={() => setDetail(null)} />
      )}
    </div>
  );
}

function AddAgentModal({ divisions, onClose, onDone }) {
  const [f, setF] = useState({ division_id: '', full_name: '', username: '', email: '', mobile: '' });
  const [password, setPassword] = useState(genPassword());
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (!f.division_id) { const m = 'Choose a division'; setError(m); toast(m, 'error'); return; }
    if (!f.full_name.trim() || !f.username.trim()) {
      const m = 'Full name and username are required'; setError(m); toast(m, 'error'); return;
    }
    if (password.length < 6) {
      const m = 'Password must be at least 6 characters'; setError(m); toast(m, 'error'); return;
    }
    setBusy(true);
    try {
      await api('/api/v1/superadmin/verification-agents', {
        method: 'POST', body: { ...f, division_id: Number(f.division_id), password },
      });
      toast(`Agent account created for ${f.full_name}`, 'success');
      onDone();
    } catch (err) {
      const msg = err.message || 'Create failed';
      setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };

  return (
    <Modal open wide title="Add a verification agent" onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="agent-new-form" disabled={busy}>{busy ? 'Creating…' : 'Create agent'}</button>
      </>}>
      <form id="agent-new-form" onSubmit={submit}>
        {error && <ErrorBox error={error} />}
        <h5 className="form-section">Assignment</h5>
        <div className="grid-2">
          <Field label="Division" required hint="Which division this agent verifies POBs for">
            <Select value={f.division_id} onChange={set('division_id')}
              options={[{ value: '', label: 'Choose a division…' },
                ...divisions.map((d) => ({ value: String(d.id), label: `${d.name} (${d.code})` }))]} />
          </Field>
        </div>

        <h5 className="form-section">Profile</h5>
        <div className="grid-2">
          <Field label="Full name" required>
            <TextInput value={f.full_name} onChange={set('full_name')} required autoFocus />
          </Field>
          <Field label="Username" required hint="Used to sign in — cannot be changed later">
            <TextInput value={f.username} onChange={set('username')} required />
          </Field>
          <Field label="Email"><TextInput type="email" value={f.email} onChange={set('email')} /></Field>
          <Field label="Mobile"><TextInput value={f.mobile} onChange={set('mobile')} /></Field>
        </div>

        <h5 className="form-section">Password</h5>
        <div className="grid-2">
          <Field label="Initial password" required hint="Share this with the new agent — min 6 characters">
            <div style={{ display: 'flex', gap: 8 }}>
              <TextInput value={password} onChange={(e) => setPassword(e.target.value)} required />
              <button type="button" className="btn btn-sm" onClick={() => setPassword(genPassword())}>Regenerate</button>
            </div>
          </Field>
        </div>
      </form>
    </Modal>
  );
}

function AgentDetailModal({ state, onClose }) {
  const { loading, agent } = state;
  return (
    <Modal open wide title={`Activity — ${agent.full_name || agent.username}`} onClose={onClose}
      footer={<button className="btn" onClick={onClose}>Close</button>}>
      {loading ? <p className="muted">Loading…</p> : (
        <div>
          <div className="kv-grid" style={{ marginBottom: 14 }}>
            <span>Division <strong>{agent.division_name}</strong></span>
            <span>Status <Badge tone={agent.status === 'active' ? 'green' : 'gray'}>{agent.status}</Badge></span>
            <span>Email <strong>{agent.email || '—'}</strong></span>
            <span>Mobile <strong>{agent.mobile || '—'}</strong></span>
          </div>
          <h5 className="form-section">Recent verification decisions</h5>
          {(agent.recent_activity || []).length ? (
            <div className="timeline">
              {agent.recent_activity.map((r) => (
                <div key={r.id} className="tl-item">
                  <span className="tl-dot" />
                  <div>
                    <strong>{r.status}</strong> — {r.campaign_name || 'Campaign'} · {r.chemist_name || 'Chemist'}
                    {r.invoice_number && <span className="muted"> · Inv {r.invoice_number}</span>}
                    {r.reason && <div className="muted">{r.reason}</div>}
                  </div>
                </div>
              ))}
            </div>
          ) : <p className="muted">No verification decisions yet.</p>}
        </div>
      )}
    </Modal>
  );
}
