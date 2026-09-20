import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, getSession } from '../../api';
import {
  DashHero, DonutChart, DonutLegend, ErrorBox, Icon, InsightList, LineChart, MetricTile,
  PanelCard, PeriodSelect, ProgressBar, Spinner, Table, useAsync,
} from '../../ui';

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
];

const STATUS_TONE = {
  verified: 'green', approved: 'green', completed: 'green', paid: 'green',
  pending: 'amber', pending_verification: 'amber', submitted: 'amber', needs_review: 'amber',
  rejected: 'red', duplicate: 'red',
};

const fmtShort = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (v >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  if (v >= 1e3) return '₹' + (v / 1e3).toFixed(1) + 'k';
  return '₹' + Math.round(v).toLocaleString('en-IN');
};
const fmtNum = (n) => (Number(n) || 0).toLocaleString('en-IN');
/* "All time" has no preceding window to compare against, so a delta there
   would be meaningless — return null and the tile hides the trend row. */
const pctDelta = (cur, prev, days) => {
  if (!days) return null;
  const c = Number(cur) || 0; const p = Number(prev) || 0;
  if (!p) return null;
  return ((c - p) / Math.abs(p)) * 100;
};
const monthLabel = (m) => {
  const [y, mm] = String(m || '').split('-');
  if (!y || !mm) return m;
  return new Date(Number(y), Number(mm) - 1, 1)
    .toLocaleDateString('en-IN', { month: 'short', year: '2-digit' });
};

export default function AdminDashboard() {
  const [days, setDays] = useState(0);
  const dash = useAsync(() => api(`/api/v1/dashboards/company?days=${days}`), [days]);
  const ver = useAsync(() => api(`/api/v1/dashboards/verification?days=${days}`), [days]);
  const fin = useAsync(() => api(`/api/v1/dashboards/finance?days=${days}`), [days]);
  const camp = useAsync(() => api(`/api/v1/dashboards/campaign?days=${days}`), [days]);
  const summ = useAsync(() => api(`/api/v1/analytics/summary?days=${days}`), [days]);

  const d = dash.data || {};
  const v = ver.data || {};
  const f = fin.data || {};
  const campaigns = (camp.data || {}).items || [];
  const team = (summ.data || {}).all_team || {};
  const teamPrev = (summ.data || {}).all_team_prev || {};

  const monthly = useMemo(
    () => (d.monthly_pob || []).slice().sort((a, b) => String(a.month).localeCompare(String(b.month))),
    [d]);

  const statusSegs = useMemo(() => Object.entries(d.pob_by_status || {})
    .map(([k, val]) => ({ label: k.replaceAll('_', ' '), value: val, color: STATUS_TONE[k] || 'gray' }))
    .filter((s) => s.value > 0), [d]);

  const verStats = v.stats || {};
  const decided = (verStats.approved || 0) + (verStats.rejected || 0);
  const approvalRate = decided ? Math.round((verStats.approved || 0) / decided * 100) : null;

  const insights = useMemo(() => {
    const out = [];
    if (approvalRate != null) {
      out.push({
        tone: approvalRate >= 75 ? 'green' : 'amber', icon: approvalRate >= 75 ? '✓' : '~',
        title: `Approval rate is ${approvalRate}%`,
        detail: `${fmtNum(verStats.approved)} approved against ${fmtNum(verStats.rejected)} rejected.`,
      });
    }
    if ((verStats.pending || 0) > 0) {
      out.push({
        tone: 'amber', icon: '◷',
        title: `${fmtNum(verStats.pending)} submissions waiting on a verifier`,
        detail: v.avg_tat_hours ? `Average turnaround is ${Number(v.avg_tat_hours).toFixed(1)} hours.` : 'No turnaround recorded yet.',
      });
    }
    if ((d.followups_due || 0) > 0) {
      out.push({
        tone: 'red', icon: '!',
        title: `${fmtNum(d.followups_due)} chemist follow-ups are due`,
        detail: 'Assign these to the field team before they age further.',
      });
    }
    if ((f.pending || 0) > 0) {
      out.push({
        tone: 'teal', icon: '₹',
        title: `${fmtShort(f.pending)} of gratification is unpaid`,
        detail: `${fmtShort(f.paid)} already disbursed across ${fmtNum(f.total_transactions)} transactions.`,
      });
    }
    const idle = campaigns.filter((c) => !c.pobs).length;
    if (idle > 0) {
      out.push({
        tone: 'gray', icon: '○',
        title: `${idle} of ${campaigns.length} campaigns have no submissions`,
        detail: 'Check activation and field communication for these campaigns.',
      });
    }
    return out;
  }, [approvalRate, verStats, v, d, f, campaigns]);

  // /dashboards/campaign returns: id, name, pobs, amount, invoice_value,
  // verified, pending, flagged.
  const campaignValue = useMemo(
    () => campaigns.reduce((s, x) => s + (Number(x.amount) || 0), 0), [campaigns]);

  const campCols = [
    {
      key: 'name',
      label: 'Campaign',
      render: (r) => (
        <Link to="/app/campaigns" className="cell-link">
          <strong>{r.name}</strong>
          <span className="muted cell-sub">
            {fmtNum(r.verified)} verified · {fmtNum(r.pending)} pending
            {r.flagged ? ` · ${fmtNum(r.flagged)} flagged` : ''}
          </span>
        </Link>
      ),
    },
    { key: 'pobs', label: 'POBs', render: (r) => fmtNum(r.pobs) },
    { key: 'amount', label: 'POB value', render: (r) => <strong>{fmtShort(r.amount)}</strong> },
    { key: 'invoice_value', label: 'Invoice value', render: (r) => fmtShort(r.invoice_value) },
    {
      key: 'share',
      label: 'Share of POB value',
      render: (r) => (
        <ProgressBar value={campaignValue ? (Number(r.amount) || 0) / campaignValue * 100 : 0} tone="primary" />
      ),
    },
  ];

  const loading = dash.loading || ver.loading;
  if (loading) return <Spinner label="Loading company analytics…" />;
  if (dash.error) return <ErrorBox error={dash.error} onRetry={dash.run} />;

  const me = getSession()?.user || {};
  const company = getSession()?.company || {};

  return (
    <div>
      <DashHero
        title={`Welcome back, ${me.full_name || me.username || 'Admin'} 👋`}
        subtitle={`Here's what's happening across ${company.name || 'your organisation'}.`}
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />}
      />

      <div className="metric-grid">
        <MetricTile label="POB submissions" value={fmtNum(d.pob_total)} icon="≣" tone="primary"
          delta={pctDelta(team.pobs, teamPrev.pobs, days)}
          sub={`${fmtNum(d.pob_verified)} verified`} />
        <MetricTile label="POB value" value={fmtShort(d.pob_amount)} icon="₹" tone="green"
          delta={pctDelta(team.amount, teamPrev.amount, days)}
          sub={`Invoice value ${fmtShort(d.pob_invoice_value)}`} />
        <MetricTile label="Active users" value={fmtNum(d.active_users)} icon={<Icon name="users" size={19} />} tone="blue"
          sub={`${fmtNum(d.users)} total accounts`} />
        <MetricTile label="Active campaigns" value={fmtNum(d.active_campaigns)} icon="◎" tone="teal"
          sub={`${fmtNum(d.campaigns)} campaigns · ${fmtNum(d.products)} products`} />
      </div>

      <div className="metric-grid">
        <MetricTile label="Chemists covered" value={fmtNum(d.chemists)} icon="◉" tone="amber"
          sub={`${fmtNum(d.visits)} visits logged`} />
        <MetricTile label="Pending verification" value={fmtNum(verStats.pending)} icon="◷" tone="amber"
          sub={v.avg_tat_hours ? `${Number(v.avg_tat_hours).toFixed(1)}h average turnaround` : 'No turnaround yet'} />
        <MetricTile label="Approval rate" value={approvalRate == null ? '—' : approvalRate + '%'} icon="✓" tone="green"
          sub={`${fmtNum(verStats.approved)} approved · ${fmtNum(verStats.rejected)} rejected`} />
        <MetricTile label="Gratification paid" value={fmtShort(f.paid)} icon={<Icon name="gift" size={19} />} tone="indigo"
          sub={`${fmtShort(f.pending)} still pending`} />
      </div>

      <div className="bi-grid">
        <PanelCard title="Submission trend" sub="POB volume and value by month">
          <LineChart
            labels={monthly.map((m) => monthLabel(m.month))}
            series={[{ label: 'POBs', values: monthly.map((m) => m.count ?? m.pobs), color: 'blue' }]}
            fmt={fmtNum}
            height={210}
          />
        </PanelCard>

        <PanelCard title="Status distribution" sub="Where submissions currently sit"
          action={<Link className="btn-link" to="/app/pob">View POBs →</Link>}>
          {statusSegs.length ? (
            <div className="donut-panel">
              <DonutChart segments={statusSegs} size={158} thickness={22}
                centerValue={fmtNum(d.pob_total)} centerLabel="total" />
              <DonutLegend segments={statusSegs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">No submissions yet</div>}
        </PanelCard>

        <PanelCard title="Campaign performance" span="2"
          sub="Ranked by POB value contributed"
          action={<Link className="btn-link" to="/app/campaigns">Manage campaigns →</Link>}>
          <Table
            cols={campCols}
            rows={campaigns.slice().sort((a, b) =>
              (Number(b.pob_amount ?? b.total_amount) || 0) - (Number(a.pob_amount ?? a.total_amount) || 0)).slice(0, 10)}
            keyOf={(r) => r.id}
            empty="No campaigns yet"
          />
        </PanelCard>

        <PanelCard title={<span><span className="ai-dot">✦</span>Top insights</span>} span="2"
          sub="What needs attention right now">
          <InsightList items={insights} empty="Everything looks healthy" />
        </PanelCard>
      </div>
    </div>
  );
}
