import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, fmtDateTime, getSession } from '../../api';
import {
  DashHero, DonutChart, DonutLegend, ErrorBox, InsightList, LineChart, MetricTile,
  PanelCard, PeriodSelect, ProgressBar, Spinner, StatusBadge, Table, useAsync,
} from '../../ui';

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
];

const STATUS_TONE = {
  approved: 'green', auto_approved: 'green',
  pending: 'amber', needs_review: 'amber',
  rejected: 'red', duplicate: 'red', superseded: 'gray',
};

const fmtShort = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (v >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  if (v >= 1e3) return '₹' + (v / 1e3).toFixed(1) + 'k';
  return '₹' + Math.round(v).toLocaleString('en-IN');
};
const fmtNum = (n) => (Number(n) || 0).toLocaleString('en-IN');
const dayLabel = (s) => {
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
};
const ageDays = (iso) => {
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : Math.floor((Date.now() - t) / 86400000);
};

export default function AgentDashboard() {
  const [days, setDays] = useState(0);
  const ver = useAsync(() => api(`/api/v1/dashboards/verification?days=${days}`), [days]);
  const queue = useAsync(() => api('/api/v1/verification/queue?status=pending&limit=50'), []);
  const tat = useAsync(() => api('/api/v1/verification/tat/report'), []);

  const v = ver.data || {};
  const stats = v.stats || {};
  const items = (queue.data || {}).items || [];
  const tatRows = (tat.data || {}).items || [];

  const daily = useMemo(
    () => (v.daily || []).slice().sort((a, b) => String(a.day).localeCompare(String(b.day))),
    [v]);

  const segs = useMemo(() => Object.entries(stats)
    .map(([k, val]) => ({ label: k.replaceAll('_', ' '), value: val, color: STATUS_TONE[k] || 'gray' }))
    .filter((s) => s.value > 0), [stats]);

  const decided = (stats.approved || 0) + (stats.rejected || 0) + (stats.duplicate || 0);
  const approvalRate = decided ? Math.round((stats.approved || 0) / decided * 100) : null;
  const pendingValue = items.reduce((s, r) => s + (Number(r.pob_amount) || 0), 0);
  const oldest = items.reduce((m, r) => {
    const a = ageDays(r.v_created_at);
    return a != null && a > m ? a : m;
  }, 0);

  const me = getSession()?.user || {};
  const myTat = tatRows.find((r) => r.verifier_id === me.id);

  const insights = useMemo(() => {
    const out = [];
    if (items.length) {
      out.push({
        tone: oldest >= 7 ? 'red' : oldest >= 3 ? 'amber' : 'blue',
        icon: '◷',
        title: `${fmtNum(items.length)} invoice${items.length > 1 ? 's are' : ' is'} waiting on you`,
        detail: oldest > 0
          ? `The oldest has been queued for ${oldest} day${oldest > 1 ? 's' : ''} — clear these first.`
          : 'All queued today.',
      });
    }
    if (pendingValue > 0) {
      out.push({
        tone: 'teal', icon: '₹',
        title: `${fmtShort(pendingValue)} of POB value is unverified`,
        detail: 'Gratification cannot be released until these are decided.',
      });
    }
    if (approvalRate != null) {
      out.push({
        tone: approvalRate >= 75 ? 'green' : 'amber',
        icon: approvalRate >= 75 ? '✓' : '~',
        title: `${approvalRate}% of decided invoices were approved`,
        detail: `${fmtNum(stats.approved)} approved · ${fmtNum(stats.rejected)} rejected · ${fmtNum(stats.duplicate)} duplicates.`,
      });
    }
    if (myTat) {
      out.push({
        tone: 'primary', icon: '⏱',
        title: `You have verified ${fmtNum(myTat.verified)} invoices`,
        detail: `Your average turnaround is ${Number(myTat.avg_tat_hours || 0).toFixed(1)} hours.`,
      });
    } else if (v.avg_tat_hours) {
      out.push({
        tone: 'primary', icon: '⏱',
        title: `Team turnaround is ${Number(v.avg_tat_hours).toFixed(1)} hours`,
        detail: 'Measured from submission to decision.',
      });
    }
    return out;
  }, [items, oldest, pendingValue, approvalRate, stats, myTat, v]);

  const queueCols = [
    {
      key: 'invoice',
      label: 'Invoice',
      render: (r) => (
        <Link to="/app/verification" className="cell-link">
          <strong>{r.invoice_number || `POB #${r.pob_id}`}</strong>
          <span className="muted cell-sub">{r.chemist_name || r.shop_name || '—'}</span>
        </Link>
      ),
    },
    { key: 'mr_name', label: 'Submitted by', render: (r) => r.mr_name || <span className="muted">—</span> },
    { key: 'campaign_name', label: 'Campaign', render: (r) => r.campaign_name || <span className="muted">—</span> },
    { key: 'pob_amount', label: 'Value', render: (r) => <strong>{fmtShort(r.pob_amount)}</strong> },
    {
      key: 'age',
      label: 'Waiting',
      render: (r) => {
        const a = ageDays(r.v_created_at);
        if (a == null) return <span className="muted">—</span>;
        const tone = a >= 7 ? 'red' : a >= 3 ? 'amber' : 'green';
        return <span className={`delta ${tone === 'green' ? 'flat' : 'down'}`}>{a}d</span>;
      },
    },
    { key: 'v_status', label: 'Status', render: (r) => <StatusBadge value={r.v_status} /> },
  ];

  const tatCols = [
    { key: 'verifier_name', label: 'Verifier', render: (r) => <strong>{r.verifier_name || 'Unassigned'}</strong> },
    { key: 'verified', label: 'Decided', render: (r) => fmtNum(r.verified) },
    { key: 'avg_tat_hours', label: 'Avg turnaround', render: (r) => `${Number(r.avg_tat_hours || 0).toFixed(1)} h` },
    {
      key: 'share',
      label: 'Share of decisions',
      render: (r) => {
        const total = tatRows.reduce((s, x) => s + (Number(x.verified) || 0), 0);
        return <ProgressBar value={total ? (Number(r.verified) || 0) / total * 100 : 0} tone="primary" />;
      },
    },
  ];

  if (ver.loading) return <Spinner label="Loading verification analytics…" />;
  if (ver.error) return <ErrorBox error={ver.error} onRetry={ver.run} />;

  return (
    <div>
      <DashHero
        title={`Welcome back, ${me.full_name || me.username || 'Agent'} 👋`}
        subtitle="Your verification workload, turnaround and decision quality at a glance."
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />}
      />

      <div className="metric-grid">
        <MetricTile label="Waiting in queue" value={fmtNum(stats.pending)} icon="◷" tone="amber"
          sub={oldest > 0 ? `Oldest waiting ${oldest} day${oldest > 1 ? 's' : ''}` : 'Nothing ageing'} />
        <MetricTile label="Approved" value={fmtNum(stats.approved)} icon="✓" tone="green"
          sub={approvalRate == null ? 'No decisions yet' : `${approvalRate}% of decided`} />
        <MetricTile label="Rejected / duplicate" value={fmtNum((stats.rejected || 0) + (stats.duplicate || 0))} icon="✕" tone="red"
          sub={`${fmtNum(stats.rejected)} rejected · ${fmtNum(stats.duplicate)} duplicates`} />
        <MetricTile label="Average turnaround"
          value={v.avg_tat_hours ? `${Number(v.avg_tat_hours).toFixed(1)} h` : '—'}
          icon="⏱" tone="primary"
          sub={myTat ? `You average ${Number(myTat.avg_tat_hours || 0).toFixed(1)} h` : 'Submission to decision'} />
      </div>

      <div className="bi-grid">
        <PanelCard title="Decisions per day" sub="Invoices verified over the last 14 active days">
          <LineChart
            labels={daily.map((r) => dayLabel(r.day))}
            series={[{ label: 'Decisions', values: daily.map((r) => r.count), color: 'green' }]}
            fmt={fmtNum}
            height={210}
          />
        </PanelCard>

        <PanelCard title="Outcome mix" sub="How submissions were resolved"
          action={<Link className="btn btn-sm" to="/app/verification">Open queue</Link>}>
          {segs.length ? (
            <div className="donut-panel">
              <DonutChart segments={segs} size={158} thickness={22}
                centerValue={fmtNum(Object.values(stats).reduce((s, x) => s + x, 0))} centerLabel="total" />
              <DonutLegend segments={segs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">No verifications yet</div>}
        </PanelCard>

        <PanelCard title="Oldest in queue" span="2"
          sub={`${fmtShort(pendingValue)} of POB value awaiting a decision`}
          action={<Link className="btn btn-sm btn-primary" to="/app/verification">Start verifying</Link>}>
          {queue.error
            ? <ErrorBox error={queue.error} onRetry={queue.run} />
            : (
              <Table
                cols={queueCols}
                rows={items.slice().sort((a, b) =>
                  String(a.v_created_at).localeCompare(String(b.v_created_at))).slice(0, 10)}
                keyOf={(r) => r.verification_id}
                empty="Queue is clear — nothing pending"
              />
            )}
        </PanelCard>

        <PanelCard title="Verifier turnaround" sub="Decisions and speed per verifier">
          <Table cols={tatCols} rows={tatRows.slice(0, 8)} keyOf={(r) => r.verifier_id ?? r.verifier_name}
            empty="No decisions recorded yet" />
        </PanelCard>

        <PanelCard title="Top insights" sub="What needs attention right now">
          <InsightList items={insights} empty="Queue is clear" />
        </PanelCard>
      </div>
    </div>
  );
}
