import { useEffect, useRef, useState } from 'react';
import { api, downloadFile, fmtDateTime, uploadFile } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, Spinner, StatCard, Table, Tabs,
  TextArea, TextInput, toast, useAsync,
} from '../../ui';
import { ExtractionMeta, money, PartiesView } from '../../StatementExtraction';

const STATUS_MAP = {
  pending: ['amber', 'Pending'],
  verified: ['green', 'Verified'],
  rejected: ['red', 'Rejected'],
};

const REJECT_REASONS = [
  'OCR data inaccurate',
  'Duplicate document',
  'Wrong document uploaded',
  'Document illegible / low quality',
  'Incomplete statement',
  'Other',
];

const PARTY_TYPES = [
  { value: 'stockist', label: 'Stockist' },
  { value: 'chemist', label: 'Chemist' },
  { value: 'other', label: 'Other' },
];

let keySeq = 0;
const nextKey = () => `new-${++keySeq}`;

export default function StatementsVerify() {
  const [tab, setTab] = useState('queue');
  const [selected, setSelected] = useState(null);

  if (selected) {
    return (
      <VerifyWorkspace mvId={selected} onBack={() => setSelected(null)} />
    );
  }

  return <VerifyPortal tab={tab} setTab={setTab} onOpen={setSelected} />;
}

function VerifyPortal({ tab, setTab, onOpen }) {
  const queue = useAsync(() => api('/api/v1/statements/verification/queue'));
  const unassigned = useAsync(() => api('/api/v1/statements/verification/unassigned'));
  const history = useAsync(() => api('/api/v1/statements/verification/history'));
  const [bulkSel, setBulkSel] = useState([]);

  const stats = queue.data?.stats || history.data?.stats || {};
  const active = { queue: queue, unassigned: unassigned, history: history }[tab];

  const taskCols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'doc', label: 'Document', render: (r) => (
      <div className="cell-link"><strong>{r.original_filename || '—'}</strong>
        <span className="muted cell-sub">{r.division_name || ''}{r.uploader_name ? ` · ${r.uploader_name}` : ''}</span></div>
    ) },
    { key: 'stockist', label: 'Stockist', render: (r) => r.stockist_name || '—' },
    { key: 'period', label: 'Period', render: (r) => (
      r.statement_from_date ? `${r.statement_from_date} → ${r.statement_to_date || '…'}` : '—'
    ) },
    { key: 'net', label: 'Invoice net', render: (r) => r.invoice_net != null ? <strong>{money(r.invoice_net)}</strong> : '—', thClass: 'num' },
    { key: 'parties', label: 'Parties', render: (r) => r.party_count || 0, thClass: 'num' },
    { key: 'when', label: 'Created', render: (r) => fmtDateTime(r.created_at) },
  ];

  const claim = async (id) => {
    try {
      await api(`/api/v1/statements/verification/${id}/claim`, { method: 'POST' });
      toast(`Task #${id} claimed`, 'success');
      queue.run(); unassigned.run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const bulkClaim = async () => {
    if (!bulkSel.length) return;
    try {
      const r = await api('/api/v1/statements/verification/bulk-claim', { method: 'POST', body: { mv_ids: bulkSel } });
      toast(`${r.claimed_count} task${r.claimed_count === 1 ? '' : 's'} claimed`, 'success');
      setBulkSel([]);
      queue.run(); unassigned.run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const exportHistory = () => downloadFile('/api/v1/statements/verification/history/export', 'verification-history.xlsx');

  return (
    <div>
      <PageHeader title="Statement Verification"
        subtitle="AI extraction quality control for uploaded claim statements."
        actions={tab === 'history' ? (
          <button className="btn" onClick={exportHistory}>⬇ Export Excel</button>
        ) : undefined} />

      <div className="stats-grid compact">
        <StatCard label="Verified total" value={stats.total_verified ?? 0} tone="green" />
        <StatCard label="Today" value={stats.today ?? 0} tone="blue" />
        <StatCard label="This week" value={stats.this_week ?? 0} tone="blue" />
        <StatCard label="This month" value={stats.this_month ?? 0} tone="blue" />
        <StatCard label="My queue" value={queue.data?.tasks?.length ?? '—'} tone="amber" />
        <StatCard label="Unassigned" value={unassigned.data?.unassigned_count ?? '—'} tone="amber" />
      </div>

      <Tabs items={[
        { value: 'queue', label: 'My Queue' },
        { value: 'unassigned', label: 'Unassigned' },
        { value: 'history', label: 'History' },
      ]} active={tab} onChange={setTab} />

      {active.loading ? <Spinner /> : active.error ? <ErrorBox error={active.error} onRetry={active.run} /> : (
        <>
          {tab === 'unassigned' && (unassigned.data?.tasks || []).length > 0 && (
            <div style={{ margin: '12px 0' }}>
              <button className="btn btn-primary" disabled={!bulkSel.length} onClick={bulkClaim}>
                Claim selected ({bulkSel.length})
              </button>
            </div>
          )}
          <Table cols={
            tab === 'unassigned' ? [
              { key: 'sel', label: '', render: (r) => (
                <input type="checkbox" className="chk" checked={bulkSel.includes(r.id)}
                  onChange={(e) => setBulkSel((prev) => e.target.checked ? [...prev, r.id] : prev.filter((x) => x !== r.id))} />
              ) },
              ...taskCols,
              { key: 'actions', label: '', render: (r) => (
                <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                  <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); claim(r.id); }}>Claim</button>
                  <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); onOpen(r.id); }}>View</button>
                </div>
              ) },
            ] : tab === 'history' ? [
              { key: 'verification_id', label: 'Ver', render: (r) => <strong>#{r.verification_id}</strong> },
              { key: 'doc', label: 'Document', render: (r) => (
                <div className="cell-link"><strong>{r.document_name}</strong>
                  <span className="muted cell-sub">{r.uploader_name || ''}</span></div>
              ) },
              { key: 'stockist', label: 'Stockist', render: (r) => r.stockist_name || '—' },
              { key: 'period', label: 'Period', render: (r) => (
                r.statement_from_date ? `${r.statement_from_date} → ${r.statement_to_date || '…'}` : '—'
              ) },
              { key: 'net', label: 'Invoice net', render: (r) => r.invoice_net != null ? <strong>{money(r.invoice_net)}</strong> : '—', thClass: 'num' },
              { key: 'status', label: 'Status', render: (r) => <VerStatusBadge value={r.verification_status} /> },
              { key: 'at', label: 'Verified', render: (r) => fmtDateTime(r.verified_at) },
            ] : taskCols
          } rows={
            tab === 'queue' ? (queue.data?.tasks || [])
            : tab === 'unassigned' ? (unassigned.data?.tasks || [])
            : (history.data?.history || [])
          } keyOf={(r) => r.id || r.verification_id}
            onRowClick={tab !== 'unassigned' ? ((r) => onOpen(tab === 'history' ? r.verification_id : r.id)) : undefined}
            empty={tab === 'queue' ? 'No tasks assigned to you yet'
              : tab === 'unassigned' ? 'No unassigned tasks — all caught up'
              : 'Nothing verified yet'} />
        </>
      )}
    </div>
  );
}

function VerStatusBadge({ value }) {
  const [tone, label] = STATUS_MAP[value] || ['gray', value || '—'];
  return <Badge tone={tone}>{label}</Badge>;
}

/* ── Verification workspace ───────────────────────────────────────────────── */

function VerifyWorkspace({ mvId, onBack }) {
  const detail = useAsync(() => api(`/api/v1/statements/verification/${mvId}`), [mvId]);

  const [parties, setParties] = useState(null);
  const [stockistName, setStockistName] = useState('');
  const [rangeFrom, setRangeFrom] = useState('');
  const [rangeTo, setRangeTo] = useState('');
  const [deletedPartyIds, setDeletedPartyIds] = useState([]);
  const [deletedItemIds, setDeletedItemIds] = useState([]);
  const doneInit = useRef(false);

  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [reasonLabel, setReasonLabel] = useState('');
  const [reExtracting, setReExtracting] = useState(false);
  const [uploadCorr, setUploadCorr] = useState(false);
  const corrFileRef = useRef(null);

  useEffect(() => { doneInit.current = false; setParties(null); }, [mvId]);

  useEffect(() => {
    if (detail.data && !doneInit.current) {
      doneInit.current = true;
      const src = detail.data;
      setParties((src.parties || []).map(({ party, items }) => ({
        party: { ...party, _key: String(party.id) },
        items: (items || []).map((i) => ({ ...i })),
      })));
      setStockistName(src.extraction?.stockist_name || '');
      setRangeFrom(src.extraction?.statement_from_date || '');
      setRangeTo(src.extraction?.statement_to_date || '');
      setDeletedPartyIds([]);
      setDeletedItemIds([]);
    }
  }, [detail.data, mvId]);

  if (detail.loading) return <Spinner label="Loading verification task…" />;
  if (detail.error) return <ErrorBox error={detail.error} onRetry={detail.run} />;

  const d = detail.data;
  const mv = d.mv || {};
  const extraction = d.extraction || null;
  const isPending = mv.status === 'pending';
  const [mvTone, mvLabel] = STATUS_MAP[mv.status] || ['gray', mv.status || '—'];

  const patchParty = (idx, key, value) => setParties((prev) => prev.map((p, i) =>
    i === idx ? { ...p, party: { ...p.party, [key]: value } } : p));

  const patchItem = (idx, iidx, key, value) => setParties((prev) => prev.map((p, i) => {
    if (i !== idx) return p;
    const items = p.items.map((it, j) => (j === iidx ? { ...it, [key]: value } : it));
    return { ...p, items };
  }));

  const addParty = () => setParties((prev) => [
    ...prev,
    { party: { _key: nextKey(), id: 0, name: '', area: '', type: 'stockist', dl_number: '', gst_number: '' }, items: [] },
  ]);

  const addItem = (idx) => setParties((prev) => prev.map((p, i) => {
    if (i !== idx) return p;
    const partyRef = p.party.id || p.party._key;
    return {
      ...p,
      items: [...p.items, {
        id: 0, party_id: partyRef, brand: '', mfg: '', pack: '',
        quantity: '', unit_rate: '', discount_percent: '', final_amount: '',
      }],
    };
  }));

  const removeParty = (idx, p) => {
    if (p.id) setDeletedPartyIds((prev) => [...prev, p.id]);
    setParties((prev) => prev.filter((_, i) => i !== idx));
  };

  const removeItem = (idx, iidx, it) => {
    if (it.id) setDeletedItemIds((prev) => [...prev, it.id]);
    setParties((prev) => prev.map((p, i) => (i === idx ? { ...p, items: p.items.filter((_, j) => j !== iidx) } : p)));
  };

  const downloadExcel = () => downloadFile(
    `/api/v1/statements/verification/${mvId}/download-excel`,
    `verification-${mvId}-${(extraction?.stockist_name || 'statement').replace(/\W+/g, '_')}.xlsx`);

  const submitInline = async () => {
    setBusy(true);
    try {
      const body = {
        stockist_name: stockistName,
        statement_from_date: rangeFrom,
        statement_to_date: rangeTo,
        parties: (parties || []).map(({ party }) => ({
          temp_id: party._key,
          id: party.id || 0,
          name: party.name || '', area: party.area || '', type: party.type || 'chemist',
          dl_number: party.dl_number || '', gst_number: party.gst_number || '',
        })),
        items: (parties || []).flatMap(({ party, items }) => items.map((it) => ({
          id: it.id || 0,
          party_id: it.id ? party.id : (party._key || party.id),
          brand: it.brand || '', mfg: it.mfg || '', pack: it.pack || '',
          quantity: Number(it.quantity) || 0,
          unit_rate: Number(it.unit_rate) || 0,
          discount_percent: Number(it.discount_percent) || 0,
          final_amount: Number(it.final_amount) || 0,
        }))),
        deleted_party_ids: deletedPartyIds,
        deleted_item_ids: deletedItemIds,
      };
      const r = await api(`/api/v1/statements/verification/${mvId}/inline-save`, { method: 'POST', body });
      toast(r.message || 'Saved & marked verified', 'success');
      onBack();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  const submitCorrections = async () => {
    if (!corrFileRef.current?.files?.[0]) { toast('Choose a corrected workbook (.xlsx)', 'error'); return; }
    setBusy(true);
    try {
      const r = await uploadFile(`/api/v1/statements/verification/${mvId}/upload-corrections`, corrFileRef.current.files[0]);
      toast(r.message || 'Corrections applied', 'success');
      setUploadCorr(false);
      onBack();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  const submitReject = async () => {
    if (!reason.trim()) { toast('Rejection reason required', 'error'); return; }
    setBusy(true);
    try {
      const r = await api(`/api/v1/statements/verification/${mvId}/reject`, {
        method: 'POST', body: { reason: reason.trim(), reason_label: reasonLabel || reason.trim() } });
      toast(r.message || 'Task rejected', 'success');
      setRejecting(false);
      onBack();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  const submitReextract = async () => {
    setBusy(true);
    try {
      const r = await api(`/api/v1/statements/verification/${mvId}/reextract`, { method: 'POST', timeout: 120000 });
      toast(r.message || 'Re-extracted', 'success');
      setReExtracting(false);
      doneInit.current = false;
      setParties(null);
      detail.run();
    } catch (e) { toast(e.message, 'error'); }
    finally { setBusy(false); }
  };

  return (
    <div>
      <PageHeader title={`Verification #${mvId}`}
        subtitle={<>
          <Badge tone={mvTone}>{mvLabel}</Badge>
          {mv.original_filename ? <span> · {mv.original_filename}</span> : null}
          {mv.division_name ? <span> · {mv.division_name}</span> : null}
        </>}
        actions={<>
          {isPending && (
            <button className="btn" onClick={() => downloadExcel()}>⬇ Excel workbook</button>
          )}
          <button className="btn" onClick={onBack}>← Back</button>
        </>} />

      <div className="card" style={{ marginTop: 12 }}>
        <h4 className="section-title">Document</h4>
        <table className="detail-table">
          <tbody>
            <tr><th>Upload date</th><td>{fmtDateTime(mv.upload_date)}</td></tr>
            <tr><th>Uploader</th><td>{mv.uploader_name}{mv.uploader_area ? ` · ${mv.uploader_area}` : ''}</td></tr>
            <tr><th>File type</th><td>{mv.file_type}</td></tr>
            <tr><th>Party count</th><td>{mv.party_count ?? (d.parties || []).length}</td></tr>
            {mv.excel_downloaded_at && (
              <tr><th>Workbook downloaded</th><td>{fmtDateTime(mv.excel_downloaded_at)}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <ExtractionMeta extraction={extraction} />

      {/* Action bar */}
      <div className="card" style={{ marginTop: 12, padding: 14, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {isPending ? (
          <>
            <button className="btn btn-primary" disabled={busy} onClick={submitInline}>
              {busy ? 'Saving…' : '✓ Save & mark verified'}
            </button>
            <button className="btn" disabled={busy} onClick={() => setUploadCorr(true)}>Upload corrected workbook</button>
            <button className="btn" disabled={busy} onClick={() => setReExtracting(true)}>⟳ Re-extract with AI</button>
            <button className="btn btn-danger" disabled={busy} onClick={() => setRejecting(true)}>Reject</button>
          </>
        ) : (
          <>
            <span className="muted">This task is {mv.status} and no longer editable.</span>
            <button className="btn" onClick={() => downloadExcel()}>⬇ Excel workbook</button>
          </>
        )}
        {busy && <span className="muted">Working…</span>}
      </div>

      {/* Editable extraction header */}
      {isPending && (
        <div className="card" style={{ marginTop: 12, padding: 14 }}>
          <h4 className="section-title">Statement header — corrections</h4>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 10 }}>
            <Field label="Stockist name">
              <TextInput value={stockistName} onChange={(e) => setStockistName(e.target.value)} />
            </Field>
            <Field label="Statement from">
              <input type="date" className="input" value={rangeFrom} onChange={(e) => setRangeFrom(e.target.value)} />
            </Field>
            <Field label="Statement to">
              <input type="date" className="input" value={rangeTo} onChange={(e) => setRangeTo(e.target.value)} />
            </Field>
          </div>
        </div>
      )}

      {/* Editable parties / items */}
      {isPending ? (
        <div style={{ marginTop: 12 }}>
          <h4 className="section-title">Parties & line items — corrections</h4>
          {(parties || []).map(({ party, items }, idx) => (
            <div className="card" key={party._key} style={{ marginTop: 12, padding: 14 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                <strong>Party {idx + 1}</strong>
                <button className="btn btn-sm btn-danger" onClick={() => removeParty(idx, party)}>Remove party</button>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10 }}>
                <Field label="Name"><TextInput value={party.name || ''} onChange={(e) => patchParty(idx, 'name', e.target.value)} /></Field>
                <Field label="Area"><TextInput value={party.area || ''} onChange={(e) => patchParty(idx, 'area', e.target.value)} /></Field>
                <Field label="Type">
                  <select className="input" value={party.type || 'stockist'} onChange={(e) => patchParty(idx, 'type', e.target.value)}>
                    {PARTY_TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                </Field>
                <Field label="DL number"><TextInput value={party.dl_number || ''} onChange={(e) => patchParty(idx, 'dl_number', e.target.value)} /></Field>
                <Field label="GST number"><TextInput value={party.gst_number || ''} onChange={(e) => patchParty(idx, 'gst_number', e.target.value)} /></Field>
              </div>
              <div className="table-wrap" style={{ marginTop: 10 }}>
                <table className="data-table">
                  <thead><tr>
                    <th>Brand</th><th>Mfg</th><th>Pack</th><th className="num">Qty</th>
                    <th className="num">Rate</th><th className="num">Disc %</th><th className="num">Amount</th><th />
                  </tr></thead>
                  <tbody>
                    {(items || []).map((it, iidx) => (
                      <tr key={it.id || `n-${iidx}`}>
                        <td><input className="input inv-in" value={it.brand || ''} onChange={(e) => patchItem(idx, iidx, 'brand', e.target.value)} /></td>
                        <td><input className="input inv-in" value={it.mfg || ''} onChange={(e) => patchItem(idx, iidx, 'mfg', e.target.value)} /></td>
                        <td><input className="input inv-in" value={it.pack || ''} onChange={(e) => patchItem(idx, iidx, 'pack', e.target.value)} /></td>
                        <td><input className="input inv-in num" type="number" defaultValue={it.quantity ?? ''}
                          onChange={(e) => patchItem(idx, iidx, 'quantity', e.target.value)} /></td>
                        <td><input className="input inv-in num" type="number" defaultValue={it.unit_rate ?? ''}
                          onChange={(e) => patchItem(idx, iidx, 'unit_rate', e.target.value)} /></td>
                        <td><input className="input inv-in num" type="number" defaultValue={it.discount_percent ?? ''}
                          onChange={(e) => patchItem(idx, iidx, 'discount_percent', e.target.value)} /></td>
                        <td><input className="input inv-in num" type="number" defaultValue={it.final_amount ?? ''}
                          onChange={(e) => patchItem(idx, iidx, 'final_amount', e.target.value)} /></td>
                        <td><button className="btn btn-sm btn-danger" onClick={() => removeItem(idx, iidx, it)}>✕</button></td>
                      </tr>
                    ))}
                    {(!items || !items.length) && (
                      <tr><td colSpan={8} style={{ color: 'var(--text-2, #68686D)', fontSize: 13 }}>No line items</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              <button className="btn btn-sm" style={{ marginTop: 8 }} onClick={() => addItem(idx)}>+ Add line item</button>
            </div>
          ))}
          <button className="btn" style={{ marginTop: 12 }} onClick={addParty}>+ Add party</button>
        </div>
      ) : (
        <PartiesView parties={d.parties || []} title="Parties & line items (final)" />
      )}

      {/* Reject modal */}
      <Modal open={rejecting} title="Reject this statement?" onClose={() => setRejecting(false)}
        footer={<>
          <button className="btn" onClick={() => setRejecting(false)}>Cancel</button>
          <button className="btn btn-danger" disabled={busy || !reason.trim()} onClick={submitReject}>
            {busy ? 'Rejecting…' : 'Confirm rejection'}
          </button>
        </>}>
        <p className="muted" style={{ marginBottom: 12 }}>
          {mv.original_filename} — the statement is moved to Rejected and the uploader is shown the reason.
        </p>
        <Field label="Quick reasons">
          <div className="reason-chips">
            {REJECT_REASONS.map((r) => (
              <button key={r} type="button" className={`chip ${reasonLabel === r ? 'active' : ''}`}
                onClick={() => {
                  setReasonLabel(r === reasonLabel ? '' : r);
                  if (r !== 'Other') setReason(r === reasonLabel ? '' : r);
                  else setReason('');
                }}>{r}</button>
            ))}
          </div>
        </Field>
        <Field label="Rejection reason" required>
          <TextArea rows={3} value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="Describe the issue — this is shown to the uploader" />
        </Field>
      </Modal>

      {/* Re-extract confirm */}
      <Modal open={reExtracting} title="Re-extract with AI?" onClose={() => setReExtracting(false)}
        footer={<>
          <button className="btn" onClick={() => setReExtracting(false)}>Cancel</button>
          <button className="btn btn-primary" disabled={busy} onClick={submitReextract}>
            {busy ? 'Extracting…' : 'Re-extract now'}
          </button>
        </>}>
        <p>The current extraction is discarded and Gemini re-reads the original document. The task stays open for your review. This consumes a fresh credit.</p>
      </Modal>

      {/* Upload corrections modal */}
      <Modal open={uploadCorr} title="Upload corrected workbook" onClose={() => setUploadCorr(false)}
        footer={<>
          <button className="btn" onClick={() => setUploadCorr(false)}>Cancel</button>
          <button className="btn btn-primary" disabled={busy} onClick={submitCorrections}>
            {busy ? 'Applying…' : 'Apply corrections'}
          </button>
        </>}>
        <p>Download the Excel workbook, edit the Parties / Items sheets, then upload it here. The document is marked verified on success.</p>
        <Field label="Corrected workbook" required hint=".xlsx only">
          <input type="file" accept=".xlsx,.xls" ref={corrFileRef} />
        </Field>
      </Modal>
    </div>
  );
}