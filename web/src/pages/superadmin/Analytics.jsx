import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../api';
import {
  BarChart, DashHero, DonutChart, DonutLegend, ErrorBox, InsightList, MetricTile,
  PanelCard, PeriodSelect, ProgressBar, RankList, Spinner, Table, Tabs, useAsync,
} from '../../ui';

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
];

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
const rate = (num, den) => (den ? Math.round((num / den) * 100) : 0);

export default function SuperAnalytics() {
  const [days, setDays] = useState(0);
  const [tab, setTab] = useState('value');
  const { data, loading, error, run } = useAsync(
    () => api('/api/v1/superadmin/analytics?days=' + days), [days]);

  const d = data || {};
  const totals = d.totals || {};
  const divs = d.divisions || {};
  const rows = d.by_division || [];
  const monthly = d.monthly || [];

  const reporting = rows.length;
  const transacting = rows.filter((r) => r.pobs > 0).length;
  const adopting = rows.filter((r) => r.active_campaigns > 0).length;

  /* Tenant health buckets — how much of the estate is actually live. */
  const healthSegs = useMemo(() => ([
    { label: 'transacting', value: transacting, color: 'green' },
    { label: 'set up, no POBs', value: adopting - transacting, color: 'amber' },
    { label: 'not started', value: reporting - adopting, color: 'gray' },
  ].filter((s) => s.value > 0)), [transacting, adopting, reporting]);

  const outcomeSegs = [
    { label: 'verified', value: totals.verified || 0, color: 'green' },
    { label: 'pending', value: totals.pending || 0, color: 'amber' },
    { label: 'rejected', value: totals.rejected || 0, color: 'red' },
  ].filter((s) => s.value > 0);

  const topByValue = rows.slice(0, 10);

  const breakdowns = {
    value: { getValue: (r) => r.pob_value, fmt: fmtShort, color: 'blue',
      sub: (r) => `${fmtNum(r.pobs)} POBs · ${r.code}` },
    volume: { getValue: (r) => r.pobs, fmt: fmtNum, color: 'teal',
      sub: (r) => `${fmtShort(r.pob_value)} · ${r.code}` },
    users: { getValue: (r) => r.users, fmt: fmtNum, color: 'green',
      sub: (r) => `${fmtNum(r.active_users)} active · ${r.code}` },
    campaigns: { getValue: (r) => r.campaigns, fmt: fmtNum, color: 'amber',
      sub: (r) => `${fmtNum(r.active_campaigns)} active · ${r.code}` },
  };
  const active = breakdowns[tab];
  const ranked = rows.slice().sort((a, b) => (active.getValue(b) || 0) - (active.getValue(a) || 0));

  const insights = useMemo(() => {
    const out = [];
    if (divs.unreachable > 0) {
      out.push({ tone: 'red', icon: '!',
        title: `${divs.unreachable} tenant database${divs.unreachable > 1 ? 's are' : ' is'} unreadable`,
        detail: (d.unreachable || []).map((u) => u.code).join(', ') + ' — schema migration needed.' });
    }
    if (reporting) {
      out.push({ tone: transacting / reporting >= 0.5 ? 'green' : 'amber', icon: '◎',
        title: `${rate(transacting, reporting)}% of divisions are transacting`,
        detail: `${transacting} of ${reporting} provisioned division tenants have submitted at least one POB.` });
    }
    if (totals.approval_rate != null) {
      out.push({ tone: totals.approval_rate >= 75 ? 'green' : 'amber', icon: '✓',
        title: `Platform approval rate is ${totals.approval_rate}%`,
        detail: `${fmtNum(totals.verified)} approved against ${fmtNum(totals.rejected)} rejected.` });
    }
    const concentration = totals.pob_value
      ? rate(topByValue.slice(0, 3).reduce((s, r) => s + (r.pob_value || 0), 0), totals.pob_value) : 0;
    if (concentration > 0) {
      out.push({ tone: concentration >= 70 ? 'amber' : 'teal', icon: '▦',
        title: `Top 3 divisions hold ${concentration}% of platform POB value`,
        detail: concentration >= 70
          ? 'Revenue is concentrated — worth diversifying the active base.'
          : 'Value is reasonably spread across the division tenants.' });
    }
    return out;
  }, [divs, d, reporting, transacting, totals, topByValue]);

  const cols = [
    { key: 'name', label: 'Division', render: (r) => (
      <Link to={`/superadmin/divisions/${r.division_id}/campaigns`} className="cell-link">
        <strong>{r.name}</strong>
        <span className="muted cell-sub">{r.code}</span>
      </Link>
    ) },
    { key: 'users', label: 'Users', render: (r) => <>{fmtNum(r.active_users)}<span className="muted"> / {fmtNum(r.users)}</span></> },
    { key: 'pobs', label: 'POBs', render: (r) => fmtNum(r.pobs) },
    { key: 'pob_value', label: 'POB value', render: (r) => <strong>{fmtShort(r.pob_value)}</strong> },
    { key: 'share', label: 'Share of platform', render: (r) => (
      <ProgressBar value={totals.pob_value ? (r.pob_value || 0) / totals.pob_value * 100 : 0} tone="primary" />
    ) },
    { key: 'approval_rate', label: 'Approval', render: (r) => (r.approval_rate == null
      ? <span className="muted">—</span>
      : <ProgressBar value={r.approval_rate}
          tone={r.approval_rate >= 75 ? 'green' : r.approval_rate >= 50 ? 'amber' : 'red'} />) },
  ];

  if (loading) return <Spinner label="Aggregating every division tenant…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      <DashHero
        title="Platform analytics"
        subtitle="Adoption, throughput and value across every division tenant."
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />} />

      <div className="metric-grid">
        <MetricTile label="POB value" value={fmtShort(totals.pob_value)} icon="₹" tone="primary"
          sub={`${fmtNum(totals.pobs)} submissions platform-wide`} />
        <MetricTile label="Division adoption" value={`${rate(transacting, reporting)}%`} icon="◎" tone="green"
          sub={`${transacting} of ${reporting} divisions transacting`} />
        <MetricTile label="Platform users" value={fmtNum(totals.users)} icon="👥" tone="teal"
          sub={`${fmtNum(totals.active_users)} active accounts`} />
        <MetricTile label="Chemists registered" value={fmtNum(totals.chemists)} icon="🏥" tone="amber"
          sub="Across all division tenants" />
      </div>

      <div className="bi-grid">
        <PanelCard title="Platform activity" span="2"
          sub="POB volume and value submitted per month, all division tenants combined">
          {monthly.length ? (
            <BarChart labels={monthly.map((m) => monthLabel(m.month))}
              series={[{ label: 'POBs', values: monthly.map((m) => m.pobs), color: 'blue' }]}
              fmt={fmtNum} height={230} />
          ) : <div className="chart-empty">No activity recorded yet</div>}
        </PanelCard>

        <PanelCard title="Division health" sub="How far each division has got"
          action={<Link className="btn btn-sm" to="/superadmin/divisions">Divisions</Link>}>
          {healthSegs.length ? (
            <div className="donut-panel">
              <DonutChart segments={healthSegs} size={150} thickness={22}
                centerValue={fmtNum(reporting)} centerLabel="tenants" />
              <DonutLegend segments={healthSegs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">No provisioned divisions</div>}
        </PanelCard>

        <PanelCard title="Platform outcome mix" sub="How submissions resolve across all division tenants">
          {outcomeSegs.length ? (
            <div className="donut-panel">
              <DonutChart segments={outcomeSegs} size={150} thickness={22}
                centerValue={fmtNum(totals.pobs)} centerLabel="POBs" />
              <DonutLegend segments={outcomeSegs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">No submissions yet</div>}
        </PanelCard>

        <PanelCard title="Verified vs unverified value" span="2"
          sub="Top divisions — how much of their submitted value has cleared verification">
          {topByValue.length ? (
            <BarChart
              labels={topByValue.map((r) => r.code)}
              series={[
                { label: 'Verified', values: topByValue.map((r) => Math.round((r.pob_value || 0) * (r.approval_rate || 0) / 100)), color: 'green' },
                { label: 'Not yet verified', values: topByValue.map((r) => Math.round((r.pob_value || 0) * (100 - (r.approval_rate || 0)) / 100)), color: 'amber' },
              ]}
              stacked fmt={fmtShort} height={230} />
          ) : <div className="chart-empty">No division value recorded</div>}
        </PanelCard>

        <PanelCard title="Division leaderboard" span="2"
          sub="Ranked across the dimension you pick"
          action={<Tabs items={[
            { value: 'value', label: 'POB value' },
            { value: 'volume', label: 'Volume' },
            { value: 'users', label: 'Users' },
            { value: 'campaigns', label: 'Campaigns' },
          ]} active={tab} onChange={setTab} />}>
          <RankList rows={ranked} getLabel={(r) => r.name} getValue={active.getValue}
            getSub={active.sub} fmt={active.fmt} limit={10} color={active.color}
            empty="No divisions reporting" />
        </PanelCard>

        <PanelCard title="Full division breakdown" span="2"
          sub="Every reporting division tenant, ranked by POB value">
          <Table cols={cols} rows={rows} keyOf={(r) => r.division_id}
            empty="No provisioned divisions yet" />
        </PanelCard>

        <PanelCard title="Platform insights" span="2" sub="What the numbers are telling you">
          <InsightList items={insights} empty="Everything looks healthy" />
        </PanelCard>
      </div>
    </div>
  );
}