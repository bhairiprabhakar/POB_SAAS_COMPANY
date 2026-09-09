import { useState } from 'react';
import { api, downloadFile, fmtDateTime } from '../../api';
import {
  ErrorBox, Field, Modal, PageHeader, Select, Spinner, Table, TextInput, toast, useAsync,
} from '../../ui';

const REPORTS = [
  { type: 'campaign', label: 'Campaign Report', desc: 'Campaigns with products, POBs and values' },
  { type: 'invoice', label: 'Invoice Report', desc: 'Every invoice submitted with verification state' },
  { type: 'chemist', label: 'Chemist Master', desc: 'Full chemist database' },
  { type: 'mr_performance', label: 'MR Performance', desc: 'POBs, value, verified/rejected per MR' },
  { type: 'leaderboard', label: 'Leaderboard', desc: 'Top field force by verified POB value' },
  { type: 'manager_performance', label: 'Manager Performance', desc: 'Team rollup for managers' },
  { type: 'brand_performance', label: 'Brand Performance', desc: 'POB value by brand' },
  { type: 'daily', label: 'Daily POB Summary', desc: 'POBs and value per day' },
  { type: 'weekly', label: 'Weekly POB Summary', desc: 'POBs and value per ISO week' },
  { type: 'monthly', label: 'Monthly POB Summary', desc: 'POBs and value per month' },
  { type: 'state', label: 'State Report', desc: 'Chemists, POBs and value by state' },
  { type: 'verification_tat', label: 'Verification TAT', desc: 'Turnaround time per verification' },
  { type: 'approval', label: 'Approval History', desc: 'Every approve/reject decision logged' },
  { type: 'pending_verification', label: 'Pending Verification', desc: 'POBs still awaiting verification' },
  { type: 'duplicate', label: 'Duplicate Invoices', desc: 'Flagged duplicate / needs-review submissions' },
  { type: 'visit', label: 'Chemist Visit Log', desc: 'Follow-up visits and stock liquidation' },
  { type: 'gift', label: 'Gift Dispatch Log', desc: 'Physical gifts dispatched with GPS/photo' },
  { type: 'cashback', label: 'Cashback / UPI Ledger', desc: 'Cashback approvals and payments' },
  { type: 'audit', label: 'Audit Log', desc: 'Who did what in this company' },
];

export default function Reports() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/reports/schedules'));
  const [showSchedule, setShowSchedule] = useState(false);
  const [busy, setBusy] = useState('');

  const exportReport = async (type) => {
    setBusy(type);
    try {
      await downloadFile(`/api/v1/reports/${type}`, `${type}_report.xlsx`);
      toast('Report exported', 'success');
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(''); }
  };

  if (loading) return <Spinner label="Loading reports…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      <PageHeader title="Reports" subtitle="Excel exports and scheduled reports"
        actions={<button className="btn btn-primary" onClick={() => setShowSchedule(true)}>+ Schedule report</button>} />
      <div className="report-grid">
        {REPORTS.map((r) => (
          <div key={r.type} className="card report-card">
            <h4>{r.label}</h4>
            <p className="muted">{r.desc}</p>
            <button className="btn btn-sm" disabled={busy === r.type} onClick={() => exportReport(r.type)}>
              {busy === r.type ? 'Exporting…' : 'Export XLSX'}
            </button>
          </div>
        ))}
      </div>
      {data?.items?.length > 0 && (
        <>
          <h3 className="sub-head">Scheduled reports</h3>
          <Table cols={[
            { key: 'name', label: 'Name' },
            { key: 'report_type', label: 'Type' },
            { key: 'cron', label: 'Cron' },
            { key: 'recipients', label: 'Recipients' },
            { key: 'enabled', label: 'Enabled', render: (r) => r.enabled ? '✓' : '✕' },
            { key: 'last_run', label: 'Last run', render: (r) => fmtDateTime(r.last_run) },
            { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
              <button className="btn-link danger" onClick={async () => {
                try { await api(`/api/v1/reports/schedules/${r.id}`, { method: 'DELETE' }); toast('Schedule deleted', 'success'); run(); }
                catch (e) { toast(e.message, 'error'); }
              }}>Delete</button>
            ) },
          ]} rows={data?.items || []} keyOf={(r) => r.id} />
        </>
      )}
      {showSchedule && (
        <ScheduleModal onClose={() => setShowSchedule(false)} onDone={() => { setShowSchedule(false); run(); }} />
      )}
    </div>
  );
}

function ScheduleModal({ onClose, onDone }) {
  const [f, setF] = useState({ format: 'excel', enabled: true, report_type: 'campaign' });
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));
  const submit = async (e) => {
    e.preventDefault();
    try {
      await api('/api/v1/reports/schedules', { method: 'POST', body: f });
      toast('Report scheduled', 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); }
  };
  return (
    <Modal open title="Schedule a report" onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="sched-form">Save</button></>}>
      <form id="sched-form" onSubmit={submit}>
        <Field label="Name" required><TextInput value={f.name || ''} onChange={set('name')} required /></Field>
        <Field label="Report"><Select value={f.report_type} onChange={set('report_type')}
          options={REPORTS.map((r) => ({ value: r.type, label: r.label }))} /></Field>
        <Field label="Cron expression" hint="e.g. 0 8 * * 1 (Monday 8am)"><TextInput value={f.cron || ''} onChange={set('cron')} placeholder="0 8 * * 1" /></Field>
        <Field label="Recipients" hint="Comma-separated emails"><TextInput value={f.recipients || ''} onChange={set('recipients')} /></Field>
      </form>
    </Modal>
  );
}
