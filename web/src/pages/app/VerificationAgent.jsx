import { useState } from 'react';
import { api, fmtDateTime, fmtMoney } from '../../api';
import {
  DetailHero, DetailTable, ErrorBox, Field, Modal, PageHeader, ProofPane, SearchBox, Spinner,
  SplitDetail, StatCard, StatusBadge, Table, Tabs, TextArea, TextInput, roleLabel, toast, useAsync, useFileUrl,
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

export default function VerificationAgent() {
  const [queueTab, setQueueTab] = useState('pending_agent');
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState(null);
  const stats = useAsync(() => api('/api/v1/verification/stats'));
  const queue = useAsync(() =>
    api(`/api/v1/verification/queue?status=${queueTab}${q ? `&q=${encodeURIComponent(q)}` : ''}`),
    [queueTab, q]);

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
    { key: 'confidence', label: 'Confidence', render: (r) => {
      if (r.confidence == null) return '—';
      const pct = Math.round(r.confidence * 100);
      const tone = pct >= 90 ? 'green' : pct >= 60 ? 'amber' : 'red';
      return <span className={`badge badge-${tone}`}>{pct}%</span>;
    }},
    { key: 'v_status', label: 'Status', render: (r) => <StatusBadge value={r.v_status} /> },
    { key: 'verifier_name', label: 'Agent', render: (r) => r.verifier_name || '—' },
  ];

  if (selected) {
    return (
      <AgentWorkspace vid={selected} onBack={() => setSelected(null)}
        onDone={() => { setSelected(null); stats.run(); queue.run(); }} />
    );
  }

  return (
    <div>
      <PageHeader title="Verification Agent" subtitle="AI-first invoice verification workspace"
        actions={<SearchBox value={q} onChange={setQ} placeholder="Search chemist / invoice / MR…" />} />

      <div className="stats-grid compact">
        <StatCard label="Pending Review" value={s.manual_review ?? s.pending ?? 0} tone="amber" />
        <StatCard label="Auto-Approved" value={s.auto_approved ?? 0} tone="green" />
        <StatCard label="Approved" value={s.approved ?? 0} tone="green" />
        <StatCard label="Rejected" value={s.rejected ?? 0} tone="red" />
        <StatCard label="Duplicate" value={s.duplicate ?? 0} tone="red" />
        <StatCard label="Avg TAT" value={`${s.avg_tat_hours ?? 0} h`} tone="blue" />
      </div>

      <Tabs items={[
        { value: 'pending_agent', label: 'Manual Review' },
        { value: 'auto_approved', label: 'Auto-Verified' },
        { value: 'approved', label: 'Approved' },
        { value: 'rejected', label: 'Rejected' },
        { value: 'duplicate', label: 'Duplicate' },
        { value: 'pending', label: 'Pending' },
      ]} active={queueTab} onChange={setQueueTab} />

      {queue.loading ? <Spinner /> : queue.error ? <ErrorBox error={queue.error} onRetry={queue.run} /> : (
        <Table cols={cols} rows={rows} keyOf={(r) => r.verification_id}
          onRowClick={(r) => setSelected(r.verification_id)} empty="No items in this queue" />
      )}
    </div>
  );
}

/* ── Agent Workspace — Decision-first hierarchy ─────────────────────────── */

function AgentWorkspace({ vid, onBack, onDone }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/verification/${vid}`));
  const pipeline = useAsync(() => api(`/api/v1/verification/${vid}/pipeline`), [vid]);
  const corrections = useAsync(() => api(`/api/v1/verification/${vid}/corrections`), [vid]);

  const [rejecting, setRejecting] = useState(false);
  const [reOpening, setReOpening] = useState(false);
  const [flagging, setFlagging] = useState(false);
  const [reason, setReason] = useState('');
  const [flagReason, setFlagReason] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyLabel, setBusyLabel] = useState('');
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
  const lines = d.lines || [];
  const report = d.report || {};
  const reportStatus = report.status || d.v_status;
  const pipelineSteps = pipeline.data?.steps || [];
  const correctionList = corrections.data?.items || [];
  const failedChecks = (report.checklist || []).filter((c) => c.state === 'fail');
  const passedChecks = (report.checklist || []).filter((c) => c.state === 'pass');
  const confidence = d.confidence != null ? Math.round(d.confidence * 100) : null;

  // Decision summary
  let decisionType = 'pending';
  let decisionLabel = 'PENDING REVIEW';
  let decisionColor = 'var(--amber)';
  if (reportStatus === 'verified' || reportStatus === 'approved') {
    decisionType = d.auto_verified ? 'auto_approved' : 'approved';
    decisionLabel = d.auto_verified ? 'AUTO-APPROVED' : 'APPROVED';
    decisionColor = 'var(--green)';
  } else if (reportStatus === 'rejected') {
    decisionType = 'rejected';
    decisionLabel = 'REJECTED';
    decisionColor = 'var(--red)';
  } else if (reportStatus === 'duplicate') {
    decisionType = 'duplicate';
    decisionLabel = 'DUPLICATE';
    decisionColor = 'var(--red)';
  } else if (d.pipeline_status === 'pending_agent') {
    decisionType = 'manual_review';
    decisionLabel = 'NEEDS REVIEW';
    decisionColor = 'var(--amber)';
  }

  const canAct = ['pending', 'pending_verification', 'needs_review', 'manual_review'].includes(d.v_status)
    || (d.v_status === 'approved' && d.auto_verified);

  return (
    <div className="vd-workspace">
      {/* Workspace Header */}
      <div className="vd-workspace-header">
        <div className="vd-header-left">
          <button className="btn btn-sm" onClick={onBack}>← Back to queue</button>
          <div className="vd-header-title">
            <h2>POB #{d.pob_id}</h2>
            <span className="vd-header-campaign">{d.campaign_name}</span>
          </div>
        </div>
        <div className="vd-header-right">
          {confidence != null && (
            <span className="vd-confidence-badge" style={{ color: confidence >= 90 ? 'var(--green)' : confidence >= 60 ? 'var(--amber)' : 'var(--red)' }}>
              {confidence}% confidence
            </span>
          )}
          <span className="vd-action-result">Agent: {d.verifier_name || 'Unclaimed'}</span>
        </div>
      </div>

      <div className="vd-workspace-body">
        <SplitDetail
          left={
            <div className="vd-invoice-pane">
              {d.invoice_verification_required
                ? <ProofPane url={invoiceUrl} name={d.invoice_original_name} hint="Uploaded invoice proof" />
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

              {/* 1. Decision Summary Card — spec §12 */}
              <div className="decision-summary" style={{ borderLeft: `4px solid ${decisionColor}`, padding: '12px 16px', marginBottom: 16, background: 'var(--surface-alt)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <span style={{ fontSize: 24, fontWeight: 700, color: decisionColor }}>
                    {decisionType === 'auto_approved' && '✅'}
                    {decisionType === 'approved' && '✅'}
                    {decisionType === 'rejected' && '❌'}
                    {decisionType === 'duplicate' && '🔁'}
                    {decisionType === 'pending' && '⏳'}
                    {decisionType === 'manual_review' && '⚠️'}
                  </span>
                  <div>
                    <div style={{ fontSize: 18, fontWeight: 700, color: decisionColor }}>{decisionLabel}</div>
                    <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                      {failedChecks.length === 0 && passedChecks.length > 0 && `All ${passedChecks.length} checks passed`}
                      {failedChecks.length > 0 && `${failedChecks.length} issue(s) found — ${failedChecks.map((c) => c.label).join(', ')}`}
                      {d.auto_verified && confidence != null && ` · ${confidence}% AI confidence`}
                      {!d.auto_verified && failedChecks.length === 0 && 'Pending agent decision'}
                    </div>
                  </div>
                </div>
              </div>

              {/* Summary strip */}
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

              {/* Submission brief */}
              <div className="vd-submission-brief">
                <span>{d.mr_name} · {d.product_name}{d.sku ? ` (${d.sku})` : ''} · {d.chemist_name}{d.shop_name ? ` (${d.shop_name})` : ''}</span>
                {d.city && <span className="muted"> · {d.city}{d.state ? `, ${d.state}` : ''}</span>}
                {d.remarks && <span className="muted"> · {d.remarks}</span>}
              </div>

              {/* 2. Auto-Verification Report — spec §13 */}
              <InvoiceProofReport report={d.report} po={{ pob_amount: d.pob_amount, invoice_amount: d.invoice_amount, quantity: d.quantity, invoice_date: d.invoice_date }} />

              {/* 3. POB vs Invoice Comparison — spec §14 */}
              {lines.length > 0 && (
                <div className="vd-section">
                  <h4 className="vd-section-title">POB vs Invoice — Brand Lines</h4>
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

              {/* 4. Correction history — read-only. Corrections are made by
                  operations management (verification.manage), never by
                  verification agents on this screen. */}
              {correctionList.length > 0 && (
                <div className="vd-section">
                  <h4 className="vd-section-title">Correction History</h4>
                  <div className="correction-history">
                    {correctionList.map((c) => (
                      <div key={c.id} style={{ fontSize: 13, padding: '6px 0', borderBottom: '1px solid var(--border)' }}>
                        <strong>{c.field_name}</strong>: <span className="muted">{c.original_value}</span> → <strong>{c.corrected_value}</strong>
                        <span className="muted"> — {c.reason} ({c.agent_name}, {fmtDateTime(c.created_at)})</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 5. Pipeline Steps — collapsible */}
              {pipelineSteps.length > 0 && (
                <details className="vd-section" open={false}>
                  <summary className="vd-section-title" style={{ cursor: 'pointer' }}>Pipeline Steps ({pipelineSteps.length})</summary>
                  <div style={{ marginTop: 8 }}>
                    {pipelineSteps.map((step) => (
                      <div key={step.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0', fontSize: 13 }}>
                        <span>{step.step_status === 'passed' ? '✅' : step.step_status === 'failed' ? '❌' : '⏭️'}</span>
                        <span style={{ fontWeight: 500 }}>{step.step_name}</span>
                        <span className="muted">— {step.step_status}</span>
                      </div>
                    ))}
                  </div>
                </details>
              )}

              {/* 6. Audit trail */}
              {d.approvals?.length > 0 && (
                <div className="vd-section">
                  <h4 className="vd-section-title">Approval Workflow</h4>
                  <div className="timeline">
                    {d.approvals.map((a) => (
                      <div key={a.id} className="tl-item">
                        <span className="tl-dot" />
                        <div>
                          <strong>Step {a.step}: {a.step_name || a.status}</strong> <StatusBadge value={a.status} />
                          {a.role_name && <div className="muted">{roleLabel(a.role_name)}{a.comment ? ` — ${a.comment}` : ''}</div>}
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

      {/* Action Bar */}
      {canAct && (
        <div className="vd-action-bar">
          <div className="vd-action-left">
            <span className="vd-action-result">Decision: <strong>{decisionLabel}</strong></span>
            {failedChecks.length > 0 && <span className="vd-action-issues">{failedChecks.length} issue(s)</span>}
          </div>
          <div className="vd-action-right">
            {d.v_status === 'approved' && d.auto_verified ? (
              <>
                <button className="btn btn-amber" disabled={busy} onClick={() => setReOpening(true)}>
                  Re-open for review
                </button>
                <span className="vd-action-result" style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                  Auto-verified · {confidence}% confidence
                </span>
              </>
            ) : (
              <>
                {canAct && (
                  <Field label="" style={{ marginBottom: 0 }}>
                    <TextInput value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional note" className="vd-action-note" />
                  </Field>
                )}
                <button className="btn btn-danger" disabled={busy} onClick={() => setRejecting(true)}>Reject</button>
                <button className="btn btn-amber" disabled={busy} onClick={() => act(() => api(`/api/v1/verification/${vid}/duplicate`, {
                  method: 'POST', body: { reason: 'Marked duplicate during verification' },
                }), 'Marked as duplicate')}>Mark Duplicate</button>
                <button className="btn btn-amber" disabled={busy} onClick={() => setFlagging(true)}>Needs Review</button>
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

      {/* Re-open modal */}
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
          This POB was <strong>auto-approved</strong> by AI verification ({confidence}% confidence).
          Re-opening moves it back to the pending queue for manual review.
        </p>
        <Field label="Reason for re-opening" required>
          <TextArea rows={3} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Client reported discrepancy, need to re-verify details" />
        </Field>
      </Modal>

      {/* Needs Review modal */}
      <Modal open={flagging} title="Flag for further review" onClose={() => { setFlagging(false); setFlagReason(''); }}
        footer={<>
          <button className="btn" onClick={() => { setFlagging(false); setFlagReason(''); }}>Cancel</button>
          <button className="btn btn-amber" disabled={!flagReason.trim() || busy} onClick={() =>
            act(() => api(`/api/v1/verification/${vid}/flag_review`, { method: 'POST', body: { reason: flagReason } }),
            'Flagged for further review').then(() => { setFlagging(false); setFlagReason(''); })}>
            {busy ? 'Flagging…' : 'Flag for review'}
          </button>
        </>}>
        <div style={{ marginBottom: 16 }}>
          <strong>Flag POB #{d.pob_id} for further review?</strong>
          <span className="muted" style={{ marginLeft: 8 }}>{d.campaign_name} · {d.chemist_name}{d.shop_name ? ` (${d.shop_name})` : ''}</span>
        </div>
        <p style={{ margin: '0 0 12px', fontSize: 13 }}>
          This keeps the POB <strong>unresolved</strong> in the manual verification queue — no approval,
          rejection or gratification is created. The MR is notified that their POB needs further review.
        </p>
        <Field label="Reason / remark" required>
          <TextArea rows={3} value={flagReason} onChange={(e) => setFlagReason(e.target.value)}
            placeholder="e.g. Invoice legible but chemist details need a follow-up call" />
        </Field>
      </Modal>
    </div>
  );
}
