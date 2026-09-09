import { useState } from 'react';
import { api, fmtDateTime, fmtMoney } from '../../api';
import {
  DetailHero, DetailTable, ErrorBox, Field, Modal, PageHeader, ProofPane, Spinner,
  StatusBadge, TextArea, TextInput, toast, useAsync, useFileUrl,
} from '../../ui';
import { InvoiceProofReport, VerificationStatusBadge } from '../../InvoiceProofReport';

const STATUS_DISPLAY = {
  submitted: { label: 'Submitted', color: 'var(--blue)', icon: '📤', desc: 'Your submission is being received' },
  processing: { label: 'Under Review', color: 'var(--amber)', icon: '⏳', desc: 'Your submission is being reviewed' },
  auto_verified: { label: 'Approved', color: 'var(--green)', icon: '✅', desc: 'Your submission has been approved' },
  manual_review: { label: 'Under Review', color: 'var(--amber)', icon: '⏳', desc: 'Your submission is being reviewed by our team' },
  correction_required: { label: 'Needs Correction', color: 'var(--orange)', icon: '⚠️', desc: 'Please correct the issues below and resubmit' },
  approved: { label: 'Approved', color: 'var(--green)', icon: '✅', desc: 'Your submission has been approved' },
  rejected: { label: 'Not Approved', color: 'var(--red)', icon: '❌', desc: 'Your submission was not approved' },
  verified: { label: 'Approved', color: 'var(--green)', icon: '✅', desc: 'Your submission has been approved' },
  duplicate: { label: 'Duplicate', color: 'var(--red)', icon: '🔁', desc: 'This invoice was already submitted' },
  needs_review: { label: 'Under Review', color: 'var(--amber)', icon: '⏳', desc: 'Your submission is being reviewed' },
};

function getStatusDisplay(status) {
  return STATUS_DISPLAY[status] || { label: status, color: 'var(--text-secondary)', icon: '📋', desc: '' };
}

export default function PobDetailUser({ pobId, onBack }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/pob/${pobId}`));
  const eligibility = useAsync(() => api(`/api/v1/pob/${pobId}/eligibility`), [pobId]);
  const [resubmitting, setResubmitting] = useState(false);
  const [resubmitFile, setResubmitFile] = useState(null);
  const [busy, setBusy] = useState(false);

  const invoiceUrl = useFileUrl(data?.invoice_path);

  if (loading) return <div className="pob-user-detail"><Spinner label="Loading POB details…" /></div>;
  if (error) return <div className="pob-user-detail"><ErrorBox error={error} onRetry={run} /></div>;

  const d = data;
  const status = d.verification_status || d.status;
  const sd = getStatusDisplay(status);
  const grat = (d.gratifications || [])[0];
  const history = d.history || [];
  const corrections = (d.report?.checklist || []).filter((c) => c.state === 'fail');

  // Determine next action
  let nextAction = 'No action needed';
  if (status === 'correction_required' || status === 'rejected') {
    nextAction = 'Please review the issues and resubmit a corrected invoice';
  } else if (status === 'pending_verification' || status === 'needs_review' || status === 'submitted') {
    nextAction = 'Your submission is being reviewed. You will be notified once a decision is made.';
  }

  const handleResubmit = async () => {
    if (!resubmitFile) {
      toast('Please select an invoice file', 'error');
      return;
    }
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
    <div className="pob-user-detail">
      <PageHeader title={`POB #${d.id}`} subtitle={d.campaign_name}
        actions={<button className="btn btn-sm" onClick={onBack}>← Back</button>} />

      {/* Status Card — spec §21 */}
      <div className="status-card" style={{ borderLeft: `4px solid ${sd.color}`, padding: '16px 20px', marginBottom: 20, background: 'var(--surface-alt)', borderRadius: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 32 }}>{sd.icon}</span>
          <div>
            <div style={{ fontSize: 20, fontWeight: 700, color: sd.color }}>{sd.label}</div>
            <div style={{ fontSize: 14, color: 'var(--text-secondary)', marginTop: 4 }}>{sd.desc}</div>
            <div style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 4 }}>
              <strong>Next action:</strong> {nextAction}
            </div>
          </div>
        </div>
      </div>

      {/* Submission Details — spec §23 (simplified, no AI/OCR internals) */}
      <div className="detail-section">
        <h3>Submission Details</h3>
        <div className="detail-grid">
          <div className="detail-item">
            <span className="detail-label">Campaign</span>
            <span className="detail-value">{d.campaign_name}</span>
          </div>
          <div className="detail-item">
            <span className="detail-label">Product</span>
            <span className="detail-value">{d.product_name}</span>
          </div>
          <div className="detail-item">
            <span className="detail-label">Chemist</span>
            <span className="detail-value">{d.chemist_name}{d.shop_name ? ` (${d.shop_name})` : ''}</span>
          </div>
          <div className="detail-item">
            <span className="detail-label">Quantity</span>
            <span className="detail-value">{d.quantity}</span>
          </div>
          <div className="detail-item">
            <span className="detail-label">POB Amount</span>
            <span className="detail-value">{fmtMoney(d.pob_amount)}</span>
          </div>
          {d.invoice_number && (
            <div className="detail-item">
              <span className="detail-label">Invoice Number</span>
              <span className="detail-value">{d.invoice_number}</span>
            </div>
          )}
          {d.invoice_date && (
            <div className="detail-item">
              <span className="detail-label">Invoice Date</span>
              <span className="detail-value">{d.invoice_date}</span>
            </div>
          )}
          {d.invoice_amount && (
            <div className="detail-item">
              <span className="detail-label">Invoice Amount</span>
              <span className="detail-value">{fmtMoney(d.invoice_amount)}</span>
            </div>
          )}
        </div>
      </div>

      {/* Correction Guidance — spec §22 */}
      {(status === 'correction_required' || status === 'rejected') && corrections.length > 0 && (
        <div className="detail-section" style={{ borderLeft: '3px solid var(--orange)', paddingLeft: 16 }}>
          <h3 style={{ color: 'var(--orange)' }}>Corrections Needed</h3>
          <ul style={{ margin: '8px 0', paddingLeft: 20 }}>
            {corrections.map((c, i) => (
              <li key={i} style={{ marginBottom: 4, fontSize: 14 }}>
                <strong>{c.label}:</strong> {c.message}
              </li>
            ))}
          </ul>
          <button className="btn btn-primary" style={{ marginTop: 8 }} onClick={() => setResubmitting(true)}>
            Resubmit Invoice
          </button>
        </div>
      )}

      {/* Invoice Proof */}
      {d.invoice_path && (
        <div className="detail-section">
          <h3>Invoice Proof</h3>
          <ProofPane url={invoiceUrl} name={d.invoice_original_name} hint="Uploaded invoice" />
        </div>
      )}

      {/* Gratification Status — spec §24 */}
      {grat && (
        <div className="detail-section" style={{ borderLeft: '3px solid var(--green)', paddingLeft: 16 }}>
          <h3>Gratification</h3>
          <div className="detail-grid">
            <div className="detail-item">
              <span className="detail-label">Type</span>
              <span className="detail-value">{grat.type_code}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Value</span>
              <span className="detail-value">{fmtMoney(grat.scheme_value)}</span>
            </div>
            <div className="detail-item">
              <span className="detail-label">Status</span>
              <StatusBadge value={grat.status} />
            </div>
            {grat.payment_ref && (
              <div className="detail-item">
                <span className="detail-label">Payment Reference</span>
                <span className="detail-value">{grat.payment_ref}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Activity Timeline — spec §25 (simplified) */}
      {history.length > 0 && (
        <div className="detail-section">
          <h3>Activity</h3>
          <div className="timeline">
            {history.map((h) => (
              <div key={h.id} className="tl-item">
                <span className="tl-dot" />
                <div>
                  <strong>{h.action === 'submitted' ? 'Submitted' :
                    h.action === 'approved' ? 'Approved' :
                    h.action === 'rejected' ? 'Rejected' :
                    h.action === 're_opened' ? 'Re-opened' :
                    h.action === 're_extracted' ? 'Invoice re-extracted' :
                    h.action === 'corrected' ? 'Corrected' :
                    h.action === 'invoice_submitted' ? 'Invoice uploaded' :
                    h.action}</strong>
                  {h.reason && <div className="muted" style={{ fontSize: 13 }}>{h.reason}</div>}
                  <div className="muted" style={{ fontSize: 12 }}>{fmtDateTime(h.created_at)}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Resubmit Modal */}
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
