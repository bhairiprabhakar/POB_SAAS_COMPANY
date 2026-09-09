import { useState } from 'react';
import { api, fmtDateTime, fmtMoney } from '../../api';
import {
  DetailHero, DetailTable, ErrorBox, Field, Modal, PageHeader, ProofPane, SearchBox, Spinner,
  SplitDetail, StatCard, StatusBadge, Table, Tabs, TextArea, TextInput, toast, useAsync, useFileUrl,
} from '../../ui';
import { InvoiceProofReport, VerificationStatusBadge } from '../../InvoiceProofReport';

const REJECT_REASONS = [
  'Invoice does not match POB',
  'Illegible / low-quality invoice',
  'Wrong document uploaded',
  'Invoice already claimed',
  'Scheme not applicable',
  'Other',
];

export default function Verification() {
  const [status, setStatus] = useState('pending');
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState(null);
  const stats = useAsync(() => api('/api/v1/verification/stats'));
  const queue = useAsync(() =>
    api(`/api/v1/verification/queue?status=${status}${q ? `&q=${encodeURIComponent(q)}` : ''}`), [status, q]);
  const tat = useAsync(() => api('/api/v1/verification/tat/report'));

  const rows = queue.data?.items || [];
  const s = stats.data || {};

  const cols = [
    { key: 'verification_id', label: 'Ver', render: (r) => <strong>#{r.verification_id}</strong> },
    { key: 'pob_id', label: 'POB', render: (r) => <code>#{r.pob_id}</code> },
    { key: 'mr_name', label: 'MR' },
    { key: 'campaign_name', label: 'Campaign' },
    { key: 'chemist_name', label: 'Chemist', render: (r) => `${r.chemist_name || '—'}${r.city ? `, ${r.city}` : ''}` },
    { key: 'invoice_number', label: 'Invoice', render: (r) => r.invoice_number || '—' },
    { key: 'pob_amount', label: 'Amount', render: (r) => fmtMoney(r.pob_amount) },
    { key: 'v_status', label: 'Status', render: (r) => <StatusBadge value={r.v_status} /> },
    { key: 'reason', label: 'Reason', render: (r) => <span className="muted">{r.reason || '—'}</span> },
    { key: 'verifier_name', label: 'Verifier', render: (r) => r.verifier_name || '—' },
  ];

  if (selected) {
    return (
      <VerificationDetail vid={selected} onBack={() => setSelected(null)}
        onDone={() => { setSelected(null); stats.run(); queue.run(); tat.run(); }} />
    );
  }

  return (
    <div>
      <PageHeader title="Invoice Verification" subtitle="Approve, reject or flag duplicates on POB invoices"
        actions={<SearchBox value={q} onChange={setQ} placeholder="Search chemist / invoice / MR…" />} />
      <div className="stats-grid compact">
        <StatCard label="Pending" value={s.pending ?? 0} tone="amber" />
        <StatCard label="Approved" value={s.approved ?? 0} tone="green" />
        <StatCard label="Rejected" value={s.rejected ?? 0} tone="red" />
        <StatCard label="Duplicate" value={s.duplicate ?? 0} tone="red" />
        <StatCard label="Needs review" value={s.needs_review ?? 0} tone="amber" />
        <StatCard label="Avg TAT" value={`${s.avg_tat_hours ?? 0} h`} tone="blue" />
      </div>
      <Tabs items={[
        { value: 'pending', label: 'Pending' },
        { value: 'approved', label: 'Approved' },
        { value: 'auto_approved', label: 'Auto-approved' },
        { value: 'rejected', label: 'Rejected' },
        { value: 'duplicate', label: 'Duplicate' },
        { value: 'needs_review', label: 'Needs review' },
      ]} active={status} onChange={setStatus} />
      {queue.loading ? <Spinner /> : queue.error ? <ErrorBox error={queue.error} onRetry={queue.run} /> : (
        <Table cols={cols} rows={rows} keyOf={(r) => r.verification_id}
          onRowClick={(r) => setSelected(r.verification_id)} empty="No items in this queue" />
      )}
    </div>
  );
}

/* ── Verification Detail (inline workspace) ─────────────────────────────── */

function VerificationDetail({ vid, onBack, onDone }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/verification/${vid}`));
  const [rejecting, setRejecting] = useState(false);
  const [reOpening, setReOpening] = useState(false);
  const [reason, setReason] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyLabel, setBusyLabel] = useState('');
  const [reExtractConfirm, setReExtractConfirm] = useState(false);
  const [reExtractFile, setReExtractFile] = useState(null);
  const invoiceUrl = useFileUrl(data?.invoice_path);

  const act = async (fn, label) => {
    setBusy(true);
    setBusyLabel(label || 'Working…');
    try { await fn(); toast(label || 'Done', 'success'); onDone(); }
    catch (e) { toast(e.message, 'error'); } finally { setBusy(false); setBusyLabel(''); }
  };

  if (loading) return <div className="vd-workspace"><PageHeader title={`Verification #${vid}`} actions={<button className="btn" onClick={onBack}>← Back</button>} /><Spinner label="Loading verification…" /></div>;
  if (error) return <div className="vd-workspace"><PageHeader title={`Verification #${vid}`} actions={<button className="btn" onClick={onBack}>← Back</button>} /><ErrorBox error={error} onRetry={run} /></div>;

  const d = data;
  const canAct = d.v_status === 'pending' || (d.v_status === 'approved' && d.auto_verified);
  const lines = d.lines || [];
  const reportStatus = d.report?.status || d.v_status;
  const failedChecks = (d.report?.checklist || []).filter((c) => c.state === 'fail').length;
  const passedChecks = (d.report?.checklist || []).filter((c) => c.state === 'pass').length;
  const statusLabel = reportStatus === 'verified' || reportStatus === 'approved'
    ? 'VERIFIED' : reportStatus === 'rejected' ? 'REJECTED'
    : reportStatus === 'duplicate' ? 'DUPLICATE'
    : 'PENDING REVIEW';

  const handleReExtract = async () => {
    setReExtractConfirm(false);
    setBusy(true);
    setBusyLabel('AI extraction in progress…');
    try {
      let r;
      if (reExtractFile) {
        const fd = new FormData();
        fd.append('file', reExtractFile);
        r = await api(`/api/v1/pob/${d.pob_id}/re-extract`, { method: 'POST', body: fd });
      } else {
        r = await api(`/api/v1/pob/${d.pob_id}/re-extract`, { method: 'POST' });
      }
      toast(`Invoice re-extracted — status ${r.status}${r.auto_verified ? ' (auto-verified)' : ''}`, 'success');
      setReExtractFile(null);
      run();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); setBusyLabel(''); }
  };

  return (
    <div className="vd-workspace">
      <div className="vd-workspace-header">
        <div className="vd-header-left">
          <button className="btn btn-sm" onClick={onBack}>← Back to queue</button>
          <div className="vd-header-title">
            <h2>POB #{d.pob_id}</h2>
            <span className="vd-header-campaign">{d.campaign_name}</span>
          </div>
        </div>
        <div className="vd-header-right">
          <span className={`vd-status-strip vd-status-${reportStatus}`}>{statusLabel}</span>
          {failedChecks > 0 && <span className="vd-header-failed">{failedChecks} failed</span>}
          {passedChecks > 0 && <span className="vd-header-passed">{passedChecks} passed</span>}
        </div>
      </div>

      <div className="vd-workspace-body">
        <SplitDetail
          left={
            <div className="vd-invoice-pane">
              {d.invoice_verification_required
                ? <ProofPane url={invoiceUrl} name={d.invoice_original_name} hint="Uploaded invoice proof"
                    empty="Invoice not yet attached — no proof to compare against." />
                : (
                  <div className="proof-pane proof-note">
                    <div className="proof-pane-head"><strong>Invoice verification</strong></div>
                    <div className="proof-pane-body">
                      <span className="proof-empty">
                        <strong>Not required for this campaign</strong>
                        <small>The POB is verified automatically — no manual proof check.</small>
                      </span>
                    </div>
                  </div>
                )}
            </div>
          }
          right={
            <div className="vd-verification-pane">
              {busy && <div className="vd-busy-overlay"><Spinner label={busyLabel} /></div>}

              <div className="vd-meta-strip">
                <VerificationStatusBadge status={reportStatus} />
                <span className="vd-meta-item">Scheme: {d.scheme_type}</span>
                {d.invoice_verification_required
                  ? <span className="vd-meta-item vd-meta-required">Invoice verification required</span>
                  : <span className="vd-meta-item">No invoice verification</span>}
                {d.auto_verified && (
                  <span className="vd-meta-item vd-meta-auto">
                    Auto-verified{d.confidence != null ? ` · ${Math.round(d.confidence * 100)}%` : ''}
                  </span>
                )}
              </div>

              <div className="vd-summary-strip">
                <div className="vd-summary-item">
                  <span className="vd-summary-label">POB Amount</span>
                  <strong className="vd-summary-value">{fmtMoney(d.pob_amount)}</strong>
                </div>
                <div className="vd-summary-item">
                  <span className="vd-summary-label">Invoice Amount</span>
                  <strong className="vd-summary-value">{fmtMoney(d.invoice_amount)}</strong>
                </div>
                <div className="vd-summary-item">
                  <span className="vd-summary-label">Submitted Qty</span>
                  <strong className="vd-summary-value">{d.quantity}</strong>
                </div>
                {d.invoice_number && (
                  <div className="vd-summary-item">
                    <span className="vd-summary-label">Invoice No</span>
                    <strong className="vd-summary-value">{d.invoice_number}</strong>
                  </div>
                )}
                {d.invoice_date && (
                  <div className="vd-summary-item">
                    <span className="vd-summary-label">Invoice Date</span>
                    <strong className="vd-summary-value">{d.invoice_date}</strong>
                  </div>
                )}
              </div>

              <div className="vd-submission-brief">
                <span>{d.mr_name} · {d.product_name}{d.sku ? ` (${d.sku})` : ''} · {d.chemist_name}{d.shop_name ? ` (${d.shop_name})` : ''}</span>
                {d.city && <span className="muted"> · {d.city}{d.state ? `, ${d.state}` : ''}</span>}
                {d.remarks && <span className="muted"> · {d.remarks}</span>}
              </div>

              <InvoiceProofReport report={d.report} po={{ pob_amount: d.pob_amount, invoice_amount: d.invoice_amount, quantity: d.quantity, invoice_date: d.invoice_date }} />

              {lines.length > 0 && (
                <div className="vd-section">
                  <h4 className="vd-section-title">Brand Verification — Same Visit</h4>
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead><tr>
                        <th>Brand / Product</th><th>SKU</th><th>Qty</th><th>POB value</th>
                        <th>Invoice qty</th><th>Invoice amt</th><th>Match</th>
                      </tr></thead>
                      <tbody>
                        {lines.map((l) => (
                          <tr key={l.id} className={l.id === d.pob_id ? 'row-current' : ''}>
                            <td>
                              <strong>{l.product_name}</strong>
                              {l.brand_name && String(l.brand_name).toLowerCase() !== String(l.product_name).toLowerCase()
                                ? <small className="muted"> — {l.brand_name}</small> : null}
                              {l.id === d.pob_id && <span className="verdict-badge verdict-blue verdict-sm" style={{ marginLeft: 6 }}>This POB</span>}
                            </td>
                            <td>{l.sku || '—'}</td>
                            <td>{l.quantity}</td>
                            <td><strong>{fmtMoney(l.pob_amount)}</strong></td>
                            <td>
                              {l.extracted_qty != null
                                ? (<><strong>{l.extracted_qty}</strong>{l.extracted_qty !== l.quantity && <small className="muted"> (auto)</small>}</>)
                                : '—'}
                            </td>
                            <td>
                              {l.extracted_amount != null
                                ? (<><strong>{fmtMoney(l.extracted_amount)}</strong>{l.extracted_amount !== (l.invoice_amount || 0) && <small className="muted"> (auto)</small>}</>)
                                : '—'}
                            </td>
                            <td>
                              {l.brand_matched
                                ? <span className="verdict-badge verdict-green verdict-sm">MATCHED</span>
                                : <span className="verdict-badge verdict-amber verdict-sm">NOT ON INVOICE</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {(d.extra_invoice_brands || []).length > 0 && (
                    <div className="vd-extra-brands">
                      <h5 className="vd-section-subtitle">Brands on invoice not in this POB</h5>
                      <div className="table-wrap">
                        <table className="data-table">
                          <thead><tr><th>Product</th><th>Qty</th><th>Rate</th><th>Amount</th></tr></thead>
                          <tbody>
                            {(d.extra_invoice_brands || []).map((it, i) => (
                              <tr key={i}>
                                <td><strong>{it.description}</strong></td>
                                <td>{it.qty}</td>
                                <td>{fmtMoney(it.rate)}</td>
                                <td><strong>{fmtMoney(it.amount)}</strong></td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {d.approvals?.length > 0 && (
                <div className="vd-section">
                  <h4 className="vd-section-title">Approval Workflow</h4>
                  <div className="timeline">
                    {d.approvals.map((a) => (
                      <div key={a.id} className="tl-item">
                        <span className="tl-dot" />
                        <div>
                          <strong>Step {a.step}: {a.step_name || a.status}</strong> <StatusBadge value={a.status} />
                          {a.role_name && <div className="muted">{a.role_name}{a.comment ? ` — ${a.comment}` : ''}</div>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="vd-section vd-activity-meta">
                <span className="muted">Submitted {fmtDateTime(d.v_created_at)}</span>
                {d.verified_at && <span className="muted"> · {d.verifier_name || 'Verified'} {fmtDateTime(d.verified_at)}</span>}
              </div>
            </div>
          }
        />
      </div>

      {canAct && (
        <div className="vd-action-bar">
          <div className="vd-action-left">
            <span className="vd-action-result">Verification Result: <strong>{statusLabel}</strong></span>
            {failedChecks > 0 && <span className="vd-action-issues">{failedChecks} issue(s)</span>}
          </div>
          <div className="vd-action-right">
            {d.v_status === 'approved' && d.auto_verified ? (
              <>
                <button className="btn btn-amber" disabled={busy} onClick={() => setReOpening(true)}>
                  Re-open for review
                </button>
                <span className="vd-action-result" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                  Auto-verified · {Math.round((d.confidence || 0) * 100)}% confidence
                </span>
              </>
            ) : (
              <>
                {d.invoice_verification_required && (
                  <button className="btn" disabled={busy} onClick={() => setReExtractConfirm(true)}>
                    Re-extract Invoice
                  </button>
                )}
                {canAct && (
                  <Field label="" style={{ marginBottom: 0 }}>
                    <TextInput value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note" className="vd-action-note" />
                  </Field>
                )}
                <button className="btn btn-danger" disabled={busy} onClick={() => setRejecting(true)}>Reject</button>
                <button className="btn btn-amber" disabled={busy} onClick={() => act(() => api(`/api/v1/verification/${vid}/duplicate`, {
                  method: 'POST', body: { reason: 'Marked duplicate during verification' },
                }), 'Marked as duplicate')}>Mark Duplicate</button>
                <button className="btn btn-primary" disabled={busy} onClick={() => act(() =>
                  api(`/api/v1/verification/${vid}/approve`, { method: 'POST', body: { note } }),
                  'POB approved — gratification created')}>Approve</button>
              </>
            )}
          </div>
        </div>
      )}

      {/* Reject modal */}
      <Modal open={rejecting} title="Confirm rejection" onClose={() => setRejecting(false)}
        footer={<>
          <button className="btn" onClick={() => setRejecting(false)}>Cancel</button>
          <button className="btn btn-danger" disabled={!reason.trim() || busy} onClick={() =>
            act(() => api(`/api/v1/verification/${vid}/reject`, { method: 'POST', body: { reason } }), 'POB rejected')}>
            {busy ? 'Rejecting…' : 'Confirm rejection'}
          </button>
        </>}>
        <div style={{ marginBottom: 16 }}>
          <strong>Reject POB #{d.pob_id}?</strong>
          <span className="muted" style={{ marginLeft: 8 }}>{d.campaign_name} · {d.chemist_name}{d.shop_name ? ` (${d.shop_name})` : ''}</span>
        </div>
        <Field label="Rejection reason" required>
          <div className="reason-chips">
            {REJECT_REASONS.map((r) => (
              <button key={r} type="button" className={`chip ${reason === r ? 'active' : ''}`}
                onClick={() => setReason(reason === r ? '' : r)}>{r}</button>
            ))}
          </div>
          <TextArea rows={3} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="Describe the reason — this is shared with the MR" />
        </Field>
        <p className="warn-note">
          Rejecting marks this POB as <strong>Rejected</strong>, cancels any gratification for it and
          notifies the MR with your reason. This action cannot be undone from this screen.
        </p>
      </Modal>

      {/* Re-open for review modal */}
      <Modal open={reOpening} title="Re-open for manual review" onClose={() => { setReOpening(false); setReason(''); }}
        footer={<>
          <button className="btn" onClick={() => { setReOpening(false); setReason(''); }}>Cancel</button>
          <button className="btn btn-amber" disabled={!reason.trim() || busy} onClick={() =>
            act(() => api(`/api/v1/verification/${vid}/re_open`, { method: 'POST', body: { reason } }),
            'POB re-opened for manual review').then(() => { setReOpening(false); setReason(''); })}>
            {busy ? 'Re-opening…' : 'Re-open for review'}
          </button>
        </>}>
        <div style={{ marginBottom: 16 }}>
          <strong>Re-open POB #{d.pob_id} for review?</strong>
          <span className="muted" style={{ marginLeft: 8 }}>{d.campaign_name} · {d.chemist_name}{d.shop_name ? ` (${d.shop_name})` : ''}</span>
        </div>
        <p style={{ margin: '0 0 12px', fontSize: 13 }}>
          This POB was <strong>auto-approved</strong> by AI verification ({Math.round((d.confidence || 0) * 100)}% confidence).
          Re-opening moves it back to the pending queue for manual review.
        </p>
        <Field label="Reason for re-opening" required>
          <TextArea rows={3} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Client reported discrepancy, need to re-verify details" />
        </Field>
      </Modal>

      {/* Re-extract confirmation */}
      <Modal open={reExtractConfirm} title="Replace current invoice?" onClose={() => { setReExtractConfirm(false); setReExtractFile(null); }}
        footer={<>
          <button className="btn" onClick={() => { setReExtractConfirm(false); setReExtractFile(null); }}>Cancel</button>
          <button className="btn btn-primary" disabled={busy} onClick={handleReExtract}>
            {busy ? 'Re-extracting…' : 'Replace & Re-extract'}
          </button>
        </>}>
        <p>Current extraction data will be replaced with data extracted from the {reExtractFile ? 'new document' : 'current invoice'}.</p>
        <p className="muted" style={{ marginTop: 8 }}>All verification rules will be automatically rerun after extraction.</p>
        <div style={{ marginTop: 12 }}>
          <Field label="Upload new invoice (optional — leave empty to re-extract current)">
            <input type="file" accept=".pdf,.jpg,.jpeg,.png,.webp,.gif"
              onChange={(e) => setReExtractFile(e.target.files?.[0] || null)} />
          </Field>
        </div>
      </Modal>
    </div>
  );
}
