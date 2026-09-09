import { useState } from 'react';
import { api, fmtDateTime, getSession } from '../../api';
import {
  ErrorBox, Field, PageHeader, Spinner, TextArea, TextInput, toast, useAsync,
} from '../../ui';

export default function Notifications() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/notifications/'));
  const templates = useAsync(() => api('/api/v1/notifications/templates'));
  const [editing, setEditing] = useState(null);
  const [showTemplates, setShowTemplates] = useState(false);
  const canManage = (getSession()?.permissions || []).includes('notification.manage');

  if (loading) return <Spinner label="Loading notifications…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const markRead = async (id) => {
    try { await api(`/api/v1/notifications/${id}/read`, { method: 'POST' }); run(); }
    catch (e) { toast(e.message, 'error'); }
  };
  const markAll = async () => {
    try { await api('/api/v1/notifications/read-all', { method: 'POST' }); toast('All marked read', 'success'); run(); }
    catch (e) { toast(e.message, 'error'); }
  };

  return (
    <div>
      <PageHeader title="Notifications" subtitle={`${data?.unread || 0} unread`}
        actions={
          <>
            {canManage && (
              <button className="btn" onClick={() => { setShowTemplates(!showTemplates); templates.run(); }}>
                {showTemplates ? 'Hide templates' : 'Manage templates'}
              </button>
            )}
            <button className="btn btn-primary" onClick={markAll}>Mark all read</button>
          </>
        } />
      <div className="notif-list">
        {(data?.items || []).length === 0 && <p className="muted">No notifications yet.</p>}
        {(data?.items || []).map((n) => (
          <div key={n.id} className={`notif ${n.is_read ? '' : 'unread'}`} onClick={() => !n.is_read && markRead(n.id)}>
            <div className="notif-head">
              <strong>{n.title || n.type}</strong>
              <span className="muted">{fmtDateTime(n.created_at)}</span>
            </div>
            <div>{n.message}</div>
            {!n.is_read && <span className="badge badge-blue">new</span>}
          </div>
        ))}
      </div>

      {showTemplates && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3 className="sub-head">Notification templates</h3>
          {templates.loading && <Spinner />}
          {templates.error && <ErrorBox error={templates.error} />}
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Code</th><th>Subject</th><th>Channel</th><th>Active</th><th></th></tr></thead>
              <tbody>
                {(templates.data?.items || []).map((t) => (
                  <tr key={t.id}>
                    <td><code>{t.code}</code></td>
                    <td>{t.subject}</td>
                    <td>{t.channel}</td>
                    <td>{t.active ? '✓' : '✕'}</td>
                    <td><button className="btn-link" onClick={() => setEditing(t)}>Edit</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {editing && (
        <TemplateModal template={editing} onClose={() => setEditing(null)}
          onDone={() => { setEditing(null); templates.run(); }} />
      )}
    </div>
  );
}

function TemplateModal({ template, onClose, onDone }) {
  const [f, setF] = useState(template);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));
  const submit = async (e) => {
    e.preventDefault();
    try {
      await api(`/api/v1/notifications/templates/${template.id}`, { method: 'PUT', body: f });
      toast('Template updated', 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); }
  };
  return (
    <Modal open wide title={`Edit template ${template.code}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="tmpl-form">Save</button></>}>
      <form id="tmpl-form" onSubmit={submit}>
        <Field label="Subject"><TextInput value={f.subject || ''} onChange={set('subject')} /></Field>
        <Field label="Body" hint={`Variables: ${(f.variables || []).map((v) => `{${v}}`).join(' ')}`}>
          <TextArea rows={4} value={f.body || ''} onChange={set('body')} /></Field>
        <Field label="Channel"><select className="input" value={f.channel || 'inapp'} onChange={set('channel')}>
          {['inapp', 'email', 'whatsapp', 'sms', 'push'].map((c) => <option key={c} value={c}>{c}</option>)}
        </select></Field>
        <label className="check"><input type="checkbox" checked={!!f.active}
          onChange={(e) => setF((p) => ({ ...p, active: e.target.checked }))} /> Active</label>
      </form>
    </Modal>
  );
}
