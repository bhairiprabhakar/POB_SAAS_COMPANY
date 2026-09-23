import { useState } from 'react';
import { api, fmtDate, fmtDateTime, fmtMoney } from '../../api';
import {
  DetailHero, DetailTable, ErrorBox, Field, Modal, PageHeader, ProofPane, Spinner,
  SplitDetail, StatusBadge, toast, useAsync, useFileUrl,
} from '../../ui';
import { InvoiceSummaryReport } from '../../InvoiceProofReport';

// Authoritative set (saas/tenant_schema.py pob_activities.status comment):
// submitted | pending_verification | verified | rejected | duplicate | needs_review
const STATUS_TONE = {
  verified: 'green', rejected: 'red', duplicate: 'red',
  pending_verification: 'amber', needs_review: 'amber', submitted: 'amber',
};

// verification_history.action, as actually inserted across pob.py/verification.py
const HISTORY_LABELS = {
  submitted: 'Submitted', invoice_submitted: 'Invoice uploaded', re_extracted: 'Invoice re-extracted',
  approved: 'Approved', rejected: 'Rejected', duplicate: 'Marked duplicate',
  needs_review: 'Sent for review', re_opened: 'Re-opened', corrected: 'Corrected',
};

export default function PobDetailUser({ pobId, onBack }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/pob/${pobId}`));
  const eligibility = useAsync(() => api(`/api/v1/pob/${pobId}/eligibility`), [pobId]);
  const [resubmitting, setResubmitting] = useState(false);
  const [resubmitFile, setResubmitFile] = useState(null);
  const [busy, setBusy] = useState(false);

  const invoiceUrl = useFileUrl(data?.invoice_path);

  const backAction = <button className="btn btn-sm" onClick={onBack}>← Back</button>;
  if (loading) return <div><PageHeader title="POB detail" actions={backAction} /><Spinner label="Loading POB details…" /></div>;
  if (error) return <div><PageHeader title="POB detail" actions={backAction} /><ErrorBox error={error} onRetry={run} /></div>;

  const d = data;
  const status = d.status;
  const tone = STATUS_TONE[status] || 'gray';
  const grat = (d.gratifications || [])[0];
  const history = d.history || [];
  const lines = d.lines || [];
  const elig = eligibility.data;
  const corrections = (d.report?.checklist || []).filter((c) => c.state === 'fail');
  const canResubmit = ['rejected', 'needs_review'].includes(status) && corrections.length > 0;
  const chemistLabel = `${d.chemist_name || ''}${d.shop_name ? ` (${d.shop_name})` : ''}`;
  const chemistLocation = d.chemist_city ? `${d.chemist_city}${d.chemist_state ? `, ${d.chemist_state}` : ''}` : null;

  const handleResubmit = async () => {
    if (!resubmitFile) { toast('Please select an invoice file', 'error'); return; }
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append('file', resubmitFile);
      await api(`/api/v1/pob/${pobId}/re-extract`, { method: 'POST', body: fd });
      toast('Invoice resubmitted successfully', 'success');
      setResubmitting(false);
      setResubmitFile(null);
      run();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageHeader title={`POB #${d.id}`} subtitle={d.campaign_name} actions={backAction} />

      <DetailHero
        title={d.campaign_name}
        subtitle={d.scheme_type ? `${d.scheme_type.replaceAll('_', ' ')} scheme` : undefined}
        badge={<StatusBadge value={status} />}
        tone={tone}
        primaryLabel="POB Amount"
        primary={fmtMoney(d.pob_amount)}
        secondary={d.invoice_amount ? `Invoice ${fmtMoney(d.invoice_amount)}` : undefined}
        facts={[
          ['Chemist', chemistLabel],
          chemistLocation ? ['Location', chemistLocation] : null,
          ['Product', d.product_name],
          ['Quantity', d.quantity],
          ['Submitted', fmtDate(d.created_at)],
        ]}
      />

      <SplitDetail
        left={
          d.invoice_path ? (
            <ProofPane url={invoiceUrl} name={d.invoice_original_name} hint="Uploaded invoice" />
          ) : (
            <div className="proof-pane proof-note">
              <div className="proof-pane-head"><strong>Invoice proof</strong></div>
              <div className="proof-pane-body">
                <span className="proof-empty">
                  <strong>No invoice attached</strong>
                  {!d.invoice_verification_required && <small>Not required for this campaign.</small>}
                </span>
              </div>
            </div>
          )
        }
        right={
          <div>
            <div className="vd-section">
              <h4 className="vd-section-title">Your Reward</h4>
              {grat ? (
                <div className="detail-hero" data-tone={grat.status === 'completed' ? 'green' : 'amber'}>
                  <div className="detail-hero-top">
                    <div className="detail-hero-title">
                      <h2>{grat.type_code.replaceAll('_', ' ')}</h2>
                      <div className="detail-hero-badges"><StatusBadge value={grat.status} /></div>
                    </div>
                    <div className="detail-hero-amount">
                      <span className="stat-label">Value</span>
                      <span className="detail-hero-value">{fmtMoney(grat.scheme_value)}</span>
                    </div>
                  </div>
                  {grat.payment_ref && (
                    <div className="detail-hero-facts">
                      <div className="fact-pill"><span>Payment ref</span><strong>{grat.payment_ref}</strong></div>
                    </div>
                  )}
                </div>
              ) : elig ? (
                <p className="muted" style={{ margin: 0 }}>
                  {elig.eligible ? '🎉 ' : '💡 '}{elig.message || 'No gratification rules configured for this campaign.'}
                </p>
              ) : (
                <Spinner label="Checking reward eligibility…" />
              )}
            </div>

            {d.invoice_path && <InvoiceSummaryReport report={d.report} po={d} lines={lines} />}

            {canResubmit && (
              <div className="vd-section">
                {corrections.length > 0 && (
                  <ul style={{ margin: '0 0 10px', paddingLeft: 20 }}>
                    {corrections.map((c, i) => (
                      <li key={i} style={{ marginBottom: 4, fontSize: 14 }}><strong>{c.label}:</strong> {c.detail || c.message}</li>
                    ))}
                  </ul>
                )}
                <button className="btn btn-primary" onClick={() => setResubmitting(true)}>Resubmit Invoice</button>
              </div>
            )}

            <div className="vd-section">
              <DetailTable title="Submission Details" rows={[
                ['Product', d.product_name],
                ['Chemist', chemistLabel],
                d.chemist_mobile ? ['Chemist mobile', d.chemist_mobile] : null,
                ['Quantity', d.quantity],
                d.invoice_number ? ['Invoice Number', d.invoice_number] : null,
                d.invoice_date ? ['Invoice Date', d.invoice_date] : null,
                d.invoice_amount ? ['Invoice Amount', fmtMoney(d.invoice_amount)] : null,
              ]} />
            </div>

            {history.length > 0 && (
              <div className="vd-section">
                <h4 className="vd-section-title">Activity</h4>
                <div className="timeline">
                  {history.map((h) => (
                    <div key={h.id} className="tl-item">
                      <span className="tl-dot" />
                      <div>
                        <strong>{HISTORY_LABELS[h.action] || h.action}</strong>
                        {h.verifier_name && <span className="muted"> — {h.verifier_name}</span>}
                        {h.reason && <div className="muted" style={{ fontSize: 13 }}>{h.reason}</div>}
                        <div className="muted" style={{ fontSize: 12 }}>{fmtDateTime(h.created_at)}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        }
      />

      <Modal open={resubmitting} title="Resubmit Invoice" onClose={() => { setResubmitting(false); setResubmitFile(null); }}
        footer={<>
          <button className="btn" onClick={() => { setResubmitting(false); setResubmitFile(null); }}>Cancel</button>
          <button className="btn btn-primary" disabled={busy || !resubmitFile} onClick={handleResubmit}>
            {busy ? 'Uploading…' : 'Resubmit'}
          </button>
        </>}>
        <p>Upload a corrected invoice to replace the current one. The system will re-examine all details.</p>
        <Field label="New invoice file" required>
          <input type="file" accept=".pdf,.jpg,.jpeg,.png,.webp,.gif"
            onChange={(e) => setResubmitFile(e.target.files?.[0] || null)} />
        </Field>
      </Modal>
    </div>
  );
}
