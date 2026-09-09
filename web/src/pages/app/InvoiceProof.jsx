import { useEffect, useRef, useState } from 'react';
import { api, fmtMoney, uploadFile } from '../../api';
import {
  ErrorBox, Field, PageHeader, ProofPane, Select, Spinner, SplitDetail, toast, useAsync,
} from '../../ui';

export default function InvoiceProof({ demo }) {
  const groups = useAsync(() => api('/api/v1/pob/pending-invoice'));
  const [campaignId, setCampaignId] = useState('');
  const [chemistId, setChemistId] = useState('');
  const [detail, setDetail] = useState(null);
  const [file, setFile] = useState(null);
  const [previewUrl, setPreviewUrl] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);

  const [mode, setMode] = useState('existing');
  const [directCampaigns, setDirectCampaigns] = useState(null);
  const [directChemists, setDirectChemists] = useState(null);
  const [dCampaignId, setDCampaignId] = useState('');
  const [dChemistId, setDChemistId] = useState('');

  useEffect(() => {
    if (!file) { setPreviewUrl(null); return; }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const rows = groups.data?.items || [];
  const hasPending = rows.length > 0;
  const activeMode = mode === 'direct' ? 'direct' : (hasPending ? 'existing' : 'direct');

  const switchTo = async (m) => {
    setMode(m);
    if (m === 'direct' && !directCampaigns) {
      try {
        const [cs, ch] = await Promise.all([
          api('/api/v1/campaigns?active=true&status=active'),
          api('/api/v1/chemists?limit=500'),
        ]);
        setDirectCampaigns(cs.items || []);
        setDirectChemists(ch.items || []);
      } catch (err) { toast(err.message, 'error'); }
    }
  };

  const campaigns = [...new Map(rows.map((g) => [g.campaign_id, { id: g.campaign_id, name: g.campaign_name }])).values()];
  const chemists = rows.filter((g) => String(g.campaign_id) === String(campaignId));
  const chemBase = (c) => `${c.chemist_name || ''}${c.shop_name ? ` — ${c.shop_name}` : ''}${c.city ? `, ${c.city}` : ''}${c.state ? `, ${c.state}` : ''}`;
  const chemLabelCount = chemists.reduce((m, c) => ((m[chemBase(c)] = (m[chemBase(c)] || 0) + 1), m), {});

  const pickChemist = async (cid) => {
    setChemistId(cid);
    setDetail(null);
    if (!cid) return;
    try {
      const d = await api(`/api/v1/pob/pending-invoice/detail?campaign_id=${campaignId}&chemist_id=${cid}`);
      setDetail(d);
    } catch (err) { toast(err.message, 'error'); }
  };

  const setFileFrom = (f) => {
    if (!f) return;
    if (!/^(application\/pdf|image\/)/.test(f.type)) {
      toast('Please choose a PDF or an image of the invoice', 'error');
      return;
    }
    setFile(f);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    setFileFrom(e.dataTransfer?.files?.[0]);
  };

  const resetForm = () => {
    setFile(null); setDetail(null);
    setChemistId(''); setCampaignId('');
    setDChemistId(''); setDCampaignId('');
  };

  const submit = async (e) => {
    e.preventDefault();
    const cid = activeMode === 'direct' ? dCampaignId : campaignId;
    const chid = activeMode === 'direct' ? dChemistId : chemistId;
    if (!cid || !chid) { toast('Select campaign and chemist', 'error'); return; }
    if (!file) { toast('Attach the invoice proof file', 'error'); return; }
    setBusy(true);
    try {
      if (demo) {
        await new Promise((r) => setTimeout(r, 400));
        toast('Test mode: invoice proof accepted. Nothing uploaded.', 'success');
        resetForm();
        return;
      }
      const extra = { campaign_id: Number(cid), chemist_id: Number(chid) };
      if (activeMode === 'direct') extra.direct = 1;
      const r = await uploadFile('/api/v1/pob/invoice-proof', file, extra);
      const done = r.results?.filter((x) => x.status === 'pending_verification' || x.status === 'verified').length || 0;
      toast(`${done}/${r.results?.length || 0} POB line(s) submitted for verification`, 'success');
      resetForm();
      groups.run();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  if (groups.loading) return <Spinner label="Loading POBs pending invoice…" />;
  if (groups.error) return <ErrorBox error={groups.error} onRetry={groups.run} />;

  const uploadPane = (
    <div className="inv-upload-pane">
      <h4 className="pob-form-section-title">
        <span className="pob-form-step">2</span>
        Upload Invoice
      </h4>
      <div
        className={`dropzone ${dragging ? 'dragging' : ''} ${file ? 'has-file' : ''}`}
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
        {file ? (
          <>
            <div className="dropzone-icon">✓</div>
            <strong>{file.name}</strong>
            <span className="muted">{Math.round(file.size / 1024)} KB — tap to change</span>
          </>
        ) : (
          <>
            <div className="dropzone-icon">📷</div>
            <strong>Tap to choose or drag & drop the invoice</strong>
            <span className="muted">PDF or a clear photo of the invoice</span>
          </>
        )}
      </div>
      {file && previewUrl && (
        <div style={{ marginTop: 12 }}>
          <ProofPane url={previewUrl} name={file.name} hint="Selected file preview" />
        </div>
      )}
      <p className="ai-note">
        Invoice number &amp; date are read automatically by AI (Gemini). After upload, your POB shows <strong>pending verification</strong>.
      </p>
      <div className="form-actions" style={{ marginTop: 12 }}>
        <button className="btn btn-primary btn-lg" disabled={busy || !file}>
          {busy ? 'Uploading…' : demo ? 'Preview upload' : 'Submit invoice proof'}
        </button>
      </div>
    </div>
  );

  return (
    <div className="inv-proof-page">
      {demo && (
        <div className="scope-banner" data-tone="amber">
          <span className="scope-dot" />
          <div>
            <strong>Test mode — how your team uploads an invoice</strong>
            <small>Live preview. No file is uploaded and nothing is saved.</small>
          </div>
        </div>
      )}

      <PageHeader title="Submit Invoice Proof"
        subtitle="Upload the invoice for a submitted POB — the invoice number and date are read automatically by AI." />

      {activeMode === 'direct' ? (
        <form className="card form-card" onSubmit={submit}>
          <div className="pob-form-section">
            <h4 className="pob-form-section-title">
              <span className="pob-form-step">1</span>
              Select Campaign &amp; Chemist
            </h4>
            <div className="grid-2">
              <Field label="Campaign" required hint="Active campaigns (invoice brands must belong to the campaign)">
                <Select value={dCampaignId}
                  onChange={(e) => { setDCampaignId(e.target.value); setDChemistId(''); }} required
                  options={(directCampaigns || []).map((c) => ({ value: c.id, label: c.name }))} />
              </Field>
              <Field label="Chemist" required hint="The pharmacy the invoice is billed to">
                <Select value={dChemistId} onChange={(e) => setDChemistId(e.target.value)} required
                  options={(directChemists || []).map((c) => {
                    const base = chemBase(c);
                    return { value: c.id, label: `${base}${c.mobile ? ` · ☎ ${c.mobile}` : ''}` };
                  })} />
              </Field>
            </div>
          </div>
          {uploadPane}
          {hasPending && (
            <p className="muted" style={{ marginTop: 12, textAlign: 'center' }}>
              You have POBs waiting for invoice —{' '}
              <button type="button" className="btn-link" onClick={() => switchTo('existing')}>use the existing flow</button>.
            </p>
          )}
        </form>
      ) : rows.length === 0 ? (
        <div className="card form-card">
          <div className="pob-form-empty">
            <div className="pob-form-empty-icon">🧾</div>
            <p>No POBs pending invoice proof</p>
            <span className="muted">Submit a POB visit first, or upload an invoice directly.</span>
          </div>
          <div className="form-actions" style={{ justifyContent: 'center' }}>
            <button className="btn btn-primary" onClick={() => switchTo('direct')}>Upload invoice without a POB visit</button>
          </div>
        </div>
      ) : (
        <form className="card form-card" onSubmit={submit}>
          <div className="pob-form-section">
            <h4 className="pob-form-section-title">
              <span className="pob-form-step">1</span>
              Select Campaign &amp; Chemist
            </h4>
            <div className="grid-2">
              <Field label="Campaign" required hint="Campaigns where you have submitted POBs">
                <Select value={campaignId} onChange={(e) => { setCampaignId(e.target.value); setChemistId(''); setDetail(null); }} required
                  options={campaigns.map((c) => ({ value: c.id, label: c.name }))} />
              </Field>
              <Field label="Chemist" required hint="Your registered chemists with POBs in this campaign">
                <Select value={chemistId} onChange={(e) => pickChemist(e.target.value)} required
                  options={chemists.map((c) => {
                    const base = chemBase(c);
                    return {
                      value: c.chemist_id,
                      label: chemLabelCount[base] > 1
                        ? `${base} · ☎ ${c.mobile || `#${c.chemist_id}`} · #${c.chemist_id} · ${c.item_count} brand(s), ${fmtMoney(c.total_amount)}`
                        : `${base} · ${c.item_count} brand(s), ${fmtMoney(c.total_amount)}`,
                    };
                  })} />
              </Field>
            </div>
          </div>

          {detail ? (
            <SplitDetail
              left={uploadPane}
              right={
                <div>
                  <h4 className="pob-form-section-title">
                    <span className="pob-form-step">3</span>
                    Review Product Details
                  </h4>
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead><tr>
                        <th>Brand / Product</th><th>SKU</th><th>Qty</th><th>PTR</th><th>PTS</th><th>MRP</th><th>Invoice</th><th>POB value</th>
                      </tr></thead>
                      <tbody>
                        {detail.items.map((i) => (
                          <tr key={i.id}>
                            <td><strong>{i.product_name}</strong>{i.brand_name ? <small className="muted"> — {i.brand_name}</small> : null}</td>
                            <td>{i.sku || '—'}</td>
                            <td>{i.quantity}</td>
                            <td>₹{i.ptr || 0}</td>
                            <td>₹{i.pts || 0}</td>
                            <td>₹{i.mrp || 0}</td>
                            <td>{i.invoice_amount ? fmtMoney(i.invoice_amount) : '—'}</td>
                            <td><strong>{fmtMoney(i.pob_amount)}</strong></td>
                          </tr>
                        ))}
                      </tbody>
                      <tfoot>
                        <tr><td colSpan={7} style={{ textAlign: 'right' }}><strong>Total</strong></td>
                          <td><strong>{fmtMoney(detail.total_amount)}</strong></td></tr>
                      </tfoot>
                    </table>
                  </div>
                </div>
              }
            />
          ) : (
            <div className="pob-form-empty">
              <p>Pick a campaign and chemist to see submitted brands</p>
            </div>
          )}
          <p className="muted" style={{ marginTop: 12, textAlign: 'center' }}>
            No visit for this chemist?{' '}
            <button type="button" className="btn-link" onClick={() => switchTo('direct')}>Upload invoice directly</button>
            {' '}— brand lines are created automatically by AI.
          </p>
        </form>
      )}
    </div>
  );
}
