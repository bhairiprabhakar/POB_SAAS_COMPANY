// Verification Decision Workspace — shared report renderer.
//
// Both the agent's Verification detail and the MR's POB detail render the same
// numbered sections from the backend report (saas/verification_checks.py) so
// the two surfaces never disagree.
//
// Information hierarchy (decision → exceptions → verification → evidence):
//   1. Verification Decision card — overall status, pass/fail counts, action
//   2. Exceptions requiring attention — failed/warning checks at a glance
//   3. POB vs invoice comparison — field-level match table
//   4. Campaign eligibility — rules engine view
//   5. Verification checklist — categorized pass/fail with expandable detail
//   6. Extracted invoice data — compact grouped sections
//   7. AI extraction summary — engine, confidence, processing state
//
import { useState } from 'react';
import { fmtMoney } from './api';
import { Badge, DetailTable } from './ui';

/* ── Status primitives ──────────────────────────────────────────────────── */

export function StateBadge({ state }) {
  const map = {
    pass: { tone: 'green', label: 'PASS' },
    fail: { tone: 'red', label: 'FAIL' },
    warn: { tone: 'amber', label: 'WARNING' },
    manual: { tone: 'amber', label: 'MANUAL' },
    na: { tone: 'gray', label: 'NOT CHECKED' },
  };
  const m = map[state] || { tone: 'gray', label: state ? String(state).toUpperCase() : '—' };
  return <span className={`verdict-badge verdict-${m.tone}`}>{m.label}</span>;
}

export function VerificationStatusBadge({ status }) {
  const map = {
    verified: { tone: 'green', label: 'VERIFIED' },
    approved: { tone: 'green', label: 'VERIFIED' },
    paid: { tone: 'green', label: 'VERIFIED' },
    rejected: { tone: 'red', label: 'REJECTED' },
    duplicate: { tone: 'red', label: 'DUPLICATE' },
    extraction_failed: { tone: 'amber', label: 'EXTRACTION FAILED' },
    ready: { tone: 'blue', label: 'READY FOR VERIFICATION' },
    no_invoice: { tone: 'gray', label: 'AWAITING INVOICE' },
  };
  const m = map[status] || { tone: 'blue', label: String(status || '').replaceAll('_', ' ').toUpperCase() };
  return <span className={`verdict-badge verdict-lg verdict-${m.tone}`}>{m.label}</span>;
}

/* ── Helpers ────────────────────────────────────────────────────────────── */

function fmtOrDash(v) { return v != null && v !== '' ? fmtMoney(v) : '—'; }
function pct(v) { return v != null ? Math.round(v * 100) : null; }

/* ── 1. Verification Decision Card ──────────────────────────────────────── */

export function VerificationDecisionCard({ report, po = {} }) {
  if (!report) return null;
  const s = report.summary || {};
  const passed = s.passed || 0;
  const failed = s.failed || 0;
  const manual = s.manual || 0;
  const total = passed + failed + manual;
  const allPass = failed === 0 && manual === 0;
  const status = report.status || (allPass ? 'verified' : 'rejected');

  const failedChecks = (report.checklist || []).filter((c) => c.state === 'fail');
  const conf = report.extraction?.confidence != null ? pct(report.extraction.confidence) : null;
  const recommendedAction = allPass ? 'Approve — all checks passed'
    : failed > 0 ? `Reject — ${failed} check(s) failed`
    : `Review — ${manual} item(s) need manual review`;

  return (
    <div className={`vd-decision-card vd-decision-${allPass ? 'pass' : failed > 0 ? 'fail' : 'review'}`}>
      <div className="vd-decision-left">
        <div className="vd-decision-status">
          <VerificationStatusBadge status={status} />
          {total > 0 && (
            <span className="vd-decision-counts">
              {passed}/{total} checks passed
              {failed > 0 && <span className="vd-fail-count"> · {failed} failed</span>}
              {manual > 0 && <span className="vd-manual-count"> · {manual} review</span>}
            </span>
          )}
        </div>
        {failedChecks.length > 0 && (
          <div className="vd-decision-reasons">
            {failedChecks.slice(0, 3).map((c) => (
              <span key={c.key} className="vd-reason-pill">{c.label}</span>
            ))}
            {failedChecks.length > 3 && <span className="vd-reason-more">+{failedChecks.length - 3} more</span>}
          </div>
        )}
        <div className="vd-decision-action">
          <span className="vd-action-label">Recommended:</span>
          <strong>{recommendedAction}</strong>
        </div>
      </div>
      <div className="vd-decision-right">
        {po.pob_amount != null && (
          <div className="vd-kv"><span>POB Amount</span><strong>{fmtMoney(po.pob_amount)}</strong></div>
        )}
        {po.invoice_amount != null && (
          <div className="vd-kv"><span>Invoice Amount</span><strong>{fmtMoney(po.invoice_amount)}</strong></div>
        )}
        {po.quantity != null && (
          <div className="vd-kv"><span>Qty</span><strong>{po.quantity}</strong></div>
        )}
        {po.invoice_date && (
          <div className="vd-kv"><span>Invoice Date</span><strong>{po.invoice_date}</strong></div>
        )}
        {conf != null && (
          <div className="vd-kv">
            <span>AI Confidence</span>
            <strong className={conf >= 90 ? 'text-green' : conf >= 60 ? 'text-amber' : 'text-red'}>{conf}%</strong>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── 2. Exceptions Requiring Attention ──────────────────────────────────── */

export function ExceptionsSection({ report }) {
  if (!report) return null;
  const checks = report.checklist || [];
  const failed = checks.filter((c) => c.state === 'fail');
  const warnings = checks.filter((c) => c.state === 'warn' || c.state === 'manual');
  const passed = checks.filter((c) => c.state === 'pass');

  if (failed.length === 0 && warnings.length === 0) return null;

  return (
    <div className="vd-section">
      <h4 className="vd-section-title">
        Exceptions Requiring Attention
        <span className="vd-exception-count">{failed.length} failed{warnings.length > 0 ? ` · ${warnings.length} warning` : ''}</span>
      </h4>
      {failed.map((c) => (
        <div key={c.key} className="vd-exception-card vd-exception-fail">
          <span className="vd-exception-icon">✕</span>
          <div className="vd-exception-body">
            <strong>{c.label}</strong>
            {c.detail && <p>{c.detail}</p>}
          </div>
        </div>
      ))}
      {warnings.map((c) => (
        <div key={c.key} className="vd-exception-card vd-exception-warn">
          <span className="vd-exception-icon">⚠</span>
          <div className="vd-exception-body">
            <strong>{c.label}</strong>
            {c.detail && <p>{c.detail}</p>}
          </div>
        </div>
      ))}
      {passed.length > 0 && (
        <details className="vd-passed-collapsed">
          <summary>{passed.length} checks passed — click to expand</summary>
          <div className="vd-passed-list">
            {passed.map((c) => (
              <div key={c.key} className="vd-exception-card vd-exception-pass">
                <span className="vd-exception-icon">✓</span>
                <span className="vd-exception-label">{c.label}</span>
                {c.detail && <span className="vd-exception-detail">{c.detail}</span>}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

/* ── 3. POB vs Invoice Comparison ───────────────────────────────────────── */

export function PobVsInvoice({ report }) {
  if (!report) return null;
  const fields = [
    { label: 'Product', ...report.product },
    { label: 'Quantity', ...report.quantity },
    { label: 'PTR', submitted: fmtOrDash(report.ptr?.submitted), invoice: fmtOrDash(report.ptr?.invoice), state: report.ptr?.state },
    { label: 'POB Amount', submitted: fmtOrDash(report.amount?.submitted), invoice: fmtOrDash(report.amount?.invoice), state: report.amount?.state },
    { label: 'Chemist', ...report.chemist },
  ];
  const allPass = fields.every((f) => f.state === 'pass');

  return (
    <div className="vd-section">
      <h4 className="vd-section-title">POB vs Invoice Verification</h4>
      <p className="vd-section-subtitle">
        Each submitted value compared against the AI-extracted invoice line — not the grand total.
      </p>
      <div className="vd-compare-table">
        <div className="vd-compare-head">
          <span>Field</span><span>Submitted POB</span><span>Invoice</span><span>Result</span>
        </div>
        {fields.map((f) => (
          <div key={f.label} className={`vd-compare-row vd-compare-${f.state || 'na'}`}>
            <span className="vd-compare-field">{f.label}</span>
            <span className="vd-compare-pob">{f.submitted ?? '—'}</span>
            <span className="vd-compare-inv">{f.invoice ?? '—'}</span>
            <span className="vd-compare-result"><StateBadge state={f.state} /></span>
          </div>
        ))}
      </div>
      {report.amount?.note && <p className="vd-note">{report.amount.note}</p>}
    </div>
  );
}

/* ── 4. Campaign Eligibility ────────────────────────────────────────────── */

export function CampaignEligibility({ report }) {
  const rules = report?.campaign?.rules || [];
  if (!rules.length) return null;
  const passed = rules.filter((r) => r.ok).length;
  const total = rules.length;

  return (
    <div className="vd-section">
      <h4 className="vd-section-title">
        Campaign Eligibility
        <span className="vd-eligibility-summary">{passed}/{total} rules satisfied</span>
      </h4>
      <div className="vd-rules-table">
        <div className="vd-rules-head">
          <span>Rule</span><span>Requirement</span><span>Submitted</span><span>Result</span>
        </div>
        {rules.map((r) => (
          <div key={r.label} className={`vd-rules-row ${r.ok ? 'vd-rule-pass' : 'vd-rule-fail'}`}>
            <span className="vd-rule-label">{r.label}</span>
            <span className="vd-rule-req">{r.requirement}</span>
            <span className="vd-rule-sub">{r.submitted}</span>
            <span className="vd-rule-result"><StateBadge state={r.ok ? 'pass' : 'fail'} /></span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── 5. Verification Checklist (categorized) ───────────────────────────── */

export function VerificationChecklist({ report }) {
  const checks = report?.checklist || [];
  if (!checks.length) return null;
  const passed = checks.filter((c) => c.state === 'pass');
  const failed = checks.filter((c) => c.state === 'fail');
  const other = checks.filter((c) => c.state !== 'pass' && c.state !== 'fail');

  return (
    <div className="vd-section">
      <h4 className="vd-section-title">
        Verification Checklist
        <span className="vd-checklist-summary">
          <span className="vd-pass-count">{passed.length} passed</span>
          {failed.length > 0 && <span className="vd-fail-count">{failed.length} failed</span>}
          {other.length > 0 && <span className="vd-manual-count">{other.length} other</span>}
        </span>
      </h4>

      {failed.length > 0 && (
        <div className="vd-checklist-group">
          {failed.map((c) => (
            <CheckItem key={c.key} check={c} />
          ))}
        </div>
      )}
      {other.length > 0 && (
        <div className="vd-checklist-group">
          {other.map((c) => (
            <CheckItem key={c.key} check={c} />
          ))}
        </div>
      )}
      {passed.length > 0 && (
        <details className="vd-passed-collapsed">
          <summary>{passed.length} checks passed — click to expand</summary>
          <div className="vd-checklist-group">
            {passed.map((c) => (
              <CheckItem key={c.key} check={c} />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

function CheckItem({ check }) {
  const [expanded, setExpanded] = useState(false);
  const isFailed = check.state === 'fail';
  return (
    <div className={`vd-check-item vd-check-${check.state || 'na'}`}>
      <div className="vd-check-head" onClick={() => isFailed && setExpanded(!expanded)}>
        <StateBadge state={check.state} />
        <strong className="vd-check-label">{check.label}</strong>
        {isFailed && <span className="vd-check-expand">{expanded ? '−' : '+'}</span>}
      </div>
      {check.detail && !isFailed && <p className="vd-check-detail">{check.detail}</p>}
      {isFailed && expanded && check.detail && (
        <div className="vd-check-expanded">
          <p>{check.detail}</p>
        </div>
      )}
    </div>
  );
}

/* ── 6. Extracted Invoice Data ──────────────────────────────────────────── */

export function ExtractedInvoiceData({ report }) {
  const ext = report?.extraction;
  const f = (ext && ext.fields) || {};
  if (!ext || Object.keys(f).length === 0) return null;

  const inv = f.invoice || {};
  const seller = f.seller || {};
  const buyer = f.buyer || {};
  const tax = f.tax || {};
  const items = Array.isArray(f.items) ? f.items : [];
  const invNo = f.invoice_number ?? inv.number;
  const invDate = f.invoice_date ?? inv.date;
  const invType = f.invoice_type ?? inv.type;
  const invTotal = f.invoice_amount ?? inv.total;
  const currency = f.currency ?? inv.currency ?? 'INR';

  return (
    <div className="vd-section">
      <h4 className="vd-section-title">Extracted Invoice Data</h4>
      <div className="vd-extraction-groups">
        <div className="vd-ext-group">
          <h5 className="vd-ext-group-title">Invoice</h5>
          <div className="vd-ext-kv">
            <span>Number</span><strong>{invNo ?? '—'}</strong>
          </div>
          <div className="vd-ext-kv"><span>Date</span><strong>{invDate ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>Type</span><strong>{invType ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>Total</span><strong>{fmtOrDash(invTotal)}</strong></div>
          <div className="vd-ext-kv"><span>Currency</span><strong>{currency ?? '—'}</strong></div>
        </div>
        <div className="vd-ext-group">
          <h5 className="vd-ext-group-title">Seller / Distributor</h5>
          <div className="vd-ext-kv"><span>Name</span><strong>{seller.name ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>GSTIN</span><strong>{seller.gstin ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>Address</span><strong>{seller.address ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>Phone</span><strong>{seller.phone ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>License</span><strong>{seller.license_no ?? '—'}</strong></div>
        </div>
        <div className="vd-ext-group">
          <h5 className="vd-ext-group-title">Buyer / Chemist</h5>
          <div className="vd-ext-kv"><span>Name</span><strong>{buyer.name ?? f.chemist_name ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>Code</span><strong>{buyer.code ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>GSTIN</span><strong>{buyer.gstin ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>Address</span><strong>{buyer.address ?? '—'}</strong></div>
          <div className="vd-ext-kv"><span>Phone</span><strong>{buyer.phone ?? '—'}</strong></div>
        </div>
        {Object.keys(tax).length > 0 && (
          <div className="vd-ext-group">
            <h5 className="vd-ext-group-title">Tax Summary</h5>
            <div className="vd-ext-kv"><span>Taxable</span><strong>{fmtOrDash(tax.taxable_amount)}</strong></div>
            <div className="vd-ext-kv"><span>CGST</span><strong>{fmtOrDash(tax.cgst)}</strong></div>
            <div className="vd-ext-kv"><span>SGST</span><strong>{fmtOrDash(tax.sgst)}</strong></div>
            <div className="vd-ext-kv"><span>IGST</span><strong>{fmtOrDash(tax.igst)}</strong></div>
          </div>
        )}
      </div>
      {items.length > 0 && (
        <div className="vd-ext-products">
          <h5 className="vd-section-subtitle">Extracted Product Lines</h5>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Product</th><th>SKU</th><th>Batch</th><th>Expiry</th>
                  <th>Qty</th><th>Free</th><th>PTR</th><th>MRP</th>
                  <th>Disc.</th><th>Tax</th><th>Amount</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it, i) => (
                  <tr key={i}>
                    <td><strong>{it.description}</strong></td>
                    <td>{it.sku || '—'}</td>
                    <td>{it.batch || '—'}</td>
                    <td>{it.expiry || '—'}</td>
                    <td>{it.qty ?? '—'}</td>
                    <td>{it.free_qty ?? 0}</td>
                    <td>{it.ptr != null ? fmtMoney(it.ptr) : (it.rate != null ? fmtMoney(it.rate) : '—')}</td>
                    <td>{it.mrp != null ? fmtMoney(it.mrp) : '—'}</td>
                    <td>{it.discount != null ? fmtMoney(it.discount) : '—'}</td>
                    <td>{it.tax != null ? fmtMoney(it.tax) : '—'}</td>
                    <td><strong>{fmtOrDash(it.amount)}</strong></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── 7. AI Extraction Summary ───────────────────────────────────────────── */

export function AiExtractionSummary({ report }) {
  const ext = report?.extraction;
  if (!ext) return null;
  const failed = report?.status === 'extraction_failed';
  const conf = ext.confidence != null ? pct(ext.confidence) : null;
  const fieldCount = ext.fields ? Object.keys(ext.fields).length : 0;
  const itemCount = Array.isArray(ext.fields?.items) ? ext.fields.items.length : 0;

  const steps = ['Uploaded', 'Processing', 'Extracted', 'Validated'];
  const currentStep = failed ? 1 : 3;

  return (
    <div className="vd-section">
      <h4 className="vd-section-title">AI Invoice Extraction</h4>
      <div className="vd-ai-card">
        <div className="vd-ai-header">
          <span className={`vd-ai-status ${failed ? 'vd-ai-failed' : 'vd-ai-done'}`}>
            {failed ? 'Extraction Failed' : 'Completed'}
          </span>
          {conf != null && (
            <span className={`vd-ai-confidence ${conf >= 90 ? 'high' : conf >= 60 ? 'mid' : 'low'}`}>
              {conf}% confidence
            </span>
          )}
        </div>
        <div className="vd-ai-timeline">
          {steps.map((step, i) => (
            <span key={step} className={`vd-timeline-step ${i <= currentStep ? 'active' : ''} ${i === currentStep ? 'current' : ''}`}>
              <span className="vd-timeline-dot" />
              <span className="vd-timeline-label">{step}</span>
            </span>
          ))}
        </div>
        <div className="vd-ai-meta">
          {ext.engine && <div className="vd-kv"><span>Engine</span><strong>{ext.engine}</strong></div>}
          {fieldCount > 0 && <div className="vd-kv"><span>Fields extracted</span><strong>{fieldCount}</strong></div>}
          {itemCount > 0 && <div className="vd-kv"><span>Products detected</span><strong>{itemCount}</strong></div>}
        </div>
        {failed && (
          <div className="vd-ai-failed-note">
            <p>No invoice data could be extracted. Manual verification required.</p>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Legacy: AutomationDecision (kept for backward compat) ──────────────── */

export function AutomationDecision({ report }) {
  const a = report?.automation;
  if (!a || !a.enabled) return null;
  const conf = a.confidence != null ? pct(a.confidence) : null;
  const verdictOk = a.verdict === true;
  const fieldRows = a.fields || [];
  return (
    <div className="vd-section">
      <h4 className="vd-section-title">Automation Decision</h4>
      <div className="vd-ai-card">
        <div className="vd-ai-header">
          <span className={`vd-ai-status ${verdictOk ? 'vd-ai-done' : 'vd-ai-failed'}`}>
            {verdictOk ? 'Auto-verified by AI' : 'Flagged for manual review'}
          </span>
          {conf != null && (
            <span className={`vd-ai-confidence ${conf >= 90 ? 'high' : conf >= 60 ? 'mid' : 'low'}`}>
              {conf}% confidence · threshold {Math.round(a.threshold * 100)}%
            </span>
          )}
        </div>
        {!verdictOk && a.mismatches?.length > 0 && (
          <div className="vd-ai-mismatches">
            <strong>Field mismatches:</strong> {a.mismatches.join(', ')}
          </div>
        )}
        <div className="vd-compare-table" style={{ marginTop: 8 }}>
          <div className="vd-compare-head">
            <span>Field</span><span>Submitted POB</span><span>Invoice</span><span>Result</span>
          </div>
          {fieldRows.map((f) => (
            <div key={f.label} className={`vd-compare-row vd-compare-${f.state || 'na'}`}>
              <span className="vd-compare-field">{f.label}</span>
              <span className="vd-compare-pob">{f.submitted}</span>
              <span className="vd-compare-inv">{f.invoice}</span>
              <span className="vd-compare-result"><StateBadge state={f.state} /></span>
            </div>
          ))}
          {!fieldRows.length && (
            <div className="vd-compare-row"><span className="vd-compare-field muted">{a.message}</span></div>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Combined report (new hierarchy) ────────────────────────────────────── */

export function InvoiceProofReport({ report, po }) {
  if (!report) return null;
  return (
    <>
      <VerificationDecisionCard report={report} po={po} />
      <ExceptionsSection report={report} />
      <PobVsInvoice report={report} />
      <CampaignEligibility report={report} />
      <VerificationChecklist report={report} />
      <AutomationDecision report={report} />
      <ExtractedInvoiceData report={report} />
      <AiExtractionSummary report={report} />
    </>
  );
}

/* ── Simplified invoice summary (admin / MR / non-agent roles) ──────────── */

export function InvoiceSummaryReport({ report, po, lines = [] }) {
  if (!report) {
    return (
      <div className="vd-section">
        <p className="muted">No verification report available yet.</p>
      </div>
    );
  }

  const ext = report?.extraction;
  const f = (ext && ext.fields) || {};
  const inv = f.invoice || {};
  const seller = f.seller || {};
  const buyer = f.buyer || {};
  const items = Array.isArray(f.items) ? f.items : [];
  const invNo = f.invoice_number ?? inv.number;
  const invDate = f.invoice_date ?? inv.date;
  const invTotal = f.invoice_amount ?? inv.total;
  const conf = ext?.confidence != null ? Math.round(ext.confidence * 100) : null;

  const s = report.summary || {};
  const passed = s.passed || 0;
  const failed = s.failed || 0;
  const allPass = failed === 0;

  const failedChecks = (report.checklist || []).filter((c) => c.state === 'fail');
  const statusLabel = allPass ? 'VERIFIED' : `${failed} check(s) failed`;

  return (
    <div className="vd-section">
      <h4 className="vd-section-title">Invoice Verification</h4>

      {/* Status banner */}
      <div className={`vd-decision-card vd-decision-${allPass ? 'pass' : 'fail'}`} style={{ marginBottom: 12 }}>
        <div className="vd-decision-left">
          <div className="vd-decision-status">
            <span className={`verdict-badge verdict-lg ${allPass ? 'verdict-green' : 'verdict-red'}`}>
              {statusLabel}
            </span>
            {conf != null && (
              <span className="vd-decision-counts">AI Confidence: {conf}%</span>
            )}
          </div>
          {failedChecks.length > 0 && (
            <div className="vd-decision-reasons">
              {failedChecks.map((c) => (
                <span key={c.key} className="vd-reason-pill">{c.label}</span>
              ))}
            </div>
          )}
        </div>
        <div className="vd-decision-right">
          {po?.pob_amount != null && (
            <div className="vd-kv"><span>POB Amount</span><strong>{fmtMoney(po.pob_amount)}</strong></div>
          )}
          {po?.invoice_amount != null && (
            <div className="vd-kv"><span>Invoice Amount</span><strong>{fmtMoney(po.invoice_amount)}</strong></div>
          )}
        </div>
      </div>

      {/* Invoice details */}
      <div className="vd-ext-group" style={{ marginBottom: 10 }}>
        <h5 className="vd-ext-group-title">Invoice Details</h5>
        <div className="vd-ext-kv"><span>Number</span><strong>{invNo ?? '—'}</strong></div>
        <div className="vd-ext-kv"><span>Date</span><strong>{invDate ?? '—'}</strong></div>
        <div className="vd-ext-kv"><span>Total</span><strong>{fmtOrDash(invTotal)}</strong></div>
        {seller.name && <div className="vd-ext-kv"><span>Seller</span><strong>{seller.name}</strong></div>}
        {(buyer.name || f.chemist_name) && <div className="vd-ext-kv"><span>Buyer</span><strong>{buyer.name ?? f.chemist_name}</strong></div>}
      </div>

      {/* Campaign products — clear multi-product support */}
      {lines.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <h5 className="vd-section-subtitle">Campaign Products — Invoice Match</h5>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Product</th><th>SKU</th><th>Qty</th><th>PTR</th><th>POB Value</th><th>Invoice Match</th>
                </tr>
              </thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.id}>
                    <td>
                      <strong>{l.product_name}</strong>
                      {l.brand_name && String(l.brand_name).toLowerCase() !== String(l.product_name).toLowerCase()
                        ? <small className="muted"> — {l.brand_name}</small> : null}
                    </td>
                    <td>{l.sku || '—'}</td>
                    <td>{l.quantity}</td>
                    <td>{fmtMoney(l.ptr)}</td>
                    <td><strong>{fmtMoney(l.pob_amount)}</strong></td>
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
        </div>
      )}

      {/* Other invoice products (not campaign products) */}
      {items.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <h5 className="vd-section-subtitle">All Invoice Products</h5>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr><th>Product</th><th>SKU</th><th>Qty</th><th>Rate</th><th>Amount</th><th>Campaign Product</th></tr>
              </thead>
              <tbody>
                {items.map((it, i) => {
                  const isCampaign = lines.some((l) =>
                    l.product_name && it.description &&
                    l.product_name.toLowerCase() === it.description.toLowerCase()
                  );
                  return (
                    <tr key={i}>
                      <td><strong>{it.description}</strong></td>
                      <td>{it.sku || '—'}</td>
                      <td>{it.qty ?? '—'}</td>
                      <td>{it.ptr != null ? fmtMoney(it.ptr) : (it.rate != null ? fmtMoney(it.rate) : '—')}</td>
                      <td><strong>{fmtOrDash(it.amount)}</strong></td>
                      <td>
                        {isCampaign
                          ? <span className="verdict-badge verdict-green verdict-sm">CAMPAIGN</span>
                          : <span className="verdict-badge verdict-gray verdict-sm">OTHER</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
