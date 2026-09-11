import { useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import PobDetailUser from './PobDetailUser';
import { api, downloadFile, fmtDateTime, fmtMoney, getSession, uploadFile } from '../../api';
import {
  Badge, DetailHero, DetailTable, EmptyState, ErrorBox, Field, Modal, PageHeader, ProofPane, SearchBox, Spinner,
  SplitDetail, StatusBadge, Table, Tabs, TextInput, toast, useAsync, useFileUrl,
} from '../../ui';
import { InvoiceProofReport, InvoiceSummaryReport, VerificationStatusBadge } from '../../InvoiceProofReport';

const POB_TABS = ['all', 'pending_verification', 'verified', 'rejected', 'duplicate', 'needs_review'];

export default function Pobs({ mine }) {
  const [params] = useSearchParams();
  const [status, setStatus] = useState(() => {
    const t = params.get('status');
    return POB_TABS.includes(t) ? t : 'all';
  });
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState(null);
  const [exportOpen, setExportOpen] = useState(false);
  const endpoint = mine ? '/api/v1/pob/mine' : '/api/v1/pob';
  const { data, loading, error, run } = useAsync(() =>
    api(`${endpoint}${!mine && status !== 'all' ? `?status=${status}` : ''}`), [endpoint, status]);

  const session = getSession();
  const role = (session?.user?.role || '').toLowerCase();
  const isVerifier = role === 'verification_agent' || role === 'verifier';
  const isAdmin = role === 'company_admin' || role === 'division_admin';

  const rows = useMemo(() => {
    const items = data?.items || [];
    if (!q) return items;
    const n = q.toLowerCase();
    return items.filter((r) =>
      r.chemist_name?.toLowerCase().includes(n) ||
      r.invoice_number?.toLowerCase().includes(n) ||
      r.campaign_name?.toLowerCase().includes(n) ||
      (r.submitted_by || r.full_name || '').toLowerCase().includes(n));
  }, [data, q]);

  if (loading) return <Spinner label="Loading POBs…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  // ── Role-based column sets ──
  const baseCols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'invoice_number', label: 'Invoice', render: (r) => r.invoice_number || '—' },
    { key: 'invoice_date', label: 'Date', render: (r) => r.invoice_date || '—' },
    { key: 'chemist_name', label: 'Chemist', render: (r) => r.chemist_name || '—' },
    { key: 'campaign_name', label: 'Campaign' },
    { key: 'product_name', label: 'Product' },
    { key: 'pob_amount', label: 'POB value', render: (r) => fmtMoney(r.pob_amount) },
    { key: 'invoice_amount', label: 'Invoice value', render: (r) => r.invoice_amount ? fmtMoney(r.invoice_amount) : '—' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.verification_status || r.status} /> },
    { key: 'created_at', label: 'Submitted', render: (r) => fmtDateTime(r.created_at) },
    { key: 'proof_lag_days', label: 'Proof lag', render: (r) =>
      r.proof_lag_days === null || r.proof_lag_days === undefined
        ? <span className="muted">—</span>
        : <span title={`Invoice proof attached ${r.proof_lag_days} day(s) after the POB submission`}><strong>{r.proof_lag_days}</strong>d</span> },
  ];
  const hierarchyCols = [
    { key: 'submitted_by', label: 'Submitted by', render: (r) => <span><strong>{r.submitted_by || '—'}</strong><br /><small className="muted">@{r.submitted_username || ''}</small></span> },
    { key: 'submitter_level', label: 'Level', render: (r) => r.submitter_level || '—' },
    { key: 'reports_to_name', label: 'Reports to', render: (r) => r.reports_to_name || '—' },
  ];

  // Admin columns: management-oriented with rejection reason + verified by
  const adminCols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    ...hierarchyCols,
    { key: 'chemist_name', label: 'Chemist', render: (r) => <span><strong>{r.chemist_name || '—'}</strong>{r.chemist_city ? <><br /><small className="muted">{r.chemist_city}</small></> : null}</span> },
    { key: 'campaign_name', label: 'Campaign' },
    { key: 'product_name', label: 'Product' },
    { key: 'quantity', label: 'Qty', render: (r) => r.quantity || '—' },
    { key: 'pob_amount', label: 'POB value', render: (r) => fmtMoney(r.pob_amount) },
    { key: 'invoice_amount', label: 'Invoice value', render: (r) => r.invoice_amount ? fmtMoney(r.invoice_amount) : '—' },
    { key: 'invoice_number', label: 'Invoice', render: (r) => r.invoice_number || '—' },
    { key: 'invoice_date', label: 'Inv. date', render: (r) => r.invoice_date || '—' },
    { key: 'status', label: 'Verification', render: (r) => <StatusBadge value={r.verification_status || r.status} /> },
    { key: 'rejection_reason', label: 'Rejection', render: (r) => r.rejection_reason ? <span title={r.rejection_reason} style={{ color: 'var(--red)', fontSize: 13 }}>{r.rejection_reason.length > 30 ? r.rejection_reason.slice(0, 30) + '…' : r.rejection_reason}</span> : <span className="muted">—</span> },
    { key: 'verified_by', label: 'Verified by', render: (r) => r.verified_by_name || <span className="muted">—</span> },
    { key: 'created_at', label: 'Submitted', render: (r) => fmtDateTime(r.created_at) },
  ];

  // Verifier: base + hierarchy columns (existing behavior)
  // Campaign user (mine=true): simplified columns — ID, Invoice, Date, Campaign, Chemist, Value, Status
  const campaignCols = mine ? [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'invoice_number', label: 'Invoice', render: (r) => r.invoice_number || '—' },
    { key: 'invoice_date', label: 'Date', render: (r) => r.invoice_date || '—' },
    { key: 'campaign_name', label: 'Campaign' },
    { key: 'chemist_name', label: 'Chemist', render: (r) => r.chemist_name || '—' },
    { key: 'pob_amount', label: 'POB value', render: (r) => fmtMoney(r.pob_amount) },
    { key: 'invoice_amount', label: 'Invoice value', render: (r) => r.invoice_amount ? fmtMoney(r.invoice_amount) : '—' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.verification_status || r.status} /> },
    { key: 'created_at', label: 'Submitted', render: (r) => fmtDateTime(r.created_at) },
  ] : null;

  let cols;
  if (mine && campaignCols) {
    cols = campaignCols;
  } else if (isAdmin) {
    cols = adminCols;
  } else if (isVerifier) {
    cols = [...baseCols.slice(0, 1), ...hierarchyCols, ...baseCols.slice(1)];
  } else {
    cols = [...baseCols.slice(0, 1), ...hierarchyCols, ...baseCols.slice(1)];
  }

  if (selected) {
    return (
      <div>
        <PobDetail pob={selected} onBack={() => setSelected(null)} />
        {exportOpen && !mine && (
          <ExportPobModal status={status} q={q} onClose={() => setExportOpen(false)} />
        )}
      </div>
    );
  }

  return (
    <div>
      <PageHeader title={mine ? 'My POBs' : 'POB Records'}
        subtitle={mine ? 'Your submissions and verification status' : 'All POB activities across the company · hierarchy reporting included'}
        actions={mine ? <SearchBox value={q} onChange={setQ} placeholder="Search chemist / invoice…" /> : <>
          <SearchBox value={q} onChange={setQ} placeholder="Search chemist / invoice / name…" />
          <button className="btn" onClick={() => setExportOpen(true)}>Export Excel</button>
        </>} />
      {!mine && (
        <Tabs items={[
          { value: 'all', label: 'All' },
          { value: 'pending_verification', label: 'Pending' },
          { value: 'verified', label: 'Verified' },
          { value: 'rejected', label: 'Rejected' },
          { value: 'duplicate', label: 'Duplicate' },
          { value: 'needs_review', label: 'Needs review' },
        ]} active={status} onChange={setStatus} />
      )}
      <Table cols={cols} rows={rows} keyOf={(r) => r.id} onRowClick={(r) => setSelected(r)}
        empty={mine ? 'No POBs submitted yet' : 'No POB records'} />
      {exportOpen && !mine && (
        <ExportPobModal status={status} q={q} onClose={() => setExportOpen(false)} />
      )}
    </div>
  );
}

function ExportPobModal({ status, q, onClose }) {
  const [st, setSt] = useState(status);
  const [hier, setHier] = useState(true);
  const [busy, setBusy] = useState(false);

  const run = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const p = new URLSearchParams();
      if (st && st !== 'all') p.set('status', st);
      if (q) p.set('q', q);
      p.set('include_hierarchy', hier ? 1 : 0);
      await downloadFile(`/api/v1/pob/export?${p.toString()}`, 'pob_records.xlsx');
      toast('Export downloaded', 'success');
      onClose();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  return (
    <Modal open title="Export POB Records" onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="export-form" disabled={busy}>{busy ? 'Exporting…' : 'Export Excel'}</button>
      </>}>
      <form id="export-form" onSubmit={run}>
        <div className="grid-2">
          <Field label="Status filter">
            <select className="input" value={st} onChange={(e) => setSt(e.target.value)}>
              <option value="all">All statuses</option>
              <option value="pending_verification">Pending</option>
              <option value="verified">Verified</option>
              <option value="rejected">Rejected</option>
              <option value="duplicate">Duplicate</option>
              <option value="needs_review">Needs review</option>
            </select>
          </Field>
          <label className="check" style={{ alignSelf: 'center', marginTop: 22 }}>
            <input type="checkbox" checked={hier} onChange={(e) => setHier(e.target.checked)} />
            <strong>Include reporting hierarchy</strong>
            <span className="muted"> — submitter level, manager and full reporting chain columns.</span>
          </label>
        </div>
        {q && <p className="muted" style={{ marginTop: 8 }}>Search filter applied: “{q}” — only matching records will be exported.</p>}
      </form>
    </Modal>
  );
}

function PobDetail({ pob, onBack }) {
  const { data, loading, error, run: reload } = useAsync(() => api(`/api/v1/pob/${pob.id}`));
  const invoiceUrl = useFileUrl(data?.invoice_path);
  const lines = data?.lines || [];
  const history = data?.history || [];
  const session = getSession();
  const role = (session?.user?.role || '').toLowerCase();
  const isVerifier = role === 'verification_agent' || role === 'verifier';
  const isAdmin = role === 'company_admin' || role === 'division_admin';
  const statusTone = (s) => ({
    verified: 'green', approved: 'green', paid: 'green', completed: 'green',
    pending: 'amber', pending_verification: 'amber', needs_review: 'amber', submitted: 'amber',
    rejected: 'red', duplicate: 'red', draft: 'gray', inactive: 'gray',
  }[s] || 'blue');

  if (loading) return <div><PageHeader title={`POB #${pob.id}`} actions={<button className="btn" onClick={onBack}>← Back</button>} /><Spinner /></div>;
  if (error) return <div><PageHeader title={`POB #${pob.id}`} actions={<button className="btn" onClick={onBack}>← Back</button>} /><ErrorBox error={error} onRetry={reload} /></div>;

  // Campaign users get a simplified, business-friendly detail view
  if (!isAdmin && !isVerifier) {
    return <PobDetailUser pobId={pob.id} onBack={onBack} />;
  }

  const status = data.verification_status || data.status;
  const isRejected = status === 'rejected';
  const rejectEntry = [...history].reverse().find((h) => h.action === 'rejected');
  const report = data.report || null;

  // ── Admin: summary drawer with progressive disclosure ──
  if (isAdmin) {
    return (
      <div>
        <PageHeader title={`POB #${pob.id} — ${data.campaign_name || ''}`}
          subtitle={`${data.product_name || ''} · ${data.chemist_name || ''}`}
          actions={<button className="btn" onClick={onBack}>← Back to POB Management</button>} />

        <EligibilityCard pobId={pob.id} />

        <SplitDetail
          left={
            <>
              <h4 className="section-title">Invoice proof</h4>
              <ProofPane url={invoiceUrl} name={data.invoice_original_name} hint="Uploaded invoice proof"
                empty={data.invoice_number ? 'Invoice proof not yet attached' : 'No invoice for this POB'} />
              {lines.length > 1 && (
                <div className="card" style={{ marginTop: 12 }}>
                  <h4 className="section-title">All products (same visit)</h4>
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead><tr>
                        <th>Brand / Product</th><th>Qty</th><th>PTR</th><th>POB value</th>
                      </tr></thead>
                      <tbody>
                        {lines.map((l) => (
                          <tr key={l.id} className={l.id === data.id ? 'row-current' : ''}>
                            <td>
                              <strong>{l.product_name}</strong>
                              {l.id === data.id && <Badge tone="blue">this line</Badge>}
                            </td>
                            <td>{l.quantity}</td>
                            <td>{fmtMoney(l.ptr)}</td>
                            <td><strong>{fmtMoney(l.pob_amount)}</strong></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </>
          }
          right={
            <div>
              <DetailHero
                tone={statusTone(status)}
                title={data.campaign_name || `POB #${data.id}`}
                subtitle={`${data.product_name || 'Product'} · ${data.chemist_name || 'Chemist'}${data.shop_name ? ` (${data.shop_name})` : ''}`}
                badge={<VerificationStatusBadge status={report?.status || status} />}
                primaryLabel="POB amount"
                primary={fmtMoney(data.pob_amount)}
                secondary={`Invoice ${fmtMoney(data.invoice_amount)} · Qty ${data.quantity}`}
                facts={[
                  ['Chemist', `${data.chemist_name || '—'}${data.shop_name ? ` (${data.shop_name})` : ''}${data.chemist_city ? ` · ${data.chemist_city}` : ''}`],
                  ['Submitted by', data.submitted_by],
                  ['Level', data.submitter_level || '—'],
                  ['Reports to', data.reports_to_name || '—'],
                  ['Invoice', `${data.invoice_number || '—'}${data.invoice_date ? ` · ${data.invoice_date}` : ''}`],
                  ['PTR', fmtMoney(data.ptr)],
                  ['Scheme', data.scheme_type || '—'],
                  ['Proof lag', data.proof_lag_days === null || data.proof_lag_days === undefined
                    ? 'Not attached yet'
                    : `${data.proof_lag_days} day(s) after submission`],
                ]}
              />

              {isRejected && rejectEntry && (
                <div className="decision-box decision-reject">
                  <span className="decision-icon">✕</span>
                  <div>
                    <strong>Rejection reason</strong>
                    <p style={{ margin: '4px 0 0' }}>{rejectEntry.reason || 'No reason provided'}</p>
                    {rejectEntry.verifier_name && (
                      <div className="muted">By {rejectEntry.verifier_name}{rejectEntry.created_at ? ` · ${fmtDateTime(rejectEntry.created_at)}` : ''}</div>
                    )}
                  </div>
                </div>
              )}

              <div className="card" style={{ marginTop: 12 }}>
                <h4 className="section-title">Verification summary</h4>
                <DetailTable rows={[
                  ['Verification status', <StatusBadge value={report?.status || status} />],
                  ['Total checks', report?.total ?? '—'],
                  ['Passes', report?.pass ?? '—'],
                  ['Failures', report?.fail ?? '—'],
                  ['Auto-verified', report?.auto_verified ? 'Yes' : 'No'],
                ]} />
              </div>

              {history.length > 0 && (
                <div className="timeline" style={{ marginTop: 16 }}>
                  <h4>Activity history</h4>
                  {[...history].reverse().map((h) => (
                    <div key={h.id} className="tl-item">
                      <span className="tl-dot" />
                      <div>
                        <strong>{h.action}</strong> <span className="muted">{fmtDateTime(h.created_at)}</span>
                        {h.reason && <div className="muted">{h.reason}</div>}
                        {h.verifier_name && <div className="muted">by {h.verifier_name}</div>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          }
        />
      </div>
    );
  }

  // ── Campaign user: simplified detail with actionable correction ──
  if (!isVerifier) {
    return (
      <div>
        <PageHeader title={`POB #${pob.id}`}
          subtitle={data.campaign_name || ''}
          actions={<button className="btn" onClick={onBack}>← Back to My POBs</button>} />

        {isRejected && rejectEntry && (
          <div className="decision-box decision-reject" style={{ marginBottom: 12 }}>
            <span className="decision-icon">✕</span>
            <div>
              <strong>Your POB was rejected</strong>
              <p style={{ margin: '4px 0 0' }}>{rejectEntry.reason || 'No reason provided'}</p>
              {rejectEntry.verifier_name && (
                <div className="muted">By {rejectEntry.verifier_name}{rejectEntry.created_at ? ` · ${fmtDateTime(rejectEntry.created_at)}` : ''}</div>
              )}
            </div>
          </div>
        )}

        <EligibilityCard pobId={pob.id} />

        <SplitDetail
          left={
            <>
              <h4 className="section-title">Invoice proof</h4>
              <ProofPane url={invoiceUrl} name={data.invoice_original_name} hint="Uploaded invoice"
                empty={data.invoice_number ? 'Invoice proof not yet attached — go to Submit Invoice Proof to upload' : 'No invoice for this POB'} />
            </>
          }
          right={
            <div>
              <DetailHero
                tone={statusTone(status)}
                title={data.campaign_name || `POB #${data.id}`}
                subtitle={`${data.product_name || 'Product'} · ${data.chemist_name || 'Chemist'}${data.shop_name ? ` (${data.shop_name})` : ''}`}
                badge={<VerificationStatusBadge status={report?.status || status} />}
                primaryLabel="POB amount"
                primary={fmtMoney(data.pob_amount)}
                secondary={`Invoice ${fmtMoney(data.invoice_amount)} · Qty ${data.quantity}`}
                facts={[
                  ['Chemist', `${data.chemist_name || '—'}${data.shop_name ? ` (${data.shop_name})` : ''}${data.chemist_city ? ` · ${data.chemist_city}` : ''}`],
                  ['Invoice', `${data.invoice_number || '—'}${data.invoice_date ? ` · ${data.invoice_date}` : ''}`],
                  ['PTR', fmtMoney(data.ptr)],
                  ['Scheme', data.scheme_type || '—'],
                  ['Submitted by', data.submitted_by],
                  ['Submitted', fmtDateTime(data.created_at)],
                ]}
              />

              {isRejected && (
                <div className="card" style={{ marginTop: 12, padding: 16 }}>
                  <h4 className="section-title">How to correct</h4>
                  <ol style={{ margin: '8px 0 0 16px', lineHeight: 2 }}>
                    <li>Go to <strong>My POBs</strong> and locate this submission.</li>
                    <li>If the invoice was missing or wrong, go to <strong>Submit Invoice Proof</strong> and re-upload the correct invoice for this POB.</li>
                    <li>If product/quantity was incorrect, submit a new POB with the correct details and mention this POB ID in the remarks.</li>
                  </ol>
                </div>
              )}

              <InvoiceSummaryReport report={report} po={{ pob_amount: data.pob_amount, invoice_amount: data.invoice_amount }} lines={lines} />

              {lines.length > 1 && (
                <div className="card" style={{ marginTop: 12, padding: 16 }}>
                  <h4 className="section-title">Other products in same visit</h4>
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead><tr>
                        <th>Product</th><th>Qty</th><th>PTR</th><th>POB value</th>
                      </tr></thead>
                      <tbody>
                        {lines.map((l) => (
                          <tr key={l.id} className={l.id === data.id ? 'row-current' : ''}>
                            <td><strong>{l.product_name}</strong>{l.id === data.id && <Badge tone="blue">this</Badge>}</td>
                            <td>{l.quantity}</td>
                            <td>{fmtMoney(l.ptr)}</td>
                            <td><strong>{fmtMoney(l.pob_amount)}</strong></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {history.length > 0 && (
                <div className="timeline" style={{ marginTop: 16 }}>
                  <h4>Status history</h4>
                  {[...history].reverse().map((h) => (
                    <div key={h.id} className="tl-item">
                      <span className="tl-dot" />
                      <div>
                        <strong>{h.action}</strong> <span className="muted">{fmtDateTime(h.created_at)}</span>
                        {h.reason && <div className="muted">{h.reason}</div>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          }
        />
      </div>
    );
  }

  return (
    <div>
      <PageHeader title={`POB #${pob.id}`} subtitle={data.campaign_name || ''}
        actions={<button className="btn" onClick={onBack}>← Back to POB records</button>} />

      <EligibilityCard pobId={pob.id} />

      <SplitDetail
        left={
          <>
            <h4 className="section-title">Invoice proof</h4>
            <ProofPane url={invoiceUrl} name={data.invoice_original_name} hint="Uploaded invoice proof"
              empty={data.invoice_number ? 'Invoice proof not yet attached' : 'No invoice for this POB'} />
          </>
        }
        right={
          <div>
            <DetailHero
              tone={statusTone(status)}
              title={data.campaign_name || `POB #${data.id}`}
              subtitle={`${data.product_name || 'Product'} · ${data.chemist_name || 'Chemist'}${data.shop_name ? ` (${data.shop_name})` : ''}`}
              badge={<VerificationStatusBadge status={report?.status || status} />}
              primaryLabel="POB amount"
              primary={fmtMoney(data.pob_amount)}
              secondary={`Invoice ${fmtMoney(data.invoice_amount)} · Qty ${data.quantity}`}
              facts={[
                ['Chemist', `${data.chemist_name || '—'}${data.shop_name ? ` (${data.shop_name})` : ''}${data.chemist_city ? ` · ${data.chemist_city}` : ''}`],
                ['Invoice', `${data.invoice_number || '—'}${data.invoice_date ? ` · ${data.invoice_date}` : ''}`],
                ['PTR', fmtMoney(data.ptr)],
                ['Scheme', data.scheme_type || '—'],
                ['Submitted by', data.submitted_by],
                ['Proof lag', data.proof_lag_days === null || data.proof_lag_days === undefined
                  ? 'Invoice proof not attached yet'
                  : `${data.proof_lag_days} day(s) after the POB submission`],
                ['Reporting chain', data.reporting_chain],
              ]}
            />

            {isRejected && rejectEntry && (
              <div className="decision-box decision-reject">
                <span className="decision-icon">✕</span>
                <div>
                  <strong>Rejected — {rejectEntry.reason || 'No reason provided'}</strong>
                  {rejectEntry.verifier_name && (
                    <div className="muted">By {rejectEntry.verifier_name}{rejectEntry.created_at ? ` · ${fmtDateTime(rejectEntry.created_at)}` : ''}</div>
                  )}
                </div>
              </div>
            )}

            {['pending_verification', 'needs_review', 'rejected', 'submitted'].includes(status) && (
              <ReExtractCard pobId={pob.id} hasInvoice={!!data.invoice_path} onDone={reload} />
            )}

            <div className="card" style={{ marginTop: 12 }}>
              <h4 className="section-title">Submission summary</h4>
              <p className="ai-note">
                Qty <strong>{data.quantity}</strong> × PTR <strong>{fmtMoney(data.ptr)}</strong> = POB amount <strong>{fmtMoney(data.pob_amount)}</strong>
              </p>
              <DetailTable rows={[
                ['Product', <strong>{data.product_name || '—'}</strong>],
                ['Chemist', <strong>{data.chemist_name || '—'}{data.shop_name ? ` (${data.shop_name})` : ''}</strong>],
                ['Submitted Qty', data.quantity],
                ['PTR', fmtMoney(data.ptr)],
                ['POB amount', <strong>{fmtMoney(data.pob_amount)}</strong>],
                ['Invoice', `${data.invoice_number || '—'}${data.invoice_date ? ` · ${data.invoice_date}` : ''}`],
                ['Scheme', data.scheme_type || '—'],
                ['Submitted by', <strong>{data.submitted_by || '—'}</strong>],
                ['Reporting chain', data.reporting_chain || '—'],
              ]} />
            </div>

            {isVerifier ? (
              <InvoiceProofReport report={report} po={{ pob_amount: data.pob_amount, invoice_amount: data.invoice_amount, quantity: data.quantity, invoice_date: data.invoice_date }} />
            ) : (
              <InvoiceSummaryReport report={report} po={{ pob_amount: data.pob_amount, invoice_amount: data.invoice_amount }} lines={lines} />
            )}

            {lines.length > 1 && (
              <div className="card" style={{ marginTop: 12 }}>
                <h4 className="section-title">Product details (same visit)</h4>
                <div className="table-wrap">
                  <table className="data-table">
                    <thead><tr>
                      <th>Brand / Product</th><th>SKU</th><th>Qty</th><th>PTR</th><th>PTS</th><th>POB value</th>
                    </tr></thead>
                    <tbody>
                      {lines.map((l) => (
                        <tr key={l.id} className={l.id === data.id ? 'row-current' : ''}>
                          <td>
                            <strong>{l.product_name}</strong>
                            {l.brand_name && String(l.brand_name).toLowerCase() !== String(l.product_name).toLowerCase()
                              ? <small className="muted"> — {l.brand_name}</small> : null}
                            {l.id === data.id && <Badge tone="blue">this line</Badge>}
                          </td>
                          <td>{l.sku || '—'}</td>
                          <td>{l.quantity}</td>
                          <td>{fmtMoney(l.ptr)}</td>
                          <td>{fmtMoney(l.pts)}</td>
                          <td><strong>{fmtMoney(l.pob_amount)}</strong></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {data.remarks && <p className="muted" style={{ marginTop: 12 }}>Remarks: {data.remarks}</p>}

            {history.length > 0 && (
              <div className="timeline" style={{ marginTop: 16 }}>
                <h4>Activity history</h4>
                {[...history].reverse().map((h) => (
                  <div key={h.id} className="tl-item">
                    <span className="tl-dot" />
                    <div>
                      <strong>{h.action}</strong> <span className="muted">{fmtDateTime(h.created_at)}</span>
                      {h.reason && <div className="muted">{h.reason}</div>}
                      {h.verifier_name && <div className="muted">by {h.verifier_name}</div>}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {data.gratifications?.length > 0 && (
              <div className="card" style={{ marginTop: 12 }}>
                <h4>Gratifications</h4>
                {data.gratifications.map((g) => (
                  <div key={g.id} className="kv-list">
                    <div><span>{g.type_code} · #{g.id}</span><strong><Badge tone={g.status}>{g.status}</Badge></strong></div>
                  </div>
                ))}
              </div>
            )}
          </div>
        }
      />
    </div>
  );
}

function ReExtractCard({ pobId, hasInvoice, onDone }) {
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(null);
  const inputRef = useRef(null);

  const run = async (withFile) => {
    if (withFile && !file) { toast('Choose an invoice file first', 'error'); return; }
    setBusy(true);
    try {
      const r = withFile
        ? await uploadFile(`/api/v1/pob/${pobId}/re-extract`, file)
        : await api(`/api/v1/pob/${pobId}/re-extract`, { method: 'POST' });
      toast(`Invoice re-extracted — status ${r.status}${r.auto_verified ? ' (auto-verified)' : ''}`, 'success');
      if (inputRef.current) inputRef.current.value = '';
      setFile(null);
      onDone();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <h4 className="section-title">Invoice extraction</h4>
      <p className="muted" style={{ marginBottom: 10 }}>
        The invoice details were read automatically by AI. If the extracted number, date or amount look wrong, replace the invoice and re-extract.
      </p>
      <div className="row" style={{ gap: 8, display: 'flex', flexWrap: 'wrap' }}>
        <input ref={inputRef} type="file" accept=".pdf,.jpg,.jpeg,.png,.webp,.gif" onChange={(e) => setFile(e.target.files?.[0] || null)} />
        <button className="btn btn-primary" disabled={busy} onClick={() => run(true)}>
          {busy ? 'Re-extracting…' : 'Replace & re-extract'}
        </button>
        {hasInvoice && (
          <button className="btn" disabled={busy} onClick={() => run(false)}>
            Re-extract current invoice
          </button>
        )}
      </div>
    </div>
  );
}

function EligibilityCard({ pobId }) {
  const { data, loading, error } = useAsync(() => api(`/api/v1/pob/${pobId}/eligibility`));

  if (loading) return <div className="card" style={{ marginTop: 12, padding: 16 }}><span className="muted">Checking eligibility…</span></div>;
  if (error || !data) return null;

  const agg = data.aggregate || {};
  const existing = data.gratification;
  const isEligible = data.eligible;
  const shortfalls = data.shortfalls || [];
  const productDetails = data.product_details || [];
  const productWarnings = data.product_warnings || [];
  const hasProductIssues = productWarnings.length > 0;

  if (existing) {
    return (
      <div className="card" style={{ marginTop: 12, padding: 16, borderLeft: '4px solid var(--green)' }}>
        <h4 className="section-title" style={{ margin: '0 0 8px', color: 'var(--green)' }}>Gratification earned</h4>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Badge tone="green">{existing.type_code}</Badge>
          <span>Worth <strong>{fmtMoney(existing.scheme_value)}</strong></span>
          <Badge tone={existing.status === 'eligible' ? 'green' : 'amber'}>{existing.status}</Badge>
        </div>
      </div>
    );
  }

  return (
    <div className="card" style={{ marginTop: 12, padding: 16,
      borderLeft: isEligible && !hasProductIssues ? '4px solid var(--green)' : isEligible ? '4px solid var(--amber)' : '4px solid var(--red, #e74c3c)' }}>
      <h4 className="section-title" style={{ margin: '0 0 8px' }}>
        {isEligible && !hasProductIssues ? 'Gratification eligible' : isEligible ? 'Gratification earned — but product gaps' : 'Gratification progress'}
      </h4>

      <p style={{ margin: '0 0 12px', color: 'var(--text-secondary)', fontSize: 13 }}>{data.message}</p>

      {/* Campaign-level aggregate summary */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 8, marginBottom: 12 }}>
        {agg.chemist_monthly_total_invoice > 0 && (
          <div style={{ background: 'var(--bg-secondary, #f5f5f5)', padding: '8px 12px', borderRadius: 6 }}>
            <div className="muted" style={{ fontSize: 11 }}>Total invoice (month)</div>
            <strong style={{ fontSize: 14 }}>{fmtMoney(agg.chemist_monthly_total_invoice)}</strong>
          </div>
        )}
        {agg.chemist_monthly_total_qty > 0 && (
          <div style={{ background: 'var(--bg-secondary, #f5f5f5)', padding: '8px 12px', borderRadius: 6 }}>
            <div className="muted" style={{ fontSize: 11 }}>Total qty (month)</div>
            <strong style={{ fontSize: 14 }}>{agg.chemist_monthly_total_qty} units</strong>
          </div>
        )}
        {agg.chemist_monthly_pob_count > 0 && (
          <div style={{ background: 'var(--bg-secondary, #f5f5f5)', padding: '8px 12px', borderRadius: 6 }}>
            <div className="muted" style={{ fontSize: 11 }}>POBs (month)</div>
            <strong style={{ fontSize: 14 }}>{agg.chemist_monthly_pob_count}</strong>
          </div>
        )}
        {agg.chemist_monthly_invoice_count > 0 && (
          <div style={{ background: 'var(--bg-secondary, #f5f5f5)', padding: '8px 12px', borderRadius: 6 }}>
            <div className="muted" style={{ fontSize: 11 }}>Invoices (month)</div>
            <strong style={{ fontSize: 14 }}>{agg.chemist_monthly_invoice_count}</strong>
          </div>
        )}
      </div>

      {/* Rule-level shortfalls (campaign threshold not met) */}
      {shortfalls.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <strong style={{ fontSize: 13, color: 'var(--red, #e74c3c)' }}>Campaign threshold not met:</strong>
          <div style={{ marginTop: 6 }}>
            {shortfalls.map((s, i) => {
              const pct = s.current && s.required ? Math.min(100, Math.round((s.current / s.required) * 100)) : 0;
              return (
                <div key={i} style={{ marginBottom: 8 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
                    <span>{s.label}</span>
                    <span className="muted">{s.message}</span>
                  </div>
                  <div style={{ marginTop: 4, background: 'var(--bg-tertiary, #e8e8e8)', borderRadius: 4, height: 6 }}>
                    <div style={{ width: `${pct}%`, height: '100%', background: 'var(--red, #e74c3c)', borderRadius: 4, transition: 'width 0.3s' }} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }} className="muted">
                    <span>Current: {s.field?.includes('invoice') ? fmtMoney(s.current) : s.current}</span>
                    <span>Required: {s.field?.includes('invoice') ? fmtMoney(s.required) : s.required}</span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Per-product progress + warnings */}
      {productDetails.length > 0 && (
        <div>
          <strong style={{ fontSize: 13 }}>Product-wise status:</strong>
          <div style={{ marginTop: 6 }}>
            {productDetails.map((pd) => {
              const pw = productWarnings.filter((w) => w.message?.includes(pd.product_name));
              const hasIssue = !pd.meets_all;
              return (
                <div key={pd.product_id} style={{ marginBottom: 10, padding: '8px 12px', background: hasIssue ? 'rgba(255,193,7,0.08)' : 'transparent', borderRadius: 6, border: hasIssue ? '1px solid rgba(255,193,7,0.3)' : '1px solid var(--border)' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                    <span style={{ fontWeight: 600, fontSize: 13 }}>{pd.product_name}</span>
                    {hasIssue ? <Badge tone="amber">Action needed</Badge> : <Badge tone="green">OK</Badge>}
                  </div>

                  {/* Quantity progress */}
                  {pd.min_quantity > 0 && (
                    <div style={{ marginBottom: 4 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                        <span className="muted">Quantity: {pd.purchased_qty} / {pd.min_quantity} min</span>
                        <span className="muted">{pd.qty_pct}%</span>
                      </div>
                      <div style={{ marginTop: 2, background: 'var(--bg-tertiary, #e8e8e8)', borderRadius: 3, height: 5 }}>
                        <div style={{ width: `${pd.qty_pct}%`, height: '100%', background: pd.qty_pct >= 100 ? 'var(--green)' : 'var(--amber)', borderRadius: 3, transition: 'width 0.3s' }} />
                      </div>
                    </div>
                  )}

                  {/* POB value progress */}
                  {pd.min_pob > 0 && (
                    <div>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                        <span className="muted">POB value: {fmtMoney(pd.purchased_pob)} / {fmtMoney(pd.min_pob)} min</span>
                        <span className="muted">{pd.pob_pct}%</span>
                      </div>
                      <div style={{ marginTop: 2, background: 'var(--bg-tertiary, #e8e8e8)', borderRadius: 3, height: 5 }}>
                        <div style={{ width: `${pd.pob_pct}%`, height: '100%', background: pd.pob_pct >= 100 ? 'var(--green)' : 'var(--amber)', borderRadius: 3, transition: 'width 0.3s' }} />
                      </div>
                    </div>
                  )}

                  {/* Warnings for this product */}
                  {pw.length > 0 && (
                    <div style={{ marginTop: 6, fontSize: 12, color: 'var(--amber)' }}>
                      {pw.map((w, j) => <div key={j}>⚠ {w.message}</div>)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
