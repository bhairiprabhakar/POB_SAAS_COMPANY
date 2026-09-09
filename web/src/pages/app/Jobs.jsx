import { useState } from 'react';
import { api, fmtDateTime } from '../../api';
import { ErrorBox, PageHeader, Spinner, toast, useAsync } from '../../ui';

export default function Jobs() {
  const [status, setStatus] = useState('');
  const { data, loading, error, run } = useAsync(
    () => api(`/api/v1/jobs?${status ? `status=${status}` : ''}`),
    [status],
  );
  const [ticking, setTicking] = useState(false);

  const tick = async () => {
    setTicking(true);
    try {
      const d = await api('/api/v1/jobs/tick', { method: 'POST' });
      toast(`Scheduler ran: ${d.processed} job(s) processed`, 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
    setTicking(false);
  };

  return (
    <div>
      <PageHeader title="Background Jobs" subtitle="Retry webhook deliveries, payout schedules, reminders and maintenance"
        actions={
          <button className="btn btn-primary" onClick={tick} disabled={ticking}>
            {ticking ? 'Running…' : 'Run scheduler now'}
          </button>
        } />
      <div className="toolbar">
        <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {['queued', 'running', 'done', 'failed'].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      {loading && <Spinner label="Loading jobs…" />}
      {error && <ErrorBox error={error} onRetry={run} />}
      {!loading && !error && (
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr>
              <th>Type</th><th>Status</th><th>Attempts</th><th>Run at</th><th>Finished</th><th>Error</th>
            </tr></thead>
            <tbody>
              {(data?.items || []).map((j) => (
                <tr key={j.id}>
                  <td><code>{j.job_type}</code></td>
                  <td>{j.status}</td>
                  <td>{j.attempts}/{j.max_attempts}</td>
                  <td className="nowrap">{fmtDateTime(j.run_at)}</td>
                  <td className="nowrap">{fmtDateTime(j.finished_at)}</td>
                  <td className="cell-pre">{j.error || '—'}</td>
                </tr>
              ))}
              {(data?.items || []).length === 0 && (
                <tr><td colSpan={6} className="empty-state">No jobs</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
