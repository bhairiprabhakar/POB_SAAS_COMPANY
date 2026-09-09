import { useState } from 'react';
import { api } from '../../api';
import {
  ErrorBox, Field, Modal, PageHeader, Select, Spinner, TextInput, toast, useAsync,
} from '../../ui';

function TreeNode({ node, depth = 0, onEdit, onDelete }) {
  const [open, setOpen] = useState(true);
  return (
    <div className={`tree-node`} style={{ paddingLeft: depth * 22 }}>
      <div className="tree-row">
        <button className={`tree-toggle${open ? ' open' : ''}`} onClick={() => setOpen(!open)} disabled={!node.children?.length}>
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
          <span className="muted"> — {node.users.map((u) => u.full_name).join(', ')}</span>
        )}
        <span className="row-actions">
          <button className="btn-link" onClick={() => onEdit(node)}>Edit</button>
          <button className="btn-link danger" onClick={() => onDelete(node)}>Delete</button>
        </span>
      </div>
      {open && (node.children || []).map((ch) => (
        <TreeNode key={ch.id} node={ch} depth={depth + 1} onEdit={onEdit} onDelete={onDelete} />
      ))}
    </div>
  );
}

export default function Hierarchy({ base = '/api/v1' }) {
  const { data, loading, error, run } = useAsync(() => api(`${base}/hierarchy/tree`));
  const levels = useAsync(() => api(`${base}/hierarchy/levels`));
  const [editing, setEditing] = useState(null);

  if (loading) return <Spinner label="Loading hierarchy…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const submit = async (e) => {
    e.preventDefault();
    try {
      if (editing.id) {
        await api(`${base}/hierarchy/levels/${editing.id}`, { method: 'PUT', body: editing });
      } else {
        await api(`${base}/hierarchy/levels`, { method: 'POST', body: editing });
      }
      toast(editing.id ? 'Level updated' : 'Level created', 'success');
      setEditing(null); run(); levels.run();
    } catch (err) { toast(err.message, 'error'); }
  };
  const set = (k) => (e) => setEditing((p) => ({ ...p, [k]: e.target.value }));

  const del = async (node) => {
    if (!window.confirm(`Delete hierarchy level "${node.name}"?`)) return;
    try {
      await api(`${base}/hierarchy/levels/${node.id}`, { method: 'DELETE' });
      toast('Level deleted', 'success');
      run(); levels.run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const allLevels = levels.data?.items || [];
  const parentOpts = allLevels.filter((l) => !editing?.id || l.id !== editing.id)
    .map((l) => ({ value: l.id, label: `${l.name} (rank ${l.rank})` }));

  return (
    <div>
      <PageHeader title="Reporting Hierarchy" subtitle="Dynamic levels and who reports to whom — configure per company"
        actions={<button className="btn btn-primary" onClick={() => setEditing({})}>+ Add level</button>} />
      <div className="card">
        {(data?.items || []).map((node) => (
          <TreeNode key={node.id} node={node} onEdit={setEditing} onDelete={del} />
        ))}
        {!data?.items?.length && <p className="muted">No hierarchy levels defined.</p>}
      </div>

      {editing && (
        <Modal open title={editing.id ? 'Edit level' : 'New hierarchy level'} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="lv-form">Save</button>
          </>}>
          <form id="lv-form" className="grid-2" onSubmit={submit}>
            <Field label="Name" required hint="Short code, e.g. ASM"><TextInput value={editing.name || ''} onChange={set('name')} required /></Field>
            <Field label="Label"><TextInput value={editing.label || ''} onChange={set('label')} /></Field>
            <Field label="Rank" hint="Higher rank = more senior"><TextInput type="number" value={editing.rank ?? ''} onChange={set('rank')} /></Field>
            <Field label="Reports to level"><Select value={editing.parent_level_id || ''} onChange={set('parent_level_id')} options={parentOpts} /></Field>
          </form>
        </Modal>
      )}
    </div>
  );
}
