import { useState } from 'react';
import { api, fmtDateTime } from '../../api';
import {
  ErrorBox, Field, Modal, PageHeader, Select, Spinner, Tabs, TextInput,
  toast, useAsync,
} from '../../ui';

export default function Security() {
  const [tab, setTab] = useState('mfa');
  return (
    <div>
      <PageHeader title="Security" subtitle="Two-factor auth, API keys and webhook endpoints" />
      <Tabs items={[
        { value: 'mfa', label: 'Two-factor auth' },
        { value: 'apikeys', label: 'API keys' },
        { value: 'webhooks', label: 'Webhooks' },
      ]} active={tab} onChange={setTab} />
      <div style={{ marginTop: 16 }}>
        {tab === 'mfa' && <MfaTab />}
        {tab === 'apikeys' && <ApiKeysTab />}
        {tab === 'webhooks' && <WebhooksTab />}
      </div>
    </div>
  );
}

// ── MFA ─────────────────────────────────────────────────────────────────────

function MfaTab() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/auth/mfa/status'));
  const [setup, setSetup] = useState(null);
  const [code, setCode] = useState('');

  const enable = async (e) => {
    e.preventDefault();
    try {
      await api('/api/v1/auth/mfa/enable', { method: 'POST', body: { code } });
      toast('Two-factor auth enabled', 'success');
      setSetup(null); setCode(''); run();
    } catch (err) { toast(err.message, 'error'); }
  };
  const disable = async () => {
    const c = window.prompt('Enter your current authenticator code to disable 2FA');
    if (!c) return;
    try {
      await api('/api/v1/auth/mfa/disable', { method: 'POST', body: { code: c } });
      toast('Two-factor auth disabled', 'success'); run();
    } catch (err) { toast(err.message, 'error'); }
  };

  if (loading) return <Spinner />;
  if (error) return <ErrorBox error={error} onRetry={run} />;
  return (
    <div className="card">
      <h3 className="sub-head">Two-factor authentication</h3>
      <p className="muted">
        {data?.enabled
          ? '2FA is enabled. Every sign-in now requires a code from your authenticator app.'
          : '2FA is off. Enable it with any TOTP app (Google Authenticator, Authy, 1Password).'}
      </p>
      {data?.enabled ? (
        <button className="btn" onClick={disable}>Disable 2FA</button>
      ) : (
        <button className="btn btn-primary" onClick={async () => {
          try { setSetup(await api('/api/v1/auth/mfa/setup', { method: 'POST' })); }
          catch (err) { toast(err.message, 'error'); }
        }}>Enable 2FA</button>
      )}
      {setup && (
        <div className="card" style={{ marginTop: 16 }}>
          <h4>Scan this QR with your authenticator app</h4>
          <p className="muted">No camera handy? Enter the secret manually:</p>
          <div className="qr-uri"><code>{setup.otpauth_uri}</code></div>
          <Field label="Secret">
            <TextInput readOnly value={setup.secret} />
          </Field>
          <form onSubmit={enable}>
            <Field label="Verify with a code" hint="Type the 6-digit code from your app to confirm it works">
              <TextInput value={code} onChange={(e) => setCode(e.target.value)}
                placeholder="000000" maxLength={6} required />
            </Field>
            <button className="btn btn-primary">Confirm & enable</button>
          </form>
        </div>
      )}
    </div>
  );
}

// ── API keys ────────────────────────────────────────────────────────────────

const ALL_SCOPES = ['pob.view', 'pob.submit', 'verification.view', 'gratification.view',
  'report.view', 'report.export', 'campaign.view', 'product.view', 'chemist.view',
  'dashboard.view', 'notification.view', 'audit.view'];

function ApiKeysTab() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/apikeys'));
  const [open, setOpen] = useState(false);
  const [created, setCreated] = useState(null);

  const revoke = async (id) => {
    if (!window.confirm('Revoke this API key? It will stop working immediately.')) return;
    try { await api(`/api/v1/apikeys/${id}`, { method: 'DELETE' }); toast('Key revoked', 'success'); run(); }
    catch (err) { toast(err.message, 'error'); }
  };

  if (loading) return <Spinner />;
  if (error) return <ErrorBox error={error} onRetry={run} />;
  return (
    <div>
      <div className="card">
        <div className="card-head">
          <h3 className="sub-head">API keys</h3>
          <button className="btn btn-primary" onClick={() => setOpen(true)}>New key</button>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Name</th><th>Key</th><th>Scopes</th><th>Status</th><th>Last used</th><th></th></tr></thead>
            <tbody>
              {(data?.items || []).map((k) => (
                <tr key={k.id}>
                  <td>{k.name}</td>
                  <td><code>{k.key}</code></td>
                  <td className="cell-pre">{(k.scopes || []).join(', ') || '—'}</td>
                  <td>{k.active ? 'active' : 'revoked'}</td>
                  <td className="nowrap">{fmtDateTime(k.last_used_at)}</td>
                  <td>{k.active && <button className="btn-link" onClick={() => revoke(k.id)}>Revoke</button>}</td>
                </tr>
              ))}
              {(data?.items || []).length === 0 && <tr><td colSpan={6} className="empty-state">No API keys</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      <NewKeyModal open={open} onClose={() => setOpen(false)}
        onDone={(key) => { setOpen(false); setCreated(key); run(); }} />
      <CreatedKeyModal keyData={created} onClose={() => setCreated(null)} />
    </div>
  );
}

function NewKeyModal({ open, onClose, onDone }) {
  const [name, setName] = useState('');
  const [scopes, setScopes] = useState(['pob.view']);
  const [busy, setBusy] = useState(false);
  const toggle = (s) => setScopes((p) => (p.includes(s) ? p.filter((x) => x !== s) : [...p, s]));
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const d = await api('/api/v1/apikeys', { method: 'POST', body: { name, scopes } });
      toast('API key created', 'success'); onDone(d);
    } catch (err) { toast(err.message, 'error'); }
    setBusy(false);
  };
  return (
    <Modal open={open} onClose={onClose} title="Create API key" footer={
      <><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="key-form" disabled={busy}>Create</button></>}>
      <form id="key-form" onSubmit={submit}>
        <Field label="Name"><TextInput value={name} onChange={(e) => setName(e.target.value)} required /></Field>
        <Field label="Permissions">
          <div className="check-grid">
            {ALL_SCOPES.map((s) => (
              <label key={s} className="check">
                <input type="checkbox" checked={scopes.includes(s)} onChange={() => toggle(s)} />
                <code>{s}</code>
              </label>
            ))}
          </div>
        </Field>
      </form>
    </Modal>
  );
}

function CreatedKeyModal({ keyData, onClose }) {
  if (!keyData) return null;
  return (
    <Modal open title="Copy your API key now" onClose={onClose}>
      <p className="muted">This is the only time the full key is shown. Store it securely — it will not be displayed again.</p>
      <div className="qr-uri"><code>{keyData.key}</code></div>
      <button className="btn btn-primary btn-block" onClick={() => {
        navigator.clipboard?.writeText(keyData.key); toast('Copied', 'success'); onClose();
      }}>Copy & close</button>
    </Modal>
  );
}

// ── Webhooks ────────────────────────────────────────────────────────────────

const EVENTS = ['pob.submitted', 'pob.approved', 'pob.rejected', 'pob.duplicate',
  'verification.approved', 'verification.rejected', 'gratification.created',
  'cashback.paid', 'gift.ready', 'gift.delivered', 'payout.batch.paid'];

function WebhooksTab() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/webhooks'));
  const [open, setOpen] = useState(false);
  const [deliveries, setDeliveries] = useState(null);
  const [created, setCreated] = useState(null);

  const remove = async (id) => {
    if (!window.confirm('Delete this webhook? Its delivery history is removed too.')) return;
    try { await api(`/api/v1/webhooks/${id}`, { method: 'DELETE' }); toast('Webhook deleted', 'success'); run(); }
    catch (err) { toast(err.message, 'error'); }
  };
  const showDeliveries = async (id) => {
    try { setDeliveries({ id, items: (await api(`/api/v1/webhooks/${id}/deliveries`)).items }); }
    catch (err) { toast(err.message, 'error'); }
  };

  if (loading) return <Spinner />;
  if (error) return <ErrorBox error={error} onRetry={run} />;
  return (
    <div>
      <div className="card">
        <div className="card-head">
          <h3 className="sub-head">Webhook endpoints</h3>
          <button className="btn btn-primary" onClick={() => setOpen(true)}>New webhook</button>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Name</th><th>URL</th><th>Events</th><th>Secret</th><th>Last delivery</th><th></th></tr></thead>
            <tbody>
              {(data?.items || []).map((w) => (
                <tr key={w.id}>
                  <td>{w.name}</td>
                  <td className="nowrap"><code>{w.url}</code></td>
                  <td className="cell-pre">{(w.events || []).join(', ')}</td>
                  <td>{w.has_secret ? '✓' : '—'}</td>
                  <td className="nowrap">{w.last_delivery_at ? `${w.last_delivery_status} · ${fmtDateTime(w.last_delivery_at)}` : 'never'}</td>
                  <td className="nowrap">
                    <button className="btn-link" onClick={() => showDeliveries(w.id)}>Deliveries</button>
                    {' '}
                    <button className="btn-link" onClick={() => remove(w.id)}>Delete</button>
                  </td>
                </tr>
              ))}
              {(data?.items || []).length === 0 && <tr><td colSpan={6} className="empty-state">No webhooks</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
      <NewWebhookModal open={open} onClose={() => setOpen(false)}
        onDone={(d) => { setOpen(false); setCreated(d); run(); }} />
      <CreatedWebhookModal data={created} onClose={() => setCreated(null)} />
      <DeliveriesModal data={deliveries} onClose={() => setDeliveries(null)} />
    </div>
  );
}

function NewWebhookModal({ open, onClose, onDone }) {
  const [f, setF] = useState({ name: '', url: '', events: ['pob.submitted'], secret: '' });
  const [busy, setBusy] = useState(false);
  const toggle = (e) => setF((p) => {
    const evs = p.events.includes(e) ? p.events.filter((x) => x !== e) : [...p.events, e];
    return { ...p, events: evs };
  });
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const d = await api('/api/v1/webhooks', { method: 'POST', body: f });
      toast('Webhook created', 'success'); onDone(d);
    } catch (err) { toast(err.message, 'error'); }
    setBusy(false);
  };
  return (
    <Modal open={open} onClose={onClose} title="New webhook" wide footer={
      <><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="hook-form" disabled={busy}>Create</button></>}>
      <form id="hook-form" onSubmit={submit}>
        <Field label="Name"><TextInput value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required /></Field>
        <Field label="Endpoint URL"><TextInput value={f.url} onChange={(e) => setF({ ...f, url: e.target.value })} placeholder="https://example.com/hooks/pob" required /></Field>
        <Field label="Signing secret" hint="Leave blank to auto-generate. Sent as X-POB-Signature.">
          <TextInput value={f.secret} onChange={(e) => setF({ ...f, secret: e.target.value })} />
        </Field>
        <Field label="Events">
          <div className="check-grid">
            {EVENTS.map((e) => (
              <label key={e} className="check">
                <input type="checkbox" checked={f.events.includes(e)} onChange={() => toggle(e)} />
                <code>{e}</code>
              </label>
            ))}
          </div>
        </Field>
      </form>
    </Modal>
  );
}

function CreatedWebhookModal({ data, onClose }) {
  if (!data) return null;
  return (
    <Modal open title="Webhook created — store this secret" onClose={onClose}>
      <p className="muted">The signing secret is shown only once. Use it to verify X-POB-Signature headers.</p>
      <div className="qr-uri"><code>{data.secret}</code></div>
      <button className="btn btn-primary btn-block" onClick={() => {
        navigator.clipboard?.writeText(data.secret); toast('Copied', 'success'); onClose();
      }}>Copy & close</button>
    </Modal>
  );
}

function DeliveriesModal({ data, onClose }) {
  if (!data) return null;
  return (
    <Modal open wide title={`Deliveries #${data.id}`} onClose={onClose}>
      <div className="table-wrap">
        <table className="data-table">
          <thead><tr><th>Event</th><th>Status</th><th>Attempts</th><th>HTTP</th><th>At</th><th>Error</th></tr></thead>
          <tbody>
            {(data.items || []).map((d) => (
              <tr key={d.id}>
                <td><code>{d.event}</code></td>
                <td>{d.status}</td>
                <td>{d.attempts}</td>
                <td>{d.http_status ?? '—'}</td>
                <td className="nowrap">{fmtDateTime(d.delivered_at || d.created_at)}</td>
                <td className="cell-pre">{d.error || d.response || '—'}</td>
              </tr>
            ))}
            {(data.items || []).length === 0 && <tr><td colSpan={6} className="empty-state">No deliveries</td></tr>}
          </tbody>
        </table>
      </div>
    </Modal>
  );
}
