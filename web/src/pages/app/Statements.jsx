import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, fmtDateTime, getSession, uploadFile } from '../../api';
import {
  Badge, ErrorBox, Field, Modal, PageHeader, ProgressBar, roleLabel, Spinner, Table, Tabs,
  TextInput, toast, useAsync,
} from '../../ui';
import { ExtractionMeta, PartiesView, money } from '../../StatementExtraction';

const STATUS_TABS = [
  { value: 'pending', label: 'Queued' },
  { value: 'processing', label: 'Processing' },
  { value: 'done', label: 'Extracted' },
  { value: 'pending_verif', label: 'Pending verification' },
  { value: 'verified_stage', label: 'Verified' },
  { value: 'rejected', label: 'Rejected' },
  { value: 'error', label: 'Failed' },
];

const STATUS_MAP = {
  pending: ['amber', 'Queued'],
  processing: ['blue', 'Extracting'],
  done: ['green', 'Extracted'],
  error: ['red', 'Failed'],
  rejected: ['red', 'Rejected'],
  deleted: ['gray', 'Deleted'],
};

function DocStatus({ upload }) {
  const [tone, label] = STATUS_MAP[upload.status] || ['gray', upload.status || '—'];
  return (
    <>
      <Badge tone={tone}>{label}</Badge>
      {upload.verification_status === 'verified' && <span> <Badge tone="green">verified</Badge></span>}
      {upload.verification_status === 'pending_verification' && <span> <Badge tone="amber">pending verification</Badge></span>}
    </>
  );
}

export default function Statements() {
  const s = getSession();
  const perms = s?.permissions || [];
  const canUpload = perms.includes('statement.upload');
  const canCredits = perms.includes('statement.credits');
  const canManage = perms.includes('statement.manage');

  const [filters, setFilters] = useState({ status: 'all', from: '', to: '' });
  const [uploadOpen, setUploadOpen] = useState(false);
  const [upload, setUpload] = useState({ mode: 'file', file: null, url: '', from: '', to: '' });
  const [dup, setDup] = useState(null);
  const [activeUploads, setActiveUploads] = useState([]);
  const [prog, setProg] = useState({});
  const [selected, setSelected] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);

  const docs = useAsync(
    () => api(`/api/v1/statements/documents?date_from=${filters.from}&date_to=${filters.to}&status_filter=${filters.status}`),
    [filters.from, filters.to, filters.status]);

  const reminder = useAsync(() => api('/api/v1/statements/period-check'));
  const credits = useAsync(() => (canCredits
    ? api('/api/v1/statements/credits')
    : Promise.resolve({ credits: null, recent_transactions: [] })), [canCredits]);

  const uploads = docs.data?.uploads || [];
  const statusCounts = docs.data?.status_counts || {};
  const wallet = credits.data?.credits || null;

  // Poll every upload that is still extracting: the ones this session queued
  // plus any "processing" rows already visible in the list (re-entering the
  // page continues tracking a worker still busy with an earlier document).
  const processingIds = useMemo(
    () => uploads.filter((u) => u.status === 'processing').map((u) => u.id),
    [uploads]);
  const pollIds = useMemo(
    () => [...new Set([...activeUploads, ...processingIds])],
    [activeUploads, processingIds]);

  useEffect(() => {
    if (!pollIds.length) return;
    let alive = true;
    let refreshDue = false;
    const iv = setInterval(async () => {
      for (const id of pollIds) {
        try {
          const p = await api(`/api/v1/statements/uploads/${id}/progress`);
          if (!alive) return;
          if (p.done) {
            refreshDue = true;
            setActiveUploads((prev) => prev.filter((x) => x !== id));
            if (p.status === 'error') toast(`Document #${id} failed: ${p.error_msg || 'extraction error'}`, 'error');
            else if (p.status === 'rejected') toast(`Document #${id} rejected: ${p.error_msg || ''}`, 'error');
            else toast(`Document #${id} extracted${p.verification_status ? ` · ${p.verification_status.replaceAll('_', ' ')}` : ''}`, 'success');
          } else {
            setProg((prev) => ({ ...prev, [id]: p.pct || prev[id] || 0 }));
          }
        } catch {
          setActiveUploads((prev) => prev.filter((x) => x !== id));
        }
      }
      if (refreshDue && alive) { docs.run(); refreshDue = false; }
    }, 2000);
    return () => { alive = false; clearInterval(iv); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollIds.join(',')]);

  const resetUpload = () => {
    setUpload({ mode: 'file', file: null, url: '', from: '', to: '' });
    setDup(null);
  };

  const submitUpload = async (force = false) => {
    const extra = {
      force_proceed: force ? '1' : '0',
      manual_from: upload.from,
      manual_to: upload.to,
    };
    try {
      let r;
      if (upload.mode === 'file' && upload.file) {
        r = await uploadFile('/api/v1/statements/upload', upload.file, extra);
      } else if (upload.mode === 'url' && upload.url.trim()) {
        r = await uploadFile('/api/v1/statements/upload-from-url', null, { ...extra, source_url: upload.url.trim() });
      } else {
        toast('Choose a file or paste a document link', 'error');
        return;
      }
      toast('Statement queued for extraction', 'success');
      setUploadOpen(false);
      resetUpload();
      setActiveUploads((prev) => [...new Set([...prev, r.upload_id])]);
      docs.run();
    } catch (e) {
      if (e.status === 409 && e.body?.detail?.duplicate_level) {
        setDup({ msg: e.body.detail.error, id: e.body.detail.existing_upload_id });
      } else {
        toast(e.message, 'error');
      }
    }
  };

  const doDelete = async () => {
    if (!confirmDelete) return;
    try {
      await api(`/api/v1/statements/uploads/${confirmDelete}/delete`, { method: 'POST' });
      toast(`Document #${confirmDelete} deleted`, 'success');
      setConfirmDelete(null);
      docs.run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const doRestore = async (id) => {
    try {
      await api(`/api/v1/statements/uploads/${id}/restore`, { method: 'POST' });
      toast(`Document #${id} restored`, 'success');
      docs.run();
    } catch (e) { toast(e.message, 'error'); }
  };

  const counts = (v) => (v === 'all' ? uploads.length : statusCounts[v] || 0);
  const tabs = [{ value: 'all', label: `All (${counts('all')})` }].concat(
    STATUS_TABS
      .filter((t) => counts(t.value) > 0 || t.value === filters.status)
      .map((t) => ({ value: t.value, label: `${t.label} (${counts(t.value)})` })));

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'doc', label: 'Document', render: (r) => (
      <div className="cell-link">
        <strong>{r.original_filename || '—'}</strong>
        <span className="muted cell-sub">{r.file_type}{r.stockist_name ? ` · ${r.stockist_name}` : ''}</span>
      </div>
    ) },
    { key: 'uploaded', label: 'Uploaded', render: (r) => (
      <div>
        {fmtDateTime(r.upload_date)}
        <span className="muted cell-sub">{r.uploader_name || '—'}{r.uploader_role ? ` · ${roleLabel(r.uploader_role)}` : ''}</span>
      </div>
    ) },
    { key: 'period', label: 'Period', render: (r) => (
      r.statement_from_date ? `${r.statement_from_date} → ${r.statement_to_date || '…'}` : '—'
    ) },
    { key: 'net', label: 'Invoice net', render: (r) => (
      r.invoice_net != null ? <strong>{money(r.invoice_net)}</strong> : '—'
    ), thClass: 'num' },
    { key: 'status', label: 'Status', render: (r) => (
      <div>
        <DocStatus upload={r} />
        {r.status === 'rejected' && r.rejection_reason && (
          <div className="muted" style={{ fontSize: 11, marginTop: 3 }} title={r.rejection_reason}>
            {r.rejection_reason.slice(0, 60)}{r.rejection_reason.length > 60 ? '…' : ''}
            {r.rejected_by_name ? ` · by ${r.rejected_by_name}` : ''}
          </div>
        )}
      </div>
    ) },
    { key: 'actions', label: '', render: (r) => (
      <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
        <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); setSelected(r.id); }}>View</button>
        {canManage && r.status !== 'deleted' && (
          <button className="btn btn-sm btn-danger" onClick={(e) => { e.stopPropagation(); setConfirmDelete(r.id); }}>Delete</button>
        )}
        {canManage && r.status === 'deleted' && (
          <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); doRestore(r.id); }}>Restore</button>
        )}
      </div>
    ) },
  ];

  if (selected) {
    return (
      <ExtractionDetailView upId={selected} onBack={() => setSelected(null)}
        canDelete={canManage} onDelete={(id) => setConfirmDelete(id)}
        onDeleted={() => { setSelected(null); docs.run(); }} />
    );
  }

  return (
    <div>
      <PageHeader title="Statements & Documents"
        subtitle="Upload claim statements for AI extraction, then review the results."
        actions={canUpload ? (
          <>
            {canCredits && (
              <Link className="btn" to="/app/statements/credits">Credits</Link>
            )}
            <button className="btn btn-primary" onClick={() => { resetUpload(); setUploadOpen(true); }}>+ Upload statement</button>
          </>
        ) : undefined} />

      {(wallet || reminder.data) && (
        <div className="stats-grid" style={{ marginBottom: 12 }}>
          {wallet && (
            <div className="card" style={{ padding: 14, gridColumn: 'span 2' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <strong>Credits remaining: {wallet.remaining}</strong>
                <span className="muted">{wallet.used_credits} used of {wallet.total_credits} · plan {wallet.plan}</span>
              </div>
              <ProgressBar value={wallet.total_credits ? Math.min(100, wallet.used_credits / wallet.total_credits * 100) : 0} tone="amber" />
            </div>
          )}
          {reminder.data?.reminder && (
            <div className="card" style={{ padding: 14 }}>
              <strong>⏰ Quarterly reminder</strong>
              <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>{reminder.data.reminder}</div>
            </div>
          )}
          {reminder.data && !reminder.data.reminder && (
            <div className="card" style={{ padding: 14 }}>
              <strong>✅ {reminder.data.current_quarter?.label} covered</strong>
              <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>
                {reminder.data.uploads_this_quarter} statement{reminder.data.uploads_this_quarter === 1 ? '' : 's'} uploaded for this quarter.
              </div>
            </div>
          )}
        </div>
      )}

      {activeUploads.length > 0 && (
        <div className="card" style={{ padding: 14, marginBottom: 12 }}>
          <strong>Extracting {activeUploads.length} document{activeUploads.length === 1 ? '' : 's'}…</strong>
          {activeUploads.map((id) => (
            <div key={id} className="muted" style={{ fontSize: 13, marginTop: 6 }}>
              #{id} — {prog[id] != null && prog[id] > 0 ? `${Math.round(prog[id])}%` : 'queued…'}
              <ProgressBar value={prog[id] || 5} tone="blue" />
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10, flexWrap: 'wrap' }}>
        <input type="date" className="input" value={filters.from}
          onChange={(e) => setFilters((f) => ({ ...f, from: e.target.value }))} title="From" />
        <input type="date" className="input" value={filters.to}
          onChange={(e) => setFilters((f) => ({ ...f, to: e.target.value }))} title="To" />
        <button className="btn btn-sm" onClick={() => setFilters({ status: filters.status, from: '', to: '' })}>Clear dates</button>
      </div>

      <Tabs items={tabs} active={filters.status} onChange={(v) => setFilters((f) => ({ ...f, status: v }))} />

      {docs.loading ? <Spinner label="Loading documents…" />
        : docs.error ? <ErrorBox error={docs.error} onRetry={docs.run} />
        : <Table cols={cols} rows={uploads} keyOf={(r) => r.id}
            onRowClick={(r) => setSelected(r.id)} empty="No statements match these filters" />}

      {/* Upload modal */}
      <Modal open={uploadOpen} title="Upload statement document" onClose={() => { setUploadOpen(false); resetUpload(); }}
        footer={<>
          <button className="btn" onClick={() => { setUploadOpen(false); resetUpload(); }}>Cancel</button>
          <button className="btn btn-primary" disabled={dup !== null}
            onClick={() => submitUpload(false)}>Queue for extraction</button>
        </>}>
        {dup ? (
          <div className="card" style={{ padding: 14, background: 'var(--amber-d, #FDEDD6)' }}>
            <strong style={{ color: 'var(--amber)' }}>Possible duplicate detected</strong>
            <p style={{ fontSize: 13, margin: '8px 0' }}>{dup.msg}</p>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn" onClick={() => setDup(null)}>Back to form</button>
              <button className="btn btn-amber" onClick={() => submitUpload(true)}>Upload anyway</button>
            </div>
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
              {['file', 'url'].map((m) => (
                <button key={m} className={`chip ${upload.mode === m ? 'active' : ''}`}
                  onClick={() => setUpload((u) => ({ ...u, mode: m }))}>
                  {m === 'file' ? 'Upload file' : 'Link from URL'}
                </button>
              ))}
            </div>
            {upload.mode === 'file' ? (
              <Field label="Document file" required hint="PDF, JPG, PNG, WebP, XLSX, XLS or CSV — max 10 MB">
                <input type="file" accept=".pdf,.jpg,.jpeg,.png,.webp,.xlsx,.xls,.csv"
                  onChange={(e) => setUpload((u) => ({ ...u, file: e.target.files?.[0] || null }))} />
                {upload.file && <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{upload.file.name}</div>}
              </Field>
            ) : (
              <Field label="Document URL" required hint="Direct link to S3 / Drive / a web-hosted file">
                <TextInput value={upload.url} onChange={(e) => setUpload((u) => ({ ...u, url: e.target.value }))}
                  placeholder="https://…" />
              </Field>
            )}
            <div className="grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <Field label="Statement from (optional)">
                <input type="date" className="input" value={upload.from}
                  onChange={(e) => setUpload((u) => ({ ...u, from: e.target.value }))} />
              </Field>
              <Field label="Statement to (optional)">
                <input type="date" className="input" value={upload.to}
                  onChange={(e) => setUpload((u) => ({ ...u, to: e.target.value }))} />
              </Field>
            </div>
            <p className="warn-note">One credit is consumed per document. Processing continues in the background — you can leave this page and track progress in the list above.</p>
          </>
        )}
      </Modal>

      {/* Delete confirm */}
      <Modal open={confirmDelete != null} title="Delete document?" onClose={() => setConfirmDelete(null)}
        footer={<>
          <button className="btn" onClick={() => setConfirmDelete(null)}>Cancel</button>
          <button className="btn btn-danger" onClick={doDelete}>Delete permanently</button>
        </>}>
        <p>Document <strong>#{confirmDelete}</strong> and its extraction data will be removed from your division's view. Use Restore if you change your mind.</p>
      </Modal>
    </div>
  );
}

/* ── Extraction detail ────────────────────────────────────────────────────── */

function ExtractionDetailView({ upId, onBack, onDeleted, canDelete, onDelete }) {
  const { data, loading, error, run } = useAsync(() => api(`/api/v1/statements/extractions/${upId}`), [upId]);
  if (loading) return <Spinner label="Loading extraction…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const upload = data.upload || {};
  const extraction = data.extraction || null;
  const parties = data.parties || [];
  const debug = data.debug_info || {};

  return (
    <div>
      <PageHeader title={upload.original_filename || `Document #${upId}`}
        subtitle={<><DocStatus upload={upload} />{upload.uploader_name ? ` · uploaded by ${upload.uploader_name}` : ''}</>}
        actions={<>
          {canDelete && upload.status !== 'deleted' && (
            <button className="btn btn-danger" onClick={() => onDelete(upId)}>Delete</button>
          )}
          <button className="btn" onClick={onBack}>← Back to statements</button>
        </>} />

      <div className="card" style={{ marginTop: 12 }}>
        <h4 className="section-title">Document</h4>
        <table className="detail-table">
          <tbody>
            <tr><th>Upload ID</th><td>#{upload.id}</td></tr>
            <tr><th>Upload date</th><td>{fmtDateTime(upload.upload_date)}</td></tr>
            <tr><th>Uploader</th><td>{upload.uploader_name}{upload.area ? ` · ${upload.area}` : ''}{upload.region ? ` · ${upload.region}` : ''}{upload.uploader_role ? ` · ${roleLabel(upload.uploader_role)}` : ''}</td></tr>
            <tr><th>File type</th><td>{upload.file_type}</td></tr>
            <tr><th>Stored as</th><td><code>{upload.stored_filename}</code></td></tr>
            {upload.status === 'rejected' && (
              <>
                <tr><th>Rejected by</th><td>{upload.rejected_by_name || '—'}</td></tr>
                <tr><th>Rejection reason</th><td>{upload.rejection_reason || '—'}</td></tr>
              </>
            )}
            {upload.status === 'error' && upload.error_msg && (
              <tr><th>Error</th><td>{upload.error_msg}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <ExtractionMeta extraction={extraction} />
      <PartiesView parties={parties} />

      {Object.keys(debug).length > 0 && (
        <details className="card" style={{ marginTop: 12, padding: 14 }}>
          <summary style={{ cursor: 'pointer', fontWeight: 600 }}>Debug info</summary>
          <pre style={{ fontSize: 11, overflow: 'auto', marginTop: 8 }}>{JSON.stringify(debug, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}