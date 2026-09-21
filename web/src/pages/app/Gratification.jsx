import { useState } from 'react';
import { api, fmtDateTime, fmtMoney, getSession } from '../../api';
import QrScanner from '../../QrScanner';
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
  const typesQ = useAsync(() => api('/api/v1/gratification/types'));

  const types = typesQ.data?.items || [];

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
            {types.filter((t) => t.active !== false).map((t) => (
              <option key={t.code} value={t.code}>{t.name}</option>
            ))}
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
  const [upiOpen, setUpiOpen] = useState(false);
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
              {perms.has('gratification.approve') && ['cashback', 'upi', 'reward_points'].includes(g.type_code) && g.status === 'eligible' && (
                <button className="btn btn-primary" onClick={() => setModal('approve')}>Approve cashback</button>
              )}
              {perms.has('gratification.pay') && ['cashback', 'upi', 'reward_points'].includes(g.type_code) && g.status === 'approved' && (
                <button className="btn btn-primary" onClick={() => setModal('pay')}>Mark paid</button>
              )}
              {perms.has('gratification.manage') && ['voucher', 'e_voucher'].includes(g.type_code) && g.status === 'eligible' && (
                <button className="btn btn-primary" onClick={() => setModal('generate')}>Generate voucher</button>
              )}
              {perms.has('gratification.manage') && ['voucher', 'e_voucher'].includes(g.type_code) && g.status === 'generated' && (
                <button className="btn btn-primary" onClick={() => setModal('send')}>Send voucher</button>
              )}
              {perms.has('gratification.manage') && ['voucher', 'e_voucher'].includes(g.type_code) && g.status === 'sent' && (
                <button className="btn btn-primary" onClick={() => setModal('redeem')}>Redeem voucher</button>
              )}
            </div>

            {['cashback', 'upi'].includes(g.type_code) && (
              <div className="card" style={{ marginTop: 12 }}>
                <h4 style={{ marginBottom: 6 }}>Chemist UPI (for payout)</h4>
                <p className="muted" style={{ marginBottom: 8 }}>
                  Verified UPI: <strong>{g.chemist_upi_id || 'None saved yet'}</strong>
                  {g.chemist_upi_id && (
                    <> — <strong>{g.chemist_upi_confirmed ? 'Confirmed' : 'Not confirmed'}</strong></>
                  )}
                </p>
                {perms.has('chemist.manage') && (
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <button className="btn" title="Payout will use this confirmed address"
                      disabled={!g.chemist_upi_id || !g.chemist_upi_confirmed}
                      onClick={() => toast('Use verified UPI: payout will run against this confirmed address', 'success')}>
                      Use verified UPI
                    </button>
                    <button className="btn btn-primary" onClick={() => setUpiOpen(true)}>
                      Scan new UPI QR
                    </button>
                  </div>
                )}
                {!perms.has('chemist.manage') && (
                  <p className="muted" style={{ fontSize: 12 }}>Only field / admin staff can update the chemist UPI.</p>
                )}
              </div>
            )}

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
      {upiOpen && (
        <Modal open title="Chemist UPI — scan & confirm" onClose={() => setUpiOpen(false)}>
          <GratUpiScan chemistId={g.chemist_id} chemistName={g.chemist_name}
            existing={g.chemist_upi_id}
            onDone={() => { setUpiOpen(false); run(); }} />
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

/**
 * GratUpiScan
 *
 * In-flow UPI capture for a cashback/upi gratification: scan the QR with the
 * camera (paste fallback), decode, name-match against the chemist, and save a
 * confirmed UPI against the chemist. Replacing an existing UPI is the explicit
 * confirmation step. The approve step then carries this confirmed address to
 * payout.
 */
function GratUpiScan({ chemistId, chemistName, existing, onDone }) {
  const [payload, setPayload] = useState('');
  const [decoded, setDecoded] = useState(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [scanning, setScanning] = useState(false);

  const decodePayload = async () => {
    if (!payload.trim()) { toast('Scan or paste the UPI QR payload', 'error'); return; }
    setBusy(true);
    try {
      const r = await api(`/api/v1/chemists/${chemistId}/upi/decode`, { method: 'POST', body: { payload } });
      setDecoded(r.details);
      if (r.details.valid) toast('QR decoded — confirm the payee name', 'success');
      else toast(r.details.error || 'Not a valid UPI payload', 'error');
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const onScanned = (text) => { setScanning(false); setPayload(text); setDecoded(null); decodePayload(); };

  const confirm = async () => {
    setSaving(true);
    try {
      const d = decoded;
      await api(`/api/v1/chemists/${chemistId}/upi`, {
        method: 'POST',
        body: {
          upi_id: d.upi_id, source: 'qr', raw_payload: payload,
          payee_name: d.payee_name, name_score: d.name_score, confirmed: true,
        },
      });
      toast('Chemist UPI saved & confirmed', 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); } finally { setSaving(false); }
  };

  return (
    <form onSubmit={(e) => { e.preventDefault(); decodePayload(); }}>
      <p className="muted" style={{ marginBottom: 10 }}>
        <strong>{chemistName}</strong>
        {existing ? ` — existing UPI: ${existing}` : ' — no saved UPI yet'}
      </p>
      {scanning ? (
        <div className="card" style={{ marginBottom: 12 }}>
          <QrScanner onScan={onScanned} onError={() => {}} onClose={() => setScanning(false)} />
        </div>
      ) : (
        <button type="button" className="btn" style={{ marginBottom: 12 }} onClick={() => setScanning(true)}>
          📷 Scan with camera
        </button>
      )}
      <Field label="Or paste the UPI payload / VPA" hint="Manual entry fallback">
        <TextInput value={payload} onChange={(e) => setPayload(e.target.value)}
          placeholder="upi://pay?pa=chemist@bank&pn=... or vpa@bank" />
      </Field>
      {!decoded && (
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? 'Decoding…' : 'Decode'}
        </button>
      )}
      {decoded && (
        <div style={{ marginTop: 12 }}>
          <div className="kv-grid">
            <span>VPA <strong>{decoded.masked_upi_id}</strong></span>
            <span>Payee <strong>{decoded.payee_name || '—'}</strong></span>
            <span>Name match {decoded.name_score != null ? <strong>{Math.round(decoded.name_score * 100)}%</strong> : <strong>—</strong>}</span>
          </div>
          {decoded.valid ? (
            <div style={{ marginTop: 10, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button type="button" className="btn btn-primary" onClick={confirm} disabled={saving}>
                {saving ? 'Saving…' : (existing ? 'Confirm & replace existing UPI' : 'Confirm & save UPI')}
              </button>
              <button type="button" className="btn" onClick={() => setDecoded(null)}>Scan again</button>
            </div>
          ) : (
            <p className="muted" style={{ marginTop: 8 }}>{decoded.error}</p>
          )}
        </div>
      )}
    </form>
  );
}
