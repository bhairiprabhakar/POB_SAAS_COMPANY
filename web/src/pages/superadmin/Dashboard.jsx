import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, getSession } from '../../api';
import {
  DashHero, DonutChart, DonutLegend, ErrorBox, InsightList, LineChart, MetricTile,
  PanelCard, PeriodSelect, ProgressBar, Spinner, Table, useAsync,
} from '../../ui';

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
];

const DONUT_COLORS = ['blue', 'green', 'teal', 'amber', 'red', 'gray'];

const fmtShort = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (v >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  if (v >= 1e3) return '₹' + (v / 1e3).toFixed(1) + 'k';
  return '₹' + Math.round(v).toLocaleString('en-IN');
};
const fmtNum = (n) => (Number(n) || 0).toLocaleString('en-IN');
const monthLabel = (m) => {
  const [y, mm] = String(m || '').split('-');
  if (!y || !mm) return m;
  return new Date(Number(y), Number(mm) - 1, 1)
    .toLocaleDateString('en-IN', { month: 'short', year: '2-digit' });
};

export default function SuperDashboard() {
  const [days, setDays] = useState(0);
  const { data, loading, error, run } = useAsync(
    () => api('/api/v1/superadmin/analytics?days=' + days), [days]);

  const d = data || {};
  const totals = d.totals || {};
  const divs = d.divisions || {};
  const rows = d.by_division || [];
  const monthly = d.monthly || [];

  const valueSegs = useMemo(() => {
    const top = rows.filter((r) => r.pob_value > 0).slice(0, 5);
    const rest = rows.slice(5).reduce((s, r) => s + (r.pob_value || 0), 0);
    const segs = top.map((r, i) => ({
      label: r.code || r.name, value: r.pob_value, color: DONUT_COLORS[i % DONUT_COLORS.length],
    }));
    if (rest > 0) segs.push({ label: 'All others', value: rest, color: 'gray' });
    return segs;
  }, [rows]);

  const insights = useMemo(() => {
    const out = [];
    if (divs.unreachable > 0) {
      out.push({
        tone: 'red', icon: '!',
        title: `${divs.unreachable} tenant${divs.unreachable > 1 ? 's' : ''} could not be read`,
        detail: (d.unreachable || []).map((u) => u.code).join(', ') + ' — check schema migrations.',
      });
    }
    if (totals.approval_rate != null) {
      const good = totals.approval_rate >= 75;
      out.push({
        tone: good ? 'green' : 'amber', icon: good ? '✓' : '~',
        title: `Platform approval rate is ${totals.approval_rate}%`,
        detail: `${fmtNum(totals.verified)} approved against ${fmtNum(totals.rejected)} rejected.`,
      });
    }
    if (totals.pending > 0) {
      out.push({
        tone: 'amber', icon: '◷',
        title: `${fmtNum(totals.pending)} submissions awaiting verification`,
        detail: 'Queued across all division tenants — watch for divisions with no active verifier.',
      });
    }
    const idle = rows.filter((r) => r.pobs === 0).length;
    if (idle > 0) {
      out.push({
        tone: 'gray', icon: '○',
        title: `${idle} division${idle > 1 ? 's have' : ' has'} no POB activity`,
        detail: 'Provisioned but not yet transacting — candidates for onboarding follow-up.',
      });
    }
    return out;
  }, [divs, totals, rows, d]);

  const cols = [
    {
      key: 'name',
      label: 'Division',
      render: (r) => (
        <Link to={`/superadmin/divisions/${r.division_id}/campaigns`} className="cell-link">
          <strong>{r.name}</strong>
          <span className="muted cell-sub">{r.code}</span>
        </Link>
      ),
    },
    { key: 'users', label: 'Users', render: (r) => <>{fmtNum(r.active_users)}<span className="muted"> / {fmtNum(r.users)}</span></> },
    { key: 'active_campaigns', label: 'Campaigns', render: (r) => <>{fmtNum(r.active_campaigns)}<span className="muted"> / {fmtNum(r.campaigns)}</span></> },
    { key: 'pobs', label: 'POBs', render: (r) => fmtNum(r.pobs) },
    { key: 'pob_value', label: 'POB Value', render: (r) => <strong>{fmtShort(r.pob_value)}</strong> },
    {
      key: 'approval_rate',
      label: 'Approval',
      render: (r) => (r.approval_rate == null
        ? <span className="muted">—</span>
        : <ProgressBar value={r.approval_rate} tone={r.approval_rate >= 75 ? 'green' : r.approval_rate >= 50 ? 'amber' : 'red'} />),
    },
  ];

  if (loading) return <Spinner label="Aggregating every division tenant…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const sa = getSession()?.user || {};

  return (
    <div>
      <DashHero
        title={`Welcome back, ${sa.username || 'Super Admin'} 👋`}
        subtitle={`Here's what's happening across ${divs.reporting || 0} provisioned division ${divs.reporting === 1 ? 'tenant' : 'tenants'}.`}
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />}
      />

      <div className="metric-grid">
        <MetricTile label="Divisions" value={fmtNum(divs.total)} icon="▣" tone="primary"
          sub={`${fmtNum(divs.active)} active · ${fmtNum(divs.reporting)} reporting`} />
        <MetricTile label="Platform users" value={fmtNum(totals.users)} icon="👥" tone="blue"
          sub={`${fmtNum(totals.active_users)} active accounts`} />
        <MetricTile label="POB submissions" value={fmtNum(totals.pobs)} icon="≣" tone="teal"
          sub={`${fmtNum(totals.verified)} verified · ${fmtNum(totals.pending)} pending`} />
        <MetricTile label="POB value" value={fmtShort(totals.pob_value)} icon="₹" tone="green"
          sub={totals.approval_rate != null ? `${totals.approval_rate}% approval rate` : 'No decisions yet'} />
      </div>

      <div className="bi-grid">
        <PanelCard title="Platform activity" sub="POB submissions per month, all division tenants combined">
          <LineChart
            labels={monthly.map((m) => monthLabel(m.month))}
            series={[
              { label: 'POBs', values: monthly.map((m) => m.pobs), color: 'blue' },
            ]}
            fmt={fmtNum}
            height={210}
          />
        </PanelCard>

        <PanelCard title="Value by division" sub="Share of total POB value"
          action={<Link className="btn btn-sm" to="/superadmin/divisions">View all</Link>}>
          {valueSegs.length ? (
            <div className="donut-panel">
              <DonutChart segments={valueSegs} size={158} thickness={22}
                centerValue={fmtShort(totals.pob_value)} centerLabel="total" />
              <DonutLegend segments={valueSegs} fmt={fmtShort} />
            </div>
          ) : <div className="chart-empty">No POB value recorded yet</div>}
        </PanelCard>

        <PanelCard title="Division performance" span="2"
          sub="Ranked by POB value — approval bar shows verified vs decided"
          action={<Link className="btn btn-sm" to="/superadmin/divisions">Manage divisions</Link>}>
          <Table cols={cols} rows={rows.slice(0, 12)} keyOf={(r) => r.division_id}
            empty="No provisioned divisions yet" />
        </PanelCard>

        <PanelCard title="Platform insights" span="2" sub="What needs attention right now">
          <InsightList items={insights} empty="Everything looks healthy" />
        </PanelCard>
      </div>
    </div>
  );
}