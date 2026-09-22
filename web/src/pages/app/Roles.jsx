import { useState } from 'react';
import { api } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, Select, Spinner, Table, TextInput, toast, useAsync,
} from '../../ui';

export default function Roles({ base = '/api/v1' }) {
  const { data, loading, error, run } = useAsync(() => api(`${base}/roles`));
  const perms = useAsync(() => api(`${base}/permissions`));
  const levels = useAsync(() => api(`${base}/hierarchy/levels`));
  const [editing, setEditing] = useState(null);

  if (loading) return <Spinner label="Loading roles…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const levelById = Object.fromEntries((levels.data?.items || []).map((l) => [l.id, l]));

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'name', label: 'Role', render: (r) => <span><strong>{r.name}</strong>{r.is_system && <Badge tone="gray">system</Badge>}</span> },
    { key: 'data_entry', label: 'Data entry', render: (r) => r.data_entry ? <Badge tone="green">enabled</Badge> : <Badge tone="gray">no</Badge> },
    { key: 'description', label: 'Description' },
    { key: 'default_hierarchy_level_id', label: 'Hierarchy level', render: (r) => {
      const lv = levelById[r.default_hierarchy_level_id];
      return lv ? <Badge tone="teal">{lv.name}</Badge> : <span className="muted">—</span>;
    } },
    { key: 'perm_count', label: 'Permissions', render: (r) => <Badge tone="blue">{r.perm_count}</Badge> },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
        {!r.is_system && (
          <button className="btn-link danger" onClick={async () => {
            if (!window.confirm(`Delete role "${r.name}"?`)) return;
            try { await api(`${base}/roles/${r.id}`, { method: 'DELETE' }); toast('Role deleted', 'success'); run(); }
            catch (err) { toast(err.message, 'error'); }
          }}>Delete</button>
        )}
      </span>
    ) },
  ];

  return (
    <div>
      <PageHeader title="Roles & Permissions" subtitle="What each role can do across modules"
        actions={<button className="btn btn-primary" onClick={() => setEditing({ permissions: [] })}>+ New role</button>} />
      <Table cols={cols} rows={data?.items || []} keyOf={(r) => r.id}
        onRowClick={(r) => setEditing({ ...r })} empty="No roles" />
      {editing && (
        <RoleModal editing={editing} base={base} allPerms={perms.data?.items || []}
          allLevels={levels.data?.items || []} isEdit={!!editing.id}
          onClose={() => setEditing(null)} onDone={() => { setEditing(null); run(); }} />
      )}
    </div>
  );
}

function RoleModal({ editing, allPerms, allLevels, isEdit, onClose, onDone, base }) {
  const [name, setName] = useState(editing.name || '');
  const [desc, setDesc] = useState(editing.description || '');
  const [dataEntry, setDataEntry] = useState(!!editing.data_entry);
  const [perms, setPerms] = useState(editing.permissions || []);
  const [levelId, setLevelId] = useState(editing.default_hierarchy_level_id || '');

  const groups = {};
  allPerms.forEach((p) => {
    (groups[p.module] = groups[p.module] || []).push(p);
  });

  const toggle = (code) => {
    setPerms((prev) => prev.includes(code) ? prev.filter((x) => x !== code) : [...prev, code]);
  };
  const toggleGroup = (module, list) => {
    const allOn = list.every((p) => perms.includes(p.code));
    setPerms((prev) => allOn ? prev.filter((x) => !list.some((p) => p.code === x)) : [...new Set([...prev, ...list.map((p) => p.code)])]);
  };

  const submit = async (e) => {
    e.preventDefault();
    try {
      const body = { name, description: desc, permissions: perms, data_entry: dataEntry,
        default_hierarchy_level_id: levelId ? Number(levelId) : null };
      if (isEdit) await api(`${base}/roles/${editing.id}`, { method: 'PUT', body });
      else await api(`${base}/roles`, { method: 'POST', body });
      toast(isEdit ? 'Role updated' : 'Role created', 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); }
  };

  return (
    <Modal open wide title={isEdit ? `Edit ${editing.name}` : 'New role'} onClose={onClose}
      footer={<>
        {isEdit && !editing.is_system && (
          <button className="btn btn-danger" onClick={async () => {
            try { await api(`${base}/roles/${editing.id}`, { method: 'DELETE' }); toast('Role deleted', 'success'); onDone(); }
            catch (err) { toast(err.message, 'error'); }
          }}>Delete</button>
        )}
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="role-form">Save</button>
      </>}>
      <form id="role-form" onSubmit={submit}>
        <div className="grid-2">
          <Field label="Role name" required><TextInput value={name} onChange={(e) => setName(e.target.value)} required /></Field>
          <Field label="Description"><TextInput value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
        </div>
        <Field label="Default hierarchy level"
          hint="If this role sits at a fixed spot in the reporting chain (e.g. MR, ASM), pick it here — Users.jsx will then reject saving a user with this role at any other level. Leave blank for roles outside the chain (distributor, finance, verifier, ...).">
          <Select value={levelId} onChange={(e) => setLevelId(e.target.value)}
            placeholder="Not tied to a level"
            options={allLevels.map((l) => ({ value: l.id, label: `${l.name} (${l.label})` }))} />
        </Field>
        <label className="check" style={{ marginTop: 12 }}>
          <input type="checkbox" checked={dataEntry} onChange={(e) => setDataEntry(e.target.checked)} />
          <strong>Data entry role</strong>
          <span className="muted"> — users with this role can register chemists, submit POBs, upload invoices and record visits (shows the field data-entry menu).</span>
        </label>
        <h4 className="sub-head">Permissions</h4>
        {Object.entries(groups).map(([module, list]) => {
          const allOn = list.every((p) => perms.includes(p.code));
          return (
            <div key={module} className="perm-group">
              <label className="check">
                <input type="checkbox" checked={allOn} onChange={() => toggleGroup(module, list)} />
                <strong>{module}</strong>
              </label>
              <div className="perm-list">
                {list.map((p) => (
                  <label key={p.code} className="check">
                    <input type="checkbox" checked={perms.includes(p.code)} onChange={() => toggle(p.code)} />
                    <code>{p.code}</code> {p.label}
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </form>
    </Modal>
  );
}
