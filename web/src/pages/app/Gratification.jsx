import { useMemo, useState } from 'react';
import { api, fmtDateTime, fmtMoney, getSession } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, ProofPane, Select, Spinner, SplitDetail,
  StatusBadge, Table, TextInput, toast, useAsync, useFileUrl,
} from '../../ui';

export default function Gratification() {
  const [type, setType] = useState('');
  const [status, setStatus] = useState('');
  const [selected, setSelected] = useState(null);
  const params = new URLSearchParams();
  if (type) params.set('type_code', type);
  if (status) params.set('status', status);
  const qs = params.toString();
  const { data, loading, error, run } = useAsync(() =>
    api(`/api/v1/gratification${qs ? `?${qs}` : ''}`), [qs]);

  const types = useMemo(() => new Set((data?.items || []).map((g) => g.type_code)), [data]);

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'type_code', label: 'Type', render: (r) => <Badge tone="blue">{r.type_code}</Badge> },
    { key: 'mr_name', label: 'MR' },
    { key: 'campaign_name', label: 'Campaign' },
    { key: 'gift_name', label: 'Gift', render: (r) => r.gift_name || '—' },
    { key: 'scheme_value', label: 'Value', render: (r) => fmtMoney(r.scheme_value) },
    { key: 'voucher_code', label: 'Voucher', render: (r) => r.voucher_code ? <code>{r.voucher_code}</code> : '—' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: 'created_at', label: 'Created', render: (r) => fmtDateTime(r.created_at) },
  ];

  if (loading) return <Spinner label="Loading gratifications…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      <PageHeader title="Gratification" subtitle="Rewards lifecycle: physical gifts, cashback/UPI and vouchers">
        <div className="inline-filters">
          <select className="input" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">All types</option>
            {[...types].map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All statuses</option>
            {['eligible', 'dispatched', 'delivered', 'completed', 'approved', 'paid', 'generated', 'sent', 'redeemed'].map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
      </PageHeader>
      <Table cols={cols} rows={data?.items || []} keyOf={(r) => r.id}
        onRowClick={(r) => setSelected(r.id)} empty="No gratifications yet — they are created when a POB is approved" />
      {selected && <GratificationDetail gid={selected} onClose={() => setSelected(null)} onDone={() => { setSelected(null); run(); }} />}
    </div>
  );
}

function GratificationDetail({ gid, onClose, onDone }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/gratification/${gid}`));
  const gifts = useAsync(() => api('/api/v1/gifts'));
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState(null);
  const photoUrl = useFileUrl(data?.photo_path);
  const perms = new Set(getSession()?.permissions || []);

  const act = async (fn, msg) => {
    setBusy(true);
    try { await fn(); toast(msg, 'success'); setModal(null); onDone(); }
    catch (e) { toast(e.message, 'error'); } finally { setBusy(false); }
  };

  if (loading) return <Modal open title={`Gratification #${gid}`} onClose={onClose}><Spinner /></Modal>;
  if (error) return <Modal open title={`Gratification #${gid}`} onClose={onClose}><ErrorBox error={error} onRetry={run} /></Modal>;
  const g = data;

  return (
    <Modal open wide xwide title={`Gratification #${g.id}`} onClose={onClose}>
      <SplitDetail
        left={
          <ProofPane url={photoUrl} name="Delivery photo" hint={g.photo_path ? 'Captured at delivery' : ''}
            empty="No delivery photo recorded yet" />
        }
        right={
          <>
            <div className="kv-grid">
              <span>Type <Badge tone="blue">{g.type_code}</Badge></span>
              <span>Status <StatusBadge value={g.status} /></span>
              <span>MR <strong>{g.mr_name}</strong></span>
              <span>Campaign <strong>{g.campaign_name}</strong></span>
              <span>Chemist <strong>{g.chemist_name}{g.shop_name ? ` (${g.shop_name})` : ''}</strong></span>
              <span>Value <strong>{fmtMoney(g.scheme_value)}</strong></span>
              <span>Gift <strong>{g.gift_name || '—'}</strong></span>
              <span>UPI <strong>{g.upi_id || '—'}</strong></span>
              <span>Voucher <strong>{g.voucher_code ? <code>{g.voucher_code}</code> : '—'}</strong></span>
              <span>Payment ref <strong>{g.payment_ref || '—'}</strong></span>
              <span>Dispatch <strong>{g.dispatch_status || '—'}</strong></span>
              <span>Delivery <strong>{g.delivery_status || '—'}</strong></span>
              <span>GPS <strong>{g.gps_lat ? `${g.gps_lat}, ${g.gps_lng}` : '—'}</strong></span>
              <span>Ack <strong>{g.acknowledgement || '—'}</strong></span>
            </div>

            <div className="card-actions">
              {perms.has('gratification.dispatch') && g.type_code === 'physical_gift' && g.status === 'eligible' && (
                <button className="btn btn-primary" onClick={() => setModal('dispatch')}>Dispatch gift</button>
              )}
              {perms.has('gratification.dispatch') && g.type_code === 'physical_gift' && ['dispatched', 'delivered'].includes(g.status) && (
                <button className="btn btn-primary" onClick={() => setModal('deliver')}>Mark delivered (GPS + photo)</button>
              )}
              {perms.has('gratification.dispatch') && g.type_code === 'physical_gift' && g.status === 'delivered' && (
                <button className="btn" onClick={() => setModal('ack')}>Acknowledge &amp; complete</button>
              )}
              {perms.has('gratification.approve') && ['cashback', 'upi'].includes(g.type_code) && g.status === 'eligible' && (
                <button className="btn btn-primary" onClick={() => setModal('approve')}>Approve cashback</button>
              )}
              {perms.has('gratification.pay') && ['cashback', 'upi'].includes(g.type_code) && g.status === 'approved' && (
                <button className="btn btn-primary" onClick={() => setModal('pay')}>Mark paid</button>
              )}
              {perms.has('gratification.manage') && g.type_code === 'voucher' && g.status === 'eligible' && (
                <button className="btn btn-primary" onClick={() => setModal('generate')}>Generate voucher</button>
              )}
              {perms.has('gratification.manage') && g.type_code === 'voucher' && g.status === 'generated' && (
                <button className="btn btn-primary" onClick={() => setModal('send')}>Send voucher</button>
              )}
              {perms.has('gratification.manage') && g.type_code === 'voucher' && g.status === 'sent' && (
                <button className="btn btn-primary" onClick={() => setModal('redeem')}>Redeem voucher</button>
              )}
            </div>

            {g.events?.length > 0 && (
              <div className="timeline">
                <h4>Events</h4>
                {g.events.map((e) => (
                  <div key={e.id} className="tl-item">
                    <span className="tl-dot" />
                    <div>
                      <strong>{e.event}</strong> <span className="muted">{fmtDateTime(e.created_at)}</span>
                      {e.detail && <div className="muted">{e.detail}</div>}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </>
        }
      />

      {modal === 'dispatch' && (
        <Modal open title="Dispatch gift" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="dispatch-form" disabled={busy}>Dispatch</button></>}>
          <DispatchForm gid={gid} gifts={gifts.data?.items || []}
            onSubmit={(body) => act(() => api(`/api/v1/gratification/${gid}/dispatch`, { method: 'POST', body }), 'Gift dispatched')} />
        </Modal>
      )}
      {modal === 'deliver' && (
        <Modal open title="Mark delivered" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="deliver-form" disabled={busy}>Save delivery</button></>}>
          <DeliverForm gid={gid} onSubmit={(fd) => act(() => api(`/api/v1/gratification/${gid}/delivered`, { method: 'POST', body: fd }), 'Gift delivered')} />
        </Modal>
      )}
      {modal === 'ack' && (
        <Modal open title="Acknowledge" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="ack-form" disabled={busy}>Complete</button></>}>
          <SimpleForm id="ack-form" fields={{ acknowledgement: { label: 'Acknowledgement', required: true } }}
            onSubmit={(body) => act(() => api(`/api/v1/gratification/${gid}/acknowledge`, { method: 'POST', body }), 'Gratification completed')} />
        </Modal>
      )}
      {modal === 'approve' && (
        <Modal open title="Approve cashback" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="cb-approve-form" disabled={busy}>Approve</button></>}>
          <SimpleForm id="cb-approve-form" fields={{
            upi_id: { label: 'UPI ID' }, note: { label: 'Note' },
          }} onSubmit={(body) => act(() => api(`/api/v1/gratification/${gid}/approve`, { method: 'POST', body }), 'Cashback approved')} />
        </Modal>
      )}
      {modal === 'pay' && (
        <Modal open title="Mark paid" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="cb-pay-form" disabled={busy}>Mark paid</button></>}>
          <SimpleForm id="cb-pay-form" fields={{ payment_ref: { label: 'Payment reference' } }}
            onSubmit={(body) => act(() => api(`/api/v1/gratification/${gid}/pay`, { method: 'POST', body }), 'Payment recorded')} />
        </Modal>
      )}
      {modal === 'generate' && (
        <Modal open title="Generate voucher" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="vgen-form" disabled={busy}>Generate</button></>}>
          <SimpleForm id="vgen-form" fields={{ code: { label: 'Voucher code (blank = auto)' } }}
            onSubmit={(body) => act(() => api(`/api/v1/gratification/${gid}/generate-voucher`, { method: 'POST', body }), 'Voucher generated')} />
        </Modal>
      )}
      {modal === 'send' && (
        <Modal open title="Send voucher" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="vsend-form" disabled={busy}>Send</button></>}>
          <SimpleForm id="vsend-form" fields={{ to: { label: 'Send to (channel / address)' } }}
            onSubmit={(body) => act(() => api(`/api/v1/gratification/${gid}/send-voucher`, { method: 'POST', body }), 'Voucher sent')} />
        </Modal>
      )}
      {modal === 'redeem' && (
        <Modal open title="Redeem voucher" onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button>
            <button className="btn btn-primary" form="vred-form" disabled={busy}>Redeem</button></>}>
          <SimpleForm id="vred-form" fields={{ redeemed_by: { label: 'Redeemed by' } }}
            onSubmit={(body) => act(() => api(`/api/v1/gratification/${gid}/redeem-voucher`, { method: 'POST', body }), 'Voucher redeemed')} />
        </Modal>
      )}
    </Modal>
  );
}

function DispatchForm({ gid, gifts, onSubmit }) {
  const [giftId, setGiftId] = useState('');
  const [tracking, setTracking] = useState('');
  return (
    <form id="dispatch-form" onSubmit={(e) => { e.preventDefault(); onSubmit({ gift_id: giftId, tracking }); }}>
      <Field label="Gift" required>
        <Select value={giftId} onChange={(e) => setGiftId(e.target.value)} required
          options={gifts.map((gf) => ({ value: gf.id, label: `${gf.name} (stock ${gf.stock})` }))} />
      </Field>
      <Field label="Tracking / courier"><TextInput value={tracking} onChange={(e) => setTracking(e.target.value)} /></Field>
    </form>
  );
}

function DeliverForm({ gid, onSubmit }) {
  const [lat, setLat] = useState('');
  const [lng, setLng] = useState('');
  const [file, setFile] = useState(null);
  return (
    <form id="deliver-form" onSubmit={(e) => {
      e.preventDefault();
      const fd = new FormData();
      if (lat) fd.append('latitude', lat);
      if (lng) fd.append('longitude', lng);
      if (file) fd.append('photo', file);
      onSubmit(fd);
    }}>
      <div className="grid-2">
        <Field label="Latitude"><TextInput value={lat} onChange={(e) => setLat(e.target.value)} /></Field>
        <Field label="Longitude"><TextInput value={lng} onChange={(e) => setLng(e.target.value)} /></Field>
      </div>
      <Field label="Delivery photo"><input type="file" accept="image/*" onChange={(e) => setFile(e.target.files[0])} /></Field>
    </form>
  );
}

function SimpleForm({ id, fields, onSubmit }) {
  const [vals, setVals] = useState({});
  return (
    <form id={id} onSubmit={(e) => {
      e.preventDefault();
      const body = {};
      Object.entries(fields).forEach(([k, f]) => { if (vals[k]) body[k] = vals[k]; });
      onSubmit(body);
    }}>
      {Object.entries(fields).map(([k, f]) => (
        <Field key={k} label={f.label} required={f.required}>
          <TextInput required={f.required} value={vals[k] || ''}
            onChange={(e) => setVals((p) => ({ ...p, [k]: e.target.value }))} />
        </Field>
      ))}
    </form>
  );
}
