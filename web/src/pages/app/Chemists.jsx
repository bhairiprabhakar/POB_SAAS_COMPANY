import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, downloadFile, fmtMoney, fmtDate, uploadFile, getSession } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, SearchBox, Select, Spinner,
  StatusBadge, TextArea, TextInput, toast, useAsync,
} from '../../ui';

const BLANK = {
  name: '', shop_name: '', owner_name: '', mobile: '', alternate_mobile: '', email: '',
  gst: '', dl_number: '', upi_id: '', ocid: '', doctor_name: '', category: '', area: '',
  address: '', city: '', district: '', state: '', pin: '', latitude: '', longitude: '',
  attachment_type: '', potential_category: '', institution_name: '', institution_type: '',
  institution_department: '', institution_contact_person: '', institution_address: '',
  monthly_business_potential: '', estimated_monthly_sales: '', brand_potential: '',
  strategic_importance: '', last_visit_date: '', visit_frequency: '',
  status: 'active',
};

const REG_FIELDS = [
  { name: 'name', label: 'Chemist name', required: true },
  { name: 'shop_name', label: 'Shop name' },
  { name: 'owner_name', label: 'Owner name' },
  { name: 'mobile', label: 'Mobile' },
  { name: 'alternate_mobile', label: 'Alt mobile' },
  { name: 'email', label: 'Email', type: 'email' },
  { name: 'gst', label: 'GST' },
  { name: 'dl_number', label: 'DL number' },
  { name: 'upi_id', label: 'UPI ID' },
  { name: 'ocid', label: 'OCID' },
  { name: 'doctor_name', label: 'Doctor' },
  { name: 'category', label: 'Category' },
  { name: 'area', label: 'Area' },
];

export default function Chemists() {
  const navigate = useNavigate();
  const session = getSession();
  const canManage = (session?.permissions || []).includes('chemist.manage');
  const [q, setQ] = useState('');
  const [status, setStatus] = useState('');
  const [campaignId, setCampaignId] = useState('');
  const [editing, setEditing] = useState(null);
  const [viewing, setViewing] = useState(null);
  const [bulk, setBulk] = useState(false);
  const [actionFor, setActionFor] = useState(null);

  const campaigns = useAsync(() => api('/api/v1/campaigns?status=active&active=1'));

  const buildUrl = useCallback(() => {
    let url = '/api/v1/chemists?limit=500';
    if (q) url += `&q=${encodeURIComponent(q)}`;
    if (status) url += `&status=${status}`;
    if (campaignId) url += `&campaign_id=${campaignId}`;
    return url;
  }, [q, status, campaignId]);

  const { data, loading, error, run } = useAsync(() => api(buildUrl()), [buildUrl]);
  const rows = data?.items || [];

  return (
    <div>
      <PageHeader title="Chemists"
        subtitle={campaignId
          ? 'Select a chemist to submit POB or upload invoice proof'
          : 'Register and manage the retail chemist network'}
        actions={
          <>
            {canManage && <button className="btn" onClick={() => downloadFile('/api/v1/chemists/bulk-template', 'chemists_template.xlsx')}>
              Template
            </button>}
            {canManage && <button className="btn" onClick={() => setBulk(true)}>Bulk Upload</button>}
            {canManage && <button className="btn btn-primary" onClick={() => navigate('/app/chemists/register')}>
              Register Chemist
            </button>}
          </>
        } />

      <div className="toolbar">
        <SearchBox value={q} onChange={setQ} placeholder="Search name / shop / mobile / GST..." />
        <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
        </select>
        <select className="input" value={campaignId} onChange={(e) => setCampaignId(e.target.value)}>
          <option value="">All campaigns</option>
          {(campaigns.data?.items || []).map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
      </div>

      {loading && <Spinner label="Loading chemists..." />}
      {error && <ErrorBox error={error} onRetry={run} />}
      {!loading && !error && (
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr>
              <th>#</th>
              <th>Name</th>
              <th>Shop</th>
              <th>Area / City</th>
              <th>Mobile</th>
              {campaignId && <>
                <th>POB Status</th>
                <th>Invoice</th>
                <th>Gratification</th>
              </>}
              <th>Registered by</th>
              <th>Hierarchy</th>
              <th>Status</th>
              <th>Actions</th>
            </tr></thead>
            <tbody>
              {rows.map((r) => (
                <ChemistRow key={r.id} row={r} campaignId={campaignId} campaigns={campaigns.data?.items || []}
                  canManage={canManage}
                  onView={() => setViewing(r.id)}
                  onAction={(action) => setActionFor({ chemist: r, action })}
                  onEdit={() => setEditing({ ...r })} />
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={campaignId ? 11 : 8} className="empty-state">No chemists found</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {actionFor && (
        <InlineAction key={`${actionFor.chemist.id}-${actionFor.action}`}
          chemist={actionFor.chemist} campaignId={campaignId} campaigns={campaigns.data?.items || []}
          action={actionFor.action}
          onClose={() => setActionFor(null)}
          onDone={() => { setActionFor(null); run(); }} />
      )}

      {editing && (
        <RegisterModal key={editing.id ?? 'new'} row={editing} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); run(); }} />
      )}

      {viewing && <DetailModal chemistId={viewing} onClose={() => setViewing(null)} />}

      {bulk && <BulkModal onClose={() => setBulk(false)} onDone={() => { setBulk(false); run(); }} />}
    </div>
  );
}

/* ── Table row ─────────────────────────────────────────────────────────── */

function ChemistRow({ row, campaignId, campaigns, canManage, onView, onAction, onEdit }) {
  const r = row;
  const pobStatus = r.pob_status;
  const invStatus = r.invoice_status;
  const vState = r.verification_state;
  const gratCount = r.grat_count || 0;
  const gratValue = r.grat_total_value || 0;
  const gratStatus = r.grat_latest_status;

  // POB status rendering
  let pobCell;
  if (!campaignId) {
    pobCell = <span className="muted">Select campaign</span>;
  } else if (!pobStatus) {
    pobCell = <span className="muted">--</span>;
  } else {
    pobCell = <StatusBadge value={pobStatus} />;
  }

  // Invoice status rendering
  let invCell = null;
  if (campaignId) {
    if (invStatus === 'uploaded') {
      invCell = <Badge tone="blue">Uploaded</Badge>;
    } else if (pobStatus && !invStatus) {
      invCell = <Badge tone="amber">Pending</Badge>;
    } else {
      invCell = <span className="muted">--</span>;
    }
  } else {
    invCell = <span className="muted">--</span>;
  }

  // Gratification rendering
  let gratCell = null;
  if (campaignId) {
    if (gratCount > 0) {
      gratCell = (
        <span>
          <Badge tone="green">{gratCount} gift{gratCount > 1 ? 's' : ''}</Badge>
          {gratValue > 0 && <span className="muted" style={{ marginLeft: 4 }}>{fmtMoney(gratValue)}</span>}
        </span>
      );
    } else {
      gratCell = <span className="muted">--</span>;
    }
  } else {
    gratCell = <span className="muted">--</span>;
  }

  // Hierarchy chain
  const chain = r.registered_by_hierarchy;
  const regName = r.registered_by_name;
  const regLevel = r.registered_by_level;

  // Determine which actions are available
  const canSubmitPob = !pobStatus || pobStatus === 'submitted' || pobStatus === 'rejected';
  const canUploadInvoice = pobStatus && invStatus !== 'uploaded' && vState !== 'auto_verified' && vState !== 'approved' && vState !== 'verified';

  return (
    <tr>
      <td><strong>#{r.id}</strong></td>
      <td>
        <div><strong>{r.name}</strong></div>
        {r.owner_name && <small className="muted">{r.owner_name}</small>}
      </td>
      <td>{r.shop_name || '--'}</td>
      <td>{r.area || ''}{r.area && r.city ? ', ' : ''}{r.city || ''}</td>
      <td className="nowrap">{r.mobile || '--'}</td>

      {campaignId && <td>{pobCell}</td>}
      {campaignId && <td>{invCell}</td>}
      {campaignId && <td>{gratCell}</td>}

      <td>
        {regName ? (
          <span>
            {regName}
            {regLevel && <small className="muted"> ({regLevel})</small>}
          </span>
        ) : <span className="muted">--</span>}
      </td>
      <td>
        {chain && chain.length > 1 ? (
          <span className="hierarchy-chain" title={chain.map((h) => `${h.name} (${h.level || h.role})`).join(' > ')}>
            {chain.slice(1).map((h) => h.level || h.role).join(' > ')}
          </span>
        ) : <span className="muted">--</span>}
      </td>
      <td><StatusBadge value={r.status} /></td>
      <td className="nowrap" style={{ whiteSpace: 'nowrap' }}>
        <button className="btn btn-sm" onClick={onView} title={`Full profile for ${r.name}`}>
          Details
        </button>
        <button className="btn btn-sm" onClick={() => onAction('upi')} title={`Scan UPI QR for ${r.name}`}>
          Scan UPI
        </button>
        {canManage ? (
          <>
            {canSubmitPob && (
              <button className="btn btn-sm btn-primary" onClick={() => onAction('pob')}
                title={campaignId ? `Submit POB for ${r.name}` : 'Select a campaign first'}>
                Submit POB
              </button>
            )}
            {canUploadInvoice && (
              <button className="btn btn-sm" onClick={() => onAction('invoice')}
                title={campaignId ? `Upload invoice for ${r.name}` : 'Select a campaign first'}>
                Upload Invoice
              </button>
            )}
            {!canSubmitPob && !canUploadInvoice && (
              <span className="muted" style={{ fontSize: 12 }}>No action needed</span>
            )}
          </>
        ) : (
          <button className="btn-link" onClick={onEdit}>Edit</button>
        )}
      </td>
    </tr>
  );
}

function del(r, run) {
  if (!window.confirm(`Delete ${r.name}? This fails if the chemist already has POB activity.`)) return;
  api(`/api/v1/chemists/${r.id}`, { method: 'DELETE' })
    .then(() => { toast('Chemist deleted', 'success'); run(); })
    .catch((err) => toast(err.message, 'error'));
}

/* ── Full chemist profile (read-only) ───────────────────────────────────── */

function DetailModal({ chemistId, onClose }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/chemists/${chemistId}`), [chemistId]);

  return (
    <Modal open wide title="Chemist profile" onClose={onClose} footer={
      <button className="btn" onClick={onClose}>Close</button>
    }>
      {loading && <Spinner label="Loading profile…" />}
      {error && <ErrorBox error={error} onRetry={run} />}
      {!loading && !error && data && <ChemistProfile data={data} />}
    </Modal>
  );
}

function ChemistProfile({ data }) {
  const d = data;
  const act = d.activity || {};
  const grat = d.gratification || {};
  const visits = d.visits || {};
  const chain = d.registered_by_hierarchy || [];
  const location = [d.address, d.city, d.district, d.state, d.pin].filter(Boolean).join(', ');

  return (
    <div>
      <div className="detail-hero" data-tone={d.status === 'active' ? 'green' : 'gray'}>
        <div className="detail-hero-top">
          <div className="detail-hero-title">
            <h2>{d.name}</h2>
            <p>{[d.shop_name, d.area].filter(Boolean).join(' — ') || d.owner_name || ''}</p>
            <div className="detail-hero-badges">
              <StatusBadge value={d.status} />
              {d.attachment_type && <Badge tone="blue">{d.attachment_type}</Badge>}
              {d.potential_category && <Badge tone="amber">{d.potential_category}</Badge>}
              {d.category && <Badge tone="teal">{d.category}</Badge>}
            </div>
          </div>
          <div className="detail-hero-amount">
            <span className="stat-label">Verified POB value</span>
            <span className="detail-hero-value">{fmtMoney(act.verified_value)}</span>
            <span className="stat-sub">{act.verified_pobs || 0} verified of {act.pobs || 0} POBs</span>
          </div>
        </div>
        <div className="detail-hero-facts">
          {d.mobile && <div className="fact-pill"><span>Mobile</span><strong>{d.mobile}</strong></div>}
          {d.owner_name && <div className="fact-pill"><span>Owner</span><strong>{d.owner_name}</strong></div>}
          {d.visit_frequency && <div className="fact-pill"><span>Visit</span><strong>{d.visit_frequency}</strong></div>}
          {visits.count > 0 && <div className="fact-pill"><span>Visits</span><strong>{visits.count}{visits.last_visit ? ` · ${fmtDate(visits.last_visit)}` : ''}</strong></div>}
          {d.upi_id && <div className="fact-pill"><span>UPI</span><strong>{d.upi_id}</strong></div>}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10, marginBottom: 16 }}>
        <div className="card" style={{ padding: 12 }}>
          <div className="stat-label">Pending</div>
          <strong style={{ fontSize: 18 }}>{act.pending_pobs || 0}</strong>
        </div>
        <div className="card" style={{ padding: 12 }}>
          <div className="stat-label">Invoiced</div>
          <strong style={{ fontSize: 18 }}>{act.invoiced_pobs || 0}</strong>
        </div>
        <div className="card" style={{ padding: 12 }}>
          <div className="stat-label">Gratifications</div>
          <strong style={{ fontSize: 18 }}>{grat.count || 0}</strong>
          <span className="muted" style={{ fontSize: 12 }}> {fmtMoney(grat.value)}</span>
        </div>
        <div className="card" style={{ padding: 12 }}>
          <div className="stat-label">Paid out</div>
          <strong style={{ fontSize: 18 }}>{fmtMoney(grat.paid_value)}</strong>
        </div>
      </div>

      <h4 className="section-title">Identity &amp; contact</h4>
      <div className="detail-grid">
        {[['Name', d.name], ['Shop name', d.shop_name], ['Owner', d.owner_name],
          ['Mobile', d.mobile], ['Alt mobile', d.alternate_mobile], ['Email', d.email],
          ['GST', d.gst], ['DL number', d.dl_number], ['UPI', d.upi_id],
          ['OCID', d.ocid], ['Doctor', d.doctor_name], ['Category', d.category],
          ['Area', d.area]].map(([k, v]) => (
          <div className="detail-item" key={k}><span className="detail-label">{k}</span><span className="detail-value">{v || '—'}</span></div>
        ))}
      </div>

      <h4 className="section-title">Address &amp; location</h4>
      <div className="detail-grid">
        <div className="detail-item" style={{ gridColumn: '1 / -1' }}><span className="detail-label">Address</span><span className="detail-value">{location || '—'}</span></div>
        {[['City', d.city], ['District', d.district], ['State', d.state], ['PIN', d.pin],
          ['Latitude', d.latitude], ['Longitude', d.longitude]].map(([k, v]) => (
          <div className="detail-item" key={k}><span className="detail-label">{k}</span><span className="detail-value">{v || '—'}</span></div>
        ))}
      </div>

      <h4 className="section-title">Classification &amp; potential</h4>
      <div className="detail-grid">
        {[['Attachment type', d.attachment_type], ['Potential category', d.potential_category],
          ['Institution', d.institution_name], ['Institution type', d.institution_type],
          ['Department', d.institution_department], ['Contact person', d.institution_contact_person],
          ['Monthly potential', d.monthly_business_potential ? fmtMoney(d.monthly_business_potential) : null],
          ['Est. monthly sales', d.estimated_monthly_sales ? fmtMoney(d.estimated_monthly_sales) : null],
          ['Brand potential', d.brand_potential], ['Strategic importance', d.strategic_importance],
          ['Visit frequency', d.visit_frequency], ['Last visit', d.last_visit_date ? fmtDate(d.last_visit_date) : null],
          ].map(([k, v]) => (
          <div className="detail-item" key={k}><span className="detail-label">{k}</span><span className="detail-value">{v || '—'}</span></div>
        ))}
        {d.institution_address && (
          <div className="detail-item" style={{ gridColumn: '1 / -1' }}><span className="detail-label">Institution address</span><span className="detail-value">{d.institution_address}</span></div>
        )}
      </div>

      {d.registered_by_name && (
        <>
          <h4 className="section-title">Registrant</h4>
          <div className="detail-grid">
            <div className="detail-item"><span className="detail-label">Registered by</span><span className="detail-value">{d.registered_by_name}{d.registered_by_level ? ` (${d.registered_by_level})` : ''}</span></div>
            {chain.length > 1 && (
              <div className="detail-item" style={{ gridColumn: '1 / -1' }}><span className="detail-label">Hierarchy</span>
                <span className="detail-value">{chain.map((h) => h.name).join(' › ')}</span>
              </div>
            )}
            {d.created_at && (
              <div className="detail-item"><span className="detail-label">Onboarded</span><span className="detail-value">{fmtDate(d.created_at)}</span></div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/* ── Inline POB / Invoice action modals ────────────────────────────────── */

function InlineAction({ chemist, campaignId, campaigns, action, onClose, onDone }) {
  return (
    <Modal open wide title={action === 'pob' ? 'Submit POB' : action === 'invoice' ? 'Upload Invoice Proof' : `Scan UPI — ${chemist.name}`} onClose={onClose}>
      {action === 'pob'
        ? <InlinePOBForm chemist={chemist} campaignId={campaignId} campaigns={campaigns} onDone={onDone} />
        : action === 'invoice'
          ? <InlineInvoiceForm chemist={chemist} campaignId={campaignId} campaigns={campaigns} onDone={onDone} />
          : <UpiScanForm chemist={chemist} onDone={onDone} />}
    </Modal>
  );
}

function UpiScanForm({ chemist, onDone }) {
  const [payload, setPayload] = useState('');
  const [decoded, setDecoded] = useState(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const cid = chemist.id;

  const decode = async (e) => {
    e.preventDefault();
    if (!payload.trim()) { toast('Paste the scanned UPI QR payload or the VPA', 'error'); return; }
    setBusy(true);
    try {
      const r = await api(`/api/v1/chemists/${cid}/upi/decode`, { method: 'POST', body: { payload } });
      setDecoded(r.details);
      if (r.details.valid) toast('QR decoded — verify the payee name', 'success');
      else toast(r.details.error || 'Not a valid UPI payload', 'error');
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const confirm = async () => {
    setSaving(true);
    try {
      const d = decoded;
      await api(`/api/v1/chemists/${cid}/upi`, {
        method: 'POST',
        body: {
          upi_id: d.upi_id, source: 'qr', raw_payload: payload,
          payee_name: d.payee_name, name_score: d.name_score, confirmed: true,
        },
      });
      toast('UPI address saved & confirmed', 'success');
      setDecoded(null); setPayload('');
      onDone();
    } catch (err) { toast(err.message, 'error'); } finally { setSaving(false); }
  };

  return (
    <form onSubmit={decode}>
      <p style={{ marginBottom: 12 }}>
        <strong>{chemist.name}</strong>{chemist.shop_name ? ` — ${chemist.shop_name}` : ''}
      </p>
      <Field label="Scanned UPI payload / VPA" required
        hint="Paste the UPI QR text (e.g. upi://pay?pa=shop@upi&pn=Shop Name) or a bare VPA">
        <TextArea rows={3} value={payload} onChange={(e) => setPayload(e.target.value)}
          placeholder="upi://pay?pa=chemist@bank&pn=Chemist Name&am=100.00" />
      </Field>
      <button className="btn btn-primary" type="submit" disabled={busy || saving}>
        {busy ? 'Decoding…' : 'Decode QR / VPA'}
      </button>

      {decoded && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="kv-grid">
            <span>VPA <strong>{decoded.masked_upi_id}</strong></span>
            <span>Payee <strong>{decoded.payee_name || '—'}</strong></span>
            <span>Valid <strong>{decoded.valid ? 'Yes' : 'No'}</strong></span>
            <span>Name match {decoded.name_score != null ? <strong>{Math.round(decoded.name_score * 100)}%</strong> : <strong>—</strong>}</span>
          </div>
          {decoded.valid ? (
            <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <button type="button" className="btn btn-primary" onClick={confirm} disabled={saving}>
                {saving ? 'Saving…' : (decoded.name_score ?? 0) < 0.6 ? 'Save anyway' : 'Confirm & save'}
              </button>
              {(decoded.name_score ?? 0) < 0.6 && (
                <span className="muted" style={{ fontSize: 12 }}>Payee name doesn't strongly match — only save if you verified it with the shop.</span>
              )}
            </div>
          ) : (
            <p className="muted" style={{ marginTop: 10 }}>{decoded.error}</p>
          )}
        </div>
      )}
    </form>
  );
}

function InlinePOBForm({ chemist, campaignId, campaigns, onDone }) {
  const [selectedCampaignId, setSelectedCampaignId] = useState(campaignId || '');
  const [products, setProducts] = useState([]);
  const [qty, setQty] = useState({});
  const [remarks, setRemarks] = useState('');
  const [busy, setBusy] = useState(false);
  const [campaignInfo, setCampaignInfo] = useState(null);

  useEffect(() => {
    if (!selectedCampaignId) { setProducts([]); setCampaignInfo(null); return; }
    api(`/api/v1/products?campaign_id=${selectedCampaignId}`).then((d) => {
      const items = (d.items || []).filter((p) => p.status !== 'inactive');
      setProducts(items);
      setQty(Object.fromEntries(items.map((p) => [p.id, 1])));
    }).catch(() => {});
    api(`/api/v1/campaigns/${selectedCampaignId}`).then(setCampaignInfo).catch(() => {});
  }, [selectedCampaignId]);

  const line = (p) => {
    const q = Number(qty[p.id]) || 0;
    const minQ = p.min_quantity || 0;
    const minPob = p.min_pob || 0;
    const maxPob = p.max_pob;
    const amount = Math.round(q * (Number(p.ptr) || 0) * 100) / 100;
    return {
      ...p, q, amount, minQ, minPob, maxPob,
      lowQty: q > 0 && minQ > 0 && q < minQ,
      lowPob: q > 0 && minPob > 0 && amount < minPob,
      highPob: q > 0 && maxPob != null && amount > maxPob,
    };
  };
  const lines = products.map(line);
  const total = lines.reduce((s, l) => s + l.amount, 0);

  const submit = async (e) => {
    e.preventDefault();
    if (!selectedCampaignId) { toast('Select a campaign', 'error'); return; }
    const items = lines.filter((l) => l.q > 0).map((l) => ({ product_id: l.id, quantity: l.q }));
    if (items.length === 0) { toast('Enter quantity for at least one product', 'error'); return; }
    const below = lines.filter((l) => l.q > 0 && (l.lowQty || l.lowPob || l.highPob));
    if (below.length > 0) {
      const reasons = below.map((l) => {
        const msgs = [];
        if (l.lowQty) msgs.push(`min qty ${l.minQ}`);
        if (l.lowPob) msgs.push(`min POB ${fmtMoney(l.minPob)}`);
        if (l.highPob) msgs.push(`max POB ${fmtMoney(l.maxPob)}`);
        return `${l.name}: ${msgs.join(', ')}`;
      });
      toast(`Cannot submit — ${reasons.join('; ')}`, 'error');
      return;
    }
    setBusy(true);
    try {
      await api('/api/v1/pob/visit', {
        method: 'POST',
        body: { campaign_id: Number(selectedCampaignId), chemist_id: Number(chemist.id), items, remarks },
      });
      toast(`POB submitted for ${chemist.name}`, 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  return (
    <form onSubmit={submit}>
      <p style={{ marginBottom: 12 }}>
        <strong>{chemist.name}</strong>{chemist.shop_name ? ` — ${chemist.shop_name}` : ''}
      </p>
      {!campaignId && (
        <Field label="Campaign" required>
          <select className="input" value={selectedCampaignId} onChange={(e) => setSelectedCampaignId(e.target.value)}>
            <option value="">-- Select campaign --</option>
            {(campaigns || []).map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        </Field>
      )}
      {selectedCampaignId && products.length === 0 && <Spinner label="Loading products..." />}
      {selectedCampaignId && products.length > 0 && (
        <>
          {campaignInfo && <p className="muted" style={{ marginBottom: 8 }}>{campaignInfo.name}</p>}
          {campaignInfo && (campaignInfo.start_date || campaignInfo.end_date || campaignInfo.terms_conditions || products.some((p) => p.min_pob > 0)) && (
            <div className="campaign-criteria-card">
              <div className="criteria-header">
                <span className="criteria-icon">📋</span>
                <h4>Campaign Requirements</h4>
              </div>
              <div className="criteria-grid">
                {(campaignInfo.start_date || campaignInfo.end_date) && (
                  <div className="criteria-item">
                    <span className="criteria-label">Period</span>
                    <span className="criteria-value">{fmtDate(campaignInfo.start_date)} → {fmtDate(campaignInfo.end_date)}</span>
                  </div>
                )}
                {campaignInfo.grace_days > 0 && (
                  <div className="criteria-item">
                    <span className="criteria-label">Grace Period</span>
                    <span className="criteria-value">{campaignInfo.grace_days}d{campaignInfo.grace_months ? ` + ${campaignInfo.grace_months}mo` : ''}</span>
                  </div>
                )}
                {products.filter((p) => p.min_pob > 0 || (p.min_quantity && p.min_quantity > 1)).length > 0 && (
                  <div className="criteria-item criteria-wide">
                    <span className="criteria-label">Product Minimums</span>
                    <div className="criteria-table-mini">
                      {products.filter((p) => p.min_pob > 0 || (p.min_quantity && p.min_quantity > 1)).map((p) => (
                        <div key={p.id} className="criteria-table-row">
                          <span>{p.name}</span>
                          <span>
                            {p.min_quantity > 1 && <>Min qty: <strong>{p.min_quantity}</strong>{' · '}</>}
                            {p.min_pob > 0 && <>Min POB: <strong>{fmtMoney(p.min_pob)}</strong></>}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {campaignInfo.terms_conditions && (
                  <div className="criteria-item criteria-wide">
                    <span className="criteria-label">Terms &amp; Conditions</span>
                    <span className="criteria-value criteria-terms">{campaignInfo.terms_conditions}</span>
                  </div>
                )}
              </div>
              <p className="criteria-note">POB will be rejected if these criteria are not met.</p>
            </div>
          )}
          <p className="pob-form-hint">Enter quantity for each product. POB value = Qty x PTR. Minimums shown per product.</p>
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr>
                <th>Product</th><th>PTR</th><th>Min Qty</th><th>Min POB</th>
                <th style={{ width: 90 }}>Quantity</th><th>POB Value</th>
              </tr></thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.id} className={l.q > 0 && (l.lowQty || l.lowPob || l.highPob) ? 'row-warning' : ''}>
                    <td><strong>{l.name}</strong></td>
                    <td>{fmtMoney(l.ptr)}</td>
                    <td>{l.minQ > 0 ? l.minQ : '--'}</td>
                    <td>{l.minPob > 0 ? fmtMoney(l.minPob) : '--'}</td>
                    <td>
                      <input className="input input-sm" type="number" min="0"
                        value={qty[l.id] || ''}
                        onChange={(e) => setQty((p) => ({ ...p, [l.id]: e.target.value }))}
                        style={{ width: 80 }} />
                    </td>
                    <td>
                      <strong>{fmtMoney(l.amount)}</strong>
                      {l.lowPob && <div className="warning-text">Min POB is {fmtMoney(l.minPob)}</div>}
                      {l.highPob && <div className="warning-text">Max POB is {fmtMoney(l.maxPob)}</div>}
                      {l.lowQty && !l.lowPob && <div className="warning-text">Min qty is {l.minQ}</div>}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot><tr><td colSpan={5} style={{ textAlign: 'right' }}><strong>Total</strong></td><td><strong>{fmtMoney(total)}</strong></td></tr></tfoot>
            </table>
          </div>
          <Field label="Remarks" style={{ marginTop: 12 }}>
            <TextArea value={remarks} onChange={(e) => setRemarks(e.target.value)} rows={2} placeholder="Optional notes" />
          </Field>
        </>
      )}
      <div style={{ marginTop: 12, textAlign: 'right' }}>
        <button className="btn btn-primary" type="submit" disabled={busy || !selectedCampaignId}>
          {busy ? 'Submitting...' : 'Submit POB'}
        </button>
      </div>
    </form>
  );
}

function InlineInvoiceForm({ chemist, campaignId, campaigns, onDone }) {
  const [selectedCampaignId, setSelectedCampaignId] = useState(campaignId || '');
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef(null);

  const setFileFrom = (f) => {
    if (!f) return;
    if (!/^(application\/pdf|image\/)/.test(f.type)) {
      toast('Choose a PDF or image of the invoice', 'error');
      return;
    }
    setFile(f);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    setFileFrom(e.dataTransfer?.files?.[0]);
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!selectedCampaignId) { toast('Select a campaign', 'error'); return; }
    if (!file) { toast('Choose an invoice file', 'error'); return; }
    setBusy(true);
    try {
      await uploadFile('/api/v1/pob/invoice-proof', file, {
        campaign_id: selectedCampaignId,
        chemist_id: chemist.id,
      });
      toast(`Invoice uploaded for ${chemist.name}`, 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  return (
    <form onSubmit={submit}>
      <p style={{ marginBottom: 12 }}>
        <strong>{chemist.name}</strong>{chemist.shop_name ? ` — ${chemist.shop_name}` : ''}
      </p>
      {!campaignId && (
        <Field label="Campaign" required>
          <select className="input" value={selectedCampaignId} onChange={(e) => setSelectedCampaignId(e.target.value)}>
            <option value="">-- Select campaign --</option>
            {(campaigns || []).map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        </Field>
      )}
      <div
        className={`pob-invoice-upload-area${dragging ? ' dragging' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        style={{ cursor: 'pointer', padding: 32, border: '2px dashed #d1d5db', borderRadius: 8, textAlign: 'center', marginBottom: 12 }}>
        <input ref={inputRef} type="file" accept="application/pdf,image/*" hidden
          onChange={(e) => setFileFrom(e.target.files?.[0])} />
        {file ? (
          <div>
            <strong>{file.name}</strong>
            <div className="muted">{(file.size / 1024).toFixed(0)} KB</div>
          </div>
        ) : (
          <div className="muted">
            Drag & drop invoice here, or click to browse
            <div style={{ marginTop: 4 }}>PDF or image</div>
          </div>
        )}
      </div>
      <p className="field-hint" style={{ marginBottom: 12 }}>
        Invoice number and date are auto-extracted by AI. After upload, the POB goes to pending verification.
      </p>
      <div style={{ textAlign: 'right' }}>
        <button className="btn btn-primary" type="submit" disabled={busy || !file || !selectedCampaignId}>
          {busy ? 'Uploading...' : 'Upload Invoice Proof'}
        </button>
      </div>
    </form>
  );
}

/* ── Register / Edit modal ─────────────────────────────────────────────── */

function RegisterModal({ row, onClose, onSaved }) {
  const isEdit = !!row.id;
  const [f, setF] = useState({ ...BLANK, ...row });
  const [busy, setBusy] = useState(false);
  const [posts, setPosts] = useState(null);
  const [lookup, setLookup] = useState('idle');
  const [looking, setLooking] = useState(null);
  const [dups, setDups] = useState(null);
  const [masters, setMasters] = useState({ attachment_types: [], potential_categories: [] });
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));

  useEffect(() => {
    api('/api/v1/chemist-masters').then(setMasters).catch(() => {});
  }, []);

  const lookupPin = async (pin) => {
    const value = (pin ?? f.pin ?? '').trim();
    if (!/^\d{6}$/.test(value)) { toast('Enter a valid 6-digit PIN code', 'error'); return; }
    setLookup('loading');
    try {
      const r = await api(`/api/v1/pincode/${value}`);
      setPosts(r.items || []);
      setLooking(value);
      setLookup(r.items.length ? 'done' : 'error');
      if (r.items.length >= 1) {
        const po = r.items[0];
        setF((p) => ({ ...p, city: po.city, district: po.district, state: po.state }));
      }
    } catch (err) {
      setPosts(null); setLookup('error');
      toast(err.message, 'error');
    }
  };

  const detectLocation = () => {
    if (!navigator.geolocation) { toast('Geolocation not supported', 'error'); return; }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setF((p) => ({
          ...p,
          latitude: pos.coords.latitude.toFixed(6),
          longitude: pos.coords.longitude.toFixed(6),
        }));
        toast('Location captured', 'success');
      },
      () => toast('Could not detect location', 'error'),
      { enableHighAccuracy: true, timeout: 10000 },
    );
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!f.name.trim()) { toast('Name is required', 'error'); return; }
    setBusy(true);
    try {
      if (!isEdit) {
        const r = await api('/api/v1/chemists', {
          method: 'POST',
          body: { ...f, latitude: f.latitude === '' ? null : f.latitude, longitude: f.longitude === '' ? null : f.longitude },
        });
        if (r?.ok === false) { setDups(r.duplicates || []); return; }
      } else {
        await api(`/api/v1/chemists/${row.id}`, {
          method: 'PUT',
          body: { ...f, latitude: f.latitude === '' ? null : f.latitude, longitude: f.longitude === '' ? null : f.longitude },
        });
      }
      toast(isEdit ? 'Chemist updated' : 'Chemist registered', 'success');
      onSaved();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const forceRegister = async () => {
    setBusy(true);
    try {
      await api('/api/v1/chemists', {
        method: 'POST',
        body: { ...f, duplicate_checks: [], latitude: f.latitude === '' ? null : f.latitude, longitude: f.longitude === '' ? null : f.longitude },
      });
      toast('Chemist registered', 'success');
      onSaved();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  return (
    <Modal open wide title={isEdit ? `Edit chemist #${row.id}` : 'Register chemist'} onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" form="chemist-form" disabled={busy}>
            {busy ? 'Saving...' : isEdit ? 'Save changes' : 'Register'}
          </button>
        </>
      }>
      <form id="chemist-form" onSubmit={submit}>
        <h4 className="section-title">Details</h4>
        <div className="grid-2">
          {REG_FIELDS.map((fld) => (
            <Field key={fld.name} label={fld.label} required={fld.required}>
              <TextInput type={fld.type || 'text'} value={f[fld.name] || ''} onChange={set(fld.name)} required={fld.required} />
            </Field>
          ))}
        </div>

        <h4 className="section-title" style={{ marginTop: 18 }}>Address & location</h4>
        <div className="grid-2">
          <Field label="Address" className="span-2">
            <TextInput value={f.address || ''} onChange={set('address')} />
          </Field>
          <Field label="PIN code" hint="6-digit PIN auto-fetches city, district & state">
            <TextInput value={f.pin || ''} maxLength={6} placeholder="e.g. 500001"
              onChange={set('pin')}
              onBlur={(e) => { const v = e.target.value.trim(); if (/^\d{6}$/.test(v) && v !== looking) lookupPin(v); }} />
          </Field>
          {posts && posts.length > 1 && (
            <Field label="Location (post office)">
              <Select value={f.city || ''} onChange={(e) => {
                const po = posts.find((p) => p.city === e.target.value);
                setF((p) => ({ ...p, city: e.target.value, district: po?.district || p.district, state: po?.state || p.state }));
              }} options={posts.map((p) => ({ value: p.city, label: `${p.city} — ${p.district}` }))} />
            </Field>
          )}
          <Field label="City"><TextInput value={f.city || ''} onChange={set('city')} /></Field>
          <Field label="District"><TextInput value={f.district || ''} onChange={set('district')} /></Field>
          <Field label="State"><TextInput value={f.state || ''} onChange={set('state')} /></Field>
          <Field label="Area"><TextInput value={f.area || ''} onChange={set('area')} /></Field>
          <Field label="Latitude"><TextInput type="number" step="any" value={f.latitude || ''} onChange={set('latitude')} /></Field>
          <Field label="Longitude"><TextInput type="number" step="any" value={f.longitude || ''} onChange={set('longitude')} /></Field>
        </div>
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          <button type="button" className="btn" onClick={() => lookupPin()} disabled={lookup === 'loading'}>
            {lookup === 'loading' ? 'Fetching...' : 'Fetch from PIN'}
          </button>
          <button type="button" className="btn" onClick={detectLocation}>Use my location</button>
        </div>

        <h4 className="section-title" style={{ marginTop: 18 }}>Classification &amp; potential</h4>
        <div className="grid-2">
          <Field label="Attachment type" hint="Hospital, retail, chain, online pharmacy…">
            <Select value={f.attachment_type || ''} onChange={set('attachment_type')}
              options={(masters.attachment_types || []).map((m) => ({ value: m.code, label: `${m.name} (${m.code})` }))} />
          </Field>
          <Field label="Potential category">
            <Select value={f.potential_category || ''} onChange={set('potential_category')}
              options={(masters.potential_categories || []).map((m) => ({ value: m.code, label: `${m.name} (${m.code})` }))} />
          </Field>
          <Field label="Institution name"><TextInput value={f.institution_name || ''} onChange={set('institution_name')} /></Field>
          <Field label="Institution type"><TextInput value={f.institution_type || ''} onChange={set('institution_type')} /></Field>
          <Field label="Department"><TextInput value={f.institution_department || ''} onChange={set('institution_department')} /></Field>
          <Field label="Contact person"><TextInput value={f.institution_contact_person || ''} onChange={set('institution_contact_person')} /></Field>
          <Field label="Institution address" className="span-2"><TextInput value={f.institution_address || ''} onChange={set('institution_address')} /></Field>
          <Field label="Monthly business potential (INR)">
            <TextInput type="number" min="0" value={f.monthly_business_potential || ''} onChange={set('monthly_business_potential')} />
          </Field>
          <Field label="Estimated monthly sales (INR)">
            <TextInput type="number" min="0" value={f.estimated_monthly_sales || ''} onChange={set('estimated_monthly_sales')} />
          </Field>
          <Field label="Brand potential"><TextInput value={f.brand_potential || ''} onChange={set('brand_potential')} /></Field>
          <Field label="Strategic importance"><TextInput value={f.strategic_importance || ''} onChange={set('strategic_importance')} /></Field>
          <Field label="Visit frequency">
            <Select value={f.visit_frequency || ''} onChange={set('visit_frequency')} placeholder="— select —"
              options={['daily', 'weekly', 'monthly', 'quarterly'].map((o) => ({ value: o, label: o }))} />
          </Field>
          <Field label="Last visit date"><TextInput type="date" value={f.last_visit_date || ''} onChange={set('last_visit_date')} /></Field>
        </div>

        <h4 className="section-title" style={{ marginTop: 18 }}>Status</h4>
        <div className="grid-2">
          <Field label="Status">
            <Select value={f.status} onChange={set('status')}
              options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} />
          </Field>
        </div>
      </form>

      {dups && (
        <Modal open title="A chemist like this already exists" onClose={() => setDups(null)}
          footer={
            <>
              <button className="btn" onClick={() => setDups(null)}>Use existing</button>
              <button className="btn btn-primary" onClick={forceRegister} disabled={busy}>
                {busy ? 'Registering…' : 'Register anyway (new shop)'}
              </button>
            </>
          }>
          <p className="muted">Existing chemist(s) matching your details were found. Re-check to avoid registering the same shop twice.</p>
          <div className="card" style={{ marginTop: 12 }}>
            {dups.map((d, i) => (
              <div key={i} style={{ padding: '8px 0', borderBottom: i < dups.length - 1 ? '1px solid var(--border)' : 'none' }}>
                <strong>{d.chemist.name}</strong>{d.chemist.shop_name ? ` — ${d.chemist.shop_name}` : ''}
                {d.chemist.city ? `, ${d.chemist.city}` : ''}
                <div className="muted" style={{ fontSize: 12 }}>
                  Matched on {d.rule.replace('_', ' ')}{d.chemist.mobile ? ` · ☎ ${d.chemist.mobile}` : ''} · #{d.chemist.id}
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </Modal>
  );
}

/* ── Bulk upload modal ─────────────────────────────────────────────────── */

function BulkModal({ onClose, onDone }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const upload = async (e) => {
    e.preventDefault();
    if (!file) { toast('Choose an Excel file first', 'error'); return; }
    setBusy(true);
    try {
      const r = await uploadFile('/api/v1/chemists/bulk-upload', file);
      setResult(r);
      if (!r.errors?.length) {
        toast(`${r.created} chemist(s) imported`, 'success');
        onDone();
      } else {
        toast(`${r.created} imported, ${r.errors.length} failed`, 'error');
      }
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };
  return (
    <Modal open title="Bulk register chemists" onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>Close</button>
          <button className="btn btn-primary" form="bulk-form" disabled={busy || !file}>
            {busy ? 'Uploading...' : 'Upload'}
          </button>
        </>
      }>
      <form id="bulk-form" onSubmit={upload}>
        <p className="muted">
          Use the template with one chemist per row. Columns: name (required), shop_name, gst, dl_number,
          owner_name, mobile, alternate_mobile, email, address, city, district, state, pin, latitude,
          longitude, ocid, doctor_name, category, area, upi_id, status.
        </p>
        <button type="button" className="btn-link" onClick={() => downloadFile('/api/v1/chemists/bulk-template', 'chemists_template.xlsx')}>
          Download template
        </button>
        <Field label="Excel file (.xlsx)">
          <TextInput type="file" accept=".xlsx" onChange={(e) => setFile(e.target.files?.[0] || null)} />
        </Field>
      </form>
      {result && (
        <div className="card" style={{ marginTop: 12 }}>
          <p><strong>{result.created}</strong> chemist(s) imported, <strong>{result.errors?.length || 0}</strong> error(s).</p>
          {result.errors?.length > 0 && (
            <ul className="err-list">{result.errors.map((m, i) => <li key={i}>{m}</li>)}</ul>
          )}
        </div>
      )}
    </Modal>
  );
}
