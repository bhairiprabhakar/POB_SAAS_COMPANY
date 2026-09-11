import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, fmtDate, fmtMoney } from '../../api';
import {
  ErrorBox, Field, PageHeader, Select, Spinner, TextArea, TextInput, toast, useAsync,
} from '../../ui';

export default function SubmitPob({ demo }) {
  const nav = useNavigate();
  const campaigns = useAsync(() => api('/api/v1/campaigns?status=active&active=1'));
  const chemists = useAsync(() => api('/api/v1/chemists?status=active'));
  const [campaignId, setCampaignId] = useState('');
  const [campaignPobRequired, setCampaignPobRequired] = useState(true);
  const [chemistId, setChemistId] = useState('');
  const [products, setProducts] = useState([]);
  const [qty, setQty] = useState({});
  const [remarks, setRemarks] = useState('');
  const [busy, setBusy] = useState(false);
  const [invoiceFile, setInvoiceFile] = useState(null);
  const [previewUrl, setPreviewUrl] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [campaignData, setCampaignData] = useState(null);
  const [elig, setElig] = useState(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!campaignId || !chemistId) { setElig(null); return; }
    api(`/api/v1/campaigns/${campaignId}/eligible-chemist?chemist_id=${chemistId}`)
      .then((r) => setElig(r.eligibility || null))
      .catch(() => setElig(null));
  }, [campaignId, chemistId]);

  useEffect(() => {
    if (!invoiceFile) { setPreviewUrl(null); return; }
    const url = URL.createObjectURL(invoiceFile);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [invoiceFile]);

  useEffect(() => {
    if (!campaignId) { setProducts([]); setCampaignPobRequired(true); setCampaignData(null); return; }
    api(`/api/v1/products?campaign_id=${campaignId}`).then((d) => {
      const items = (d.items || []).filter((p) => p.status !== 'inactive');
      setProducts(items);
      setQty(Object.fromEntries(items.map((p) => [p.id, 1])));
    }).catch(() => setProducts([]));
    api(`/api/v1/campaigns/${campaignId}`).then((c) => {
      setCampaignPobRequired(c.pob_required !== false);
      setCampaignData(c);
    }).catch(() => { setCampaignPobRequired(true); setCampaignData(null); });
  }, [campaignId]);

  const setQ = (pid) => (e) => setQty((prev) => ({ ...prev, [pid]: e.target.value }));

  const line = (p) => {
    const q = Number(qty[p.id]) || 0;
    const minQ = p.min_quantity || 0;
    const minPob = p.min_pob || 0;
    const amount = Math.round(q * (Number(p.ptr) || 0) * 100) / 100;
    return { ...p, q, amount, minQ, minPob, lowQty: q > 0 && minQ > 0 && q < minQ, lowPob: q > 0 && minPob > 0 && amount < minPob };
  };
  const lines = products.map(line);
  const total = lines.reduce((s, l) => s + l.amount, 0);

  const chemRows = chemists.data?.items || [];
  const chemBase = (c) => `${c.name || ''}${c.shop_name ? ` — ${c.shop_name}` : ''}${c.city ? `, ${c.city}` : ''}${c.state ? `, ${c.state}` : ''}`;
  const chemLabelCount = chemRows.reduce((m, c) => ((m[chemBase(c)] = (m[chemBase(c)] || 0) + 1), m), {});
  const chemistOptions = [...chemRows]
    .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
    .map((c) => {
      const base = chemBase(c);
      return {
        value: c.id,
        label: chemLabelCount[base] > 1
          ? `${base} · ☎ ${c.mobile || `#${c.id}`} · #${c.id}`
          : base,
      };
    });

  const setFileFrom = (f) => {
    if (!f) return;
    if (!/^(application\/pdf|image\/)/.test(f.type)) {
      toast('Please choose a PDF or an image of the invoice', 'error');
      return;
    }
    setInvoiceFile(f);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    setFileFrom(e.dataTransfer?.files?.[0]);
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!campaignId || !chemistId) { toast('Select campaign and chemist', 'error'); return; }
    if (elig && !elig.eligible) {
      toast('This chemist is not in the campaign\'s eligible segment — select an eligible chemist.', 'error');
      return;
    }

    if (!campaignPobRequired) {
      if (!invoiceFile) { toast('Choose an invoice file', 'error'); return; }
      setBusy(true);
      try {
        if (demo) {
          await new Promise((r) => setTimeout(r, 400));
          toast('Test mode: Invoice submitted. Nothing saved.', 'success');
          return;
        }
        const fd = new FormData();
        fd.append('campaign_id', campaignId);
        fd.append('chemist_id', chemistId);
        fd.append('invoice', invoiceFile);
        const data = await api('/api/v1/pob/invoice-submit', { method: 'POST', body: fd });
        toast(data.message || 'Invoice submitted', 'success');
        nav('/app/pob/mine');
      } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
      return;
    }

    const items = lines.filter((l) => l.q > 0).map((l) => ({ product_id: l.id, quantity: l.q }));
    if (items.length === 0) { toast('Enter quantity for at least one brand', 'error'); return; }
    const below = lines.filter((l) => l.q > 0 && (l.lowQty || l.lowPob));
    if (below.length > 0) {
      const reasons = below.map((l) => {
        if (l.lowQty && l.lowPob) return `${l.name}: min qty ${l.minQ}, min POB ${fmtMoney(l.minPob)}`;
        if (l.lowQty) return `${l.name}: quantity below minimum (${l.minQ})`;
        return `${l.name}: POB below minimum (${fmtMoney(l.minPob)})`;
      });
      toast(`Cannot submit — ${reasons.join('; ')}`, 'error');
      return;
    }
    setBusy(true);
    try {
      if (demo) {
        await new Promise((r) => setTimeout(r, 400));
        toast(`Test mode: POB for ${items.length} brand(s), value ${fmtMoney(total)}. Nothing saved.`, 'success');
        setQty(Object.fromEntries(products.map((p) => [p.id, 1])));
        setRemarks('');
        return;
      }
      const r = await api('/api/v1/pob/visit', { method: 'POST', body: { campaign_id: Number(campaignId), chemist_id: Number(chemistId), items, remarks } });
      toast(`POB submitted (${items.length} brand${items.length > 1 ? 's' : ''}, ${fmtMoney(r.total_amount)}). Add the invoice proof when ready.`, 'success');
      nav('/app/pob/mine');
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  if (campaigns.loading || chemists.loading) return <Spinner label="Loading campaign data…" />;
  if (campaigns.error) return <ErrorBox error={campaigns.error} onRetry={campaigns.run} />;

  const isInvoiceOnly = campaignId && !campaignPobRequired;
  const isStandardMode = campaignId && campaignPobRequired;

  return (
    <div className="pob-submit-page">
      {demo && (
        <div className="scope-banner" data-tone="amber">
          <span className="scope-dot" />
          <div>
            <strong>Test mode — how your team submits a POB</strong>
            <small>Live preview with real campaigns, chemists &amp; brands. Nothing you submit is saved.</small>
          </div>
        </div>
      )}

      <PageHeader
        title={isInvoiceOnly ? 'Submit Invoice' : 'Submit POB'}
        subtitle={isInvoiceOnly
          ? 'Upload the invoice — product, quantity and amount will be auto-extracted by AI.'
          : 'Select a campaign and chemist, enter brand-wise quantities — the POB value is calculated automatically.'}
        actions={!demo && !isInvoiceOnly && (
          <button className="btn" onClick={() => nav('/app/pob/invoice')}>Attach invoice proof →</button>
        )} />

      {/* Step 1: Campaign & Chemist */}
      <form className="card form-card" onSubmit={submit}>
        <div className="pob-form-section">
          <h4 className="pob-form-section-title">
            <span className="pob-form-step">1</span>
            Select Campaign &amp; Chemist
          </h4>
          <div className="grid-2">
            <Field label="POB Campaign" required hint="Only active campaigns are listed">
              <Select value={campaignId} onChange={(e) => setCampaignId(e.target.value)} required
                options={(campaigns.data?.items || []).map((c) => ({ value: c.id, label: `${c.name}${c.brand_name ? ` — ${c.brand_name}` : ''}` }))} />
            </Field>
            <Field label="Registered Chemist" required hint="Same-name chemists shown with mobile & ID">
              <Select value={chemistId} onChange={(e) => setChemistId(e.target.value)} required
                options={chemistOptions} />
            </Field>
          </div>
</div>

          {elig && elig.restricted && (
            elig.eligible ? (
              <div className="elig-banner" data-tone="green" style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, backgroundColor: 'rgba(16,185,129,.12)', border: '1px solid rgba(16,185,129,.35)' }}>
                <strong>✓ This chemist matches the campaign's eligible segment.</strong>
                <div className="muted" style={{ fontSize: 12 }}>
                  {elig.matches?.attachment_type ? 'Attachment type: in segment.' : `Attachment type "${elig.chemist.attachment_type || 'n/a'}" is outside the target.`}{' '}
                  {elig.matches?.potential_category ? 'Potential category: in segment.' : `Potential category "${elig.chemist.potential_category || 'n/a'}" is outside the target.`}
                </div>
              </div>
            ) : (
              <div className="elig-banner" data-tone="red" style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, backgroundColor: 'rgba(220,38,38,.12)', border: '1px solid rgba(220,38,38,.4)' }}>
                <strong>⛔ This chemist is not eligible for the selected campaign.</strong>
                <div className="muted" style={{ fontSize: 12 }}>{elig.reason}</div>
              </div>
            )
          )}

          {campaignData && campaignId && (
          <div className="campaign-criteria-card">
            <div className="criteria-header">
              <span className="criteria-icon">📋</span>
              <h4>Campaign Requirements</h4>
            </div>
            <div className="criteria-grid">
              {(campaignData.start_date || campaignData.end_date) && (
                <div className="criteria-item">
                  <span className="criteria-label">Campaign Period</span>
                  <span className="criteria-value">
                    {fmtDate(campaignData.start_date)} → {fmtDate(campaignData.end_date)}
                  </span>
                </div>
              )}
              {campaignData.grace_days > 0 && (
                <div className="criteria-item">
                  <span className="criteria-label">Invoice Grace Period</span>
                  <span className="criteria-value">
                    {campaignData.grace_days} days{campaignData.grace_months ? ` + ${campaignData.grace_months} month(s)` : ''}
                  </span>
                </div>
              )}
              {campaignData.pre_grace_days > 0 && (
                <div className="criteria-item">
                  <span className="criteria-label">Pre-Launch Window</span>
                  <span className="criteria-value">{campaignData.pre_grace_days} days before start</span>
                </div>
              )}
              {products.length > 0 && products.some((p) => p.min_pob > 0 || p.min_quantity > 1) && (
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
              {campaignData.terms_conditions && (
                <div className="criteria-item criteria-wide">
                  <span className="criteria-label">Terms &amp; Conditions</span>
                  <span className="criteria-value criteria-terms">{campaignData.terms_conditions}</span>
                </div>
              )}
            </div>
            <p className="criteria-note">POB will be rejected if these criteria are not met.</p>
          </div>
        )}
        {isInvoiceOnly && (
          <div className="pob-form-section pob-invoice-section">
            <h4 className="pob-form-section-title">
              <span className="pob-form-step">2</span>
              Upload Invoice
            </h4>
            <div className="pob-invoice-upload-area">
              <div
                className={`dropzone ${dragging ? 'dragging' : ''} ${invoiceFile ? 'has-file' : ''}`}
                onClick={() => inputRef.current?.click()}
                onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click(); }}
              >
                <input ref={inputRef} type="file" accept="application/pdf,image/*"
                  onChange={(e) => setFileFrom(e.target.files?.[0])} hidden />
                {invoiceFile ? (
                  <>
                    <div className="dropzone-icon">✓</div>
                    <strong>{invoiceFile.name}</strong>
                    <span className="muted">{Math.round(invoiceFile.size / 1024)} KB — tap to change</span>
                  </>
                ) : (
                  <>
                    <div className="dropzone-icon">📷</div>
                    <strong>Tap to choose or drag & drop the invoice</strong>
                    <span className="muted">PDF or a clear photo of the invoice</span>
                  </>
                )}
              </div>
              {invoiceFile && previewUrl && (
                <div className="pob-invoice-preview">
                  <img src={previewUrl} alt="Invoice preview" style={{ maxWidth: '100%', maxHeight: 200, borderRadius: 8, border: '1px solid var(--border)' }} />
                </div>
              )}
            </div>
            <p className="ai-note">
              Invoice number &amp; date are read automatically by AI (Gemini). After upload, your submission shows <strong>pending verification</strong>.
            </p>
            <div className="form-actions">
              <button className="btn btn-primary btn-lg" disabled={busy || !invoiceFile}>
                {busy ? 'Submitting…' : demo ? 'Preview POB' : 'Submit Invoice'}
              </button>
            </div>
          </div>
        )}

        {/* Standard Mode */}
        {isStandardMode && (
          <div className="pob-form-section">
            <h4 className="pob-form-section-title">
              <span className="pob-form-step">2</span>
              Enter Brand Quantities
            </h4>
            <p className="pob-form-hint">Each brand shows its campaign minimum. POB value = Qty × PTR.</p>
            <div className="table-wrap">
              <table className="data-table">
                <thead><tr>
                  <th>Brand / Product</th><th>SKU</th><th>PTR</th><th>PTS</th>
                  <th>Min qty</th><th>Min POB</th>
                  <th style={{ width: 110 }}>Quantity</th>
                  <th>POB value</th>
                </tr></thead>
                <tbody>
                  {lines.map((l) => (
                    <tr key={l.id} className={l.q > 0 && (l.lowQty || l.lowPob) ? 'row-warning' : ''}>
                      <td><strong>{l.name}</strong>{l.brand_name ? <small className="muted"> — {l.brand_name}</small> : null}</td>
                      <td>{l.sku || '—'}</td>
                      <td>₹{l.ptr || 0}</td>
                      <td>₹{l.pts || 0}</td>
                      <td>{l.minQ > 0 ? l.minQ : '—'}</td>
                      <td>{l.minPob > 0 ? fmtMoney(l.minPob) : '—'}</td>
                      <td>
                        <TextInput type="number" min="0" step="1" value={qty[l.id] ?? ''} onChange={setQ(l.id)} />
                      </td>
                      <td>
                        <strong>{fmtMoney(l.amount)}</strong>
                        {l.lowPob && <div className="warning-text">Below min POB ({fmtMoney(l.minPob)})</div>}
                        {l.lowQty && !l.lowPob && <div className="warning-text">Below min qty ({l.minQ})</div>}
                      </td>
                    </tr>
                  ))}
                  {lines.length === 0 && <tr><td colSpan={8} className="empty-state">No products in this campaign</td></tr>}
                </tbody>
                <tfoot>
                  <tr>
                    <td colSpan={7} style={{ textAlign: 'right' }}><strong>Total POB value</strong></td>
                    <td><strong>{fmtMoney(total)}</strong></td>
                  </tr>
                </tfoot>
              </table>
            </div>
            <div className="pob-form-extras">
              <Field label="Remarks"><TextArea rows={2} value={remarks} onChange={(e) => setRemarks(e.target.value)} placeholder="Optional notes about this visit" /></Field>
            </div>
            <div className="form-actions">
              <button className="btn btn-primary btn-lg" disabled={busy}>
                {busy ? 'Submitting…' : demo ? 'Preview POB' : 'Submit POB'}
              </button>
            </div>
          </div>
        )}

        {!campaignId && (
          <div className="pob-form-empty">
            <div className="pob-form-empty-icon">📋</div>
            <p>Select a campaign above to get started</p>
          </div>
        )}
      </form>
    </div>
  );
}
