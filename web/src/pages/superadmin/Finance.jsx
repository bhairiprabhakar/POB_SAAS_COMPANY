import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../api';
import {
  Badge, BarChart, DashHero, DivisionLink, DonutChart, DonutLegend, EmptyState, ErrorBox, InsightList,
  MetricTile, PanelCard, PeriodSelect, ProgressBar, Spinner, Table, canOpenDivisionConsole, useAsync,
} from '../../ui';

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
  { days: 365, label: 'This year' },
];

const DONUT_COLORS = ['blue', 'green', 'teal', 'amber', 'red', 'gray'];
const STAGES = ['eligible', 'approved', 'paid', 'dispatched', 'delivered', 'completed'];
const STAGE_LABELS = {
  eligible: 'Eligible', approved: 'Approved', paid: 'Paid', dispatched: 'Dispatched',
  delivered: 'Delivered', completed: 'Completed',
};
const TYPE_LABELS = {
  cashback: 'Cashback', upi: 'UPI transfer', vouchers: 'Vouchers',
  physical_gift: 'Physical gift', gift: 'Physical gift', voucher: 'Voucher', other: 'Other',
};

const fmtShort = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (v >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  if (v >= 1e3) return '₹' + (v / 1e3).toFixed(1) + 'k';
  return '₹' + Math.round(v).toLocaleString('en-IN');
};
const fmtNum = (n) => (Number(n) || 0).toLocaleString('en-IN');
const fmtCost = (v) => {
  const n = Number(v) || 0;
  if (n && n < 0.01) return '$' + n.toFixed(4);
  return '$' + n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
};
const monthLabel = (m) => {
  const [y, mm] = String(m || '').split('-');
  if (!y || !mm) return m;
  return new Date(Number(y), Number(mm) - 1, 1)
    .toLocaleDateString('en-IN', { month: 'short', year: '2-digit' });
};

export default function SuperFinance() {
  const [days, setDays] = useState(0);
  const { data, loading, error, run } = useAsync(
    () => api('/api/v1/superadmin/finance?days=' + days), [days]);

  const d = data || {};
  const pv = d.payout_values || {};
  const pc = d.payout_counts || {};
  const types = d.types || {};
  const rows = d.by_division || [];
  const monthly = d.monthly || [];
  const committed = Number(d.committed) || 0;
  const paidOut = Number(d.paid_out) || 0;
  const cleared = Number(d.verified?.value) || 0;
  const aiCost = Number(d.ai?.cost) || 0;

  const pipeline = useMemo(() => ({
    labels: STAGES.map((s) => STAGE_LABELS[s]),
    values: STAGES.map((s) => Number(pv[s]) || 0),
  }), [pv]);

  const stageDonut = useMemo(() => (
    STAGES
      .map((s, i) => ({ label: STAGE_LABELS[s], value: Number(pv[s]) || 0, color: DONUT_COLORS[i % DONUT_COLORS.length] }))
      .filter((s) => s.value > 0)
  ), [pv]);

  const typeRows = useMemo(() => (
    Object.entries(types)
      .map(([code, t]) => ({ code, ...t }))
      .sort((a, b) => b.value - a.value)
  ), [types]);

  const typeDonut = useMemo(() => (
    typeRows
      .map((t, i) => ({ label: TYPE_LABELS[t.code] || t.code, value: t.value, color: DONUT_COLORS[i % DONUT_COLORS.length] }))
      .filter((s) => s.value > 0)
  ), [typeRows]);

  const totalValue = useMemo(() => typeRows.reduce((s, t) => s + (t.value || 0), 0), [typeRows]);

  const insights = useMemo(() => {
    const out = [];
    if (d.unreachable?.length) {
      out.push({
        tone: 'red', icon: '!',
        title: `${d.unreachable.length} tenant${d.unreachable.length > 1 ? 's' : ''} could not be read`,
        detail: (d.unreachable || []).map((u) => u.code).join(', ') + ' — finance totals exclude them.',
      });
    }
    if (cleared > 0) {
      const claimCost = cleared + aiCost;
      const healthy = committed <= cleared;
      out.push({
        tone: healthy ? 'green' : 'amber', icon: healthy ? '✓' : '◷',
        title: `${fmtShort(committed)} committed against ${fmtShort(cleared)} cleared value`,
        detail: committed > 0
          ? `${Math.round((committed / cleared) * 100)}% of cleared claim value is already earmarked for payouts.`
          : 'No outstanding payout commitments yet.',
      });
    }
    if (aiCost > 0 && cleared > 0) {
      const per = cleared / aiCost;
      out.push({
        tone: per >= 50 ? 'green' : 'amber', icon: '◇',
        title: `₹${fmtShort(cleared)} value cleared for every ${fmtCost(aiCost)} of AI cost`,
        detail: `${fmtNum(d.ai?.calls || 0)} extraction calls platform-wide in this period.`,
      });
    }
    const top = rows[0];
    if (top && top.liability > 0 && committed > 0) {
      const share = Math.round((top.liability / committed) * 100);
      out.push({
        tone: share >= 40 ? 'amber' : 'teal', icon: '▦',
        title: `${top.name} carries ${share}% of total payout liability`,
        detail: 'Concentrated payables can strain cash; worth reviewing that division step.',
      });
    }
    const stageSum = STAGES.reduce((s, k) => s + (Number(pv[k]) || 0), 0);
    const notPaid = totalValue ? Math.round(((totalValue - stageSum) / totalValue) * 100) : 0;
    if (notPaid > 0) {
      out.push({
        tone: 'gray', icon: '◷',
        title: `${notPaid}% of earned grant value is not yet in a paid pipeline stage`,
        detail: 'Remaining records sit outside eligible→completed — usually physical gifts or vouchers still in transit.',
      });
    }
    return out;
  }, [d, rows, committed, cleared, aiCost, pv, totalValue]);

  const stageCols = [
    { key: 'stage', label: 'Stage', render: (r) => <strong>{r.label}</strong> },
    { key: 'records', label: 'Records', render: (r) => fmtNum(r.count), thClass: 'num' },
    { key: 'value', label: 'Grant value (₹)', render: (r) => <strong>{fmtShort(r.value)}</strong>, thClass: 'num' },
    { key: 'share', label: 'Share', render: (r) => (
      <ProgressBar value={totalValue ? (r.value / totalValue) * 100 : 0} tone="primary" />
    ) },
  ];
  const stageRows = STAGES.map((s) => ({ key: s, label: STAGE_LABELS[s], value: Number(pv[s]) || 0, count: Number(pc[s]) || 0 }));

  const typeCols = [
    { key: 'type', label: 'Type', render: (r) => <strong>{TYPE_LABELS[r.code] || r.code}</strong> },
    { key: 'records', label: 'Records', render: (r) => fmtNum(r.count), thClass: 'num' },
    { key: 'value', label: 'Grant value (₹)', render: (r) => <strong>{fmtShort(r.value)}</strong>, thClass: 'num' },
    { key: 'share', label: 'Share', render: (r) => (
      <ProgressBar value={totalValue ? (r.value / totalValue) * 100 : 0} tone="teal" />
    ) },
  ];

  const divCols = [
    { key: 'name', label: 'Division', render: (r) => (
      <DivisionLink id={r.division_id} tab="gratification" className="cell-link">
        <strong>{r.name}</strong>
        <span className="muted cell-sub">{r.code}</span>
      </DivisionLink>
    ) },
    { key: 'verified_value', label: 'Cleared claim value', render: (r) => fmtShort(r.verified_value), thClass: 'num' },
    { key: 'liability', label: 'Payout liability', render: (r) => <strong>{fmtShort(r.liability)}</strong>, thClass: 'num' },
    { key: 'paid_out', label: 'Paid out', render: (r) => fmtShort(r.paid_out), thClass: 'num' },
    { key: 'ai_cost', label: 'AI cost', render: (r) => <span className="mono muted">{fmtCost(r.ai_cost)}</span>, thClass: 'num' },
    { key: 'payout_share', label: 'Liability share', render: (r) => (
      <ProgressBar value={committed ? (r.liability / committed) * 100 : 0} tone="amber" />
    ) },
  ];

  if (loading) return <Spinner label="Aggregating payout liability across every division…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const monthlyPayout = monthly.map((m) => m.paid_out || 0);
  const monthlyCleared = monthly.map((m) => m.cleared || 0);
  const hasMonthly = monthly.length > 0;

  return (
    <div>
      <DashHero
        title="Owner finance"
        subtitle="Payout liability, money out and cleared claim value across every division tenant."
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />}
      />

      <div className="metric-grid">
        <MetricTile label="Payout liability" value={fmtShort(committed)} icon="💸" tone="amber"
          sub={`${fmtNum((Number(pc.eligible) || 0) + (Number(pc.approved) || 0))} eligible + approved records`} />
        <MetricTile label="Paid out" value={fmtShort(paidOut)} icon="✓" tone="green"
          sub="Paid / dispatched / delivered / completed" />
        <MetricTile label="Cleared claim value" value={fmtShort(cleared)} icon="₹" tone="primary"
          sub={`${fmtNum(d.verified?.count || 0)} verified POBs`} />
        <MetricTile label="AI extraction cost" value={fmtCost(aiCost)} icon="◇" tone="blue"
          sub={`${fmtNum(d.ai?.calls || 0)} calls · ${d.currency || 'USD'}`} />
      </div>

      <div className="bi-grid">
        <PanelCard title="Payout pipeline" span="2"
          sub="Grant value in ₹ at each stage of payout across the whole estate">
          {pipeline.values.some((v) => v > 0) ? (
            <BarChart labels={pipeline.labels} series={[
              { label: 'Grant value', values: pipeline.values, color: 'blue' },
            ]} fmt={fmtShort} height={230} />
          ) : <div className="chart-empty">No payout value recorded yet</div>}
        </PanelCard>

        <PanelCard title="Liability by type" sub="Payout commitments by grant type">
          {typeDonut.length ? (
            <div className="donut-panel">
              <DonutChart segments={typeDonut} size={150} thickness={22}
                centerValue={fmtShort(totalValue)} centerLabel="total" />
              <DonutLegend segments={typeDonut} fmt={fmtShort} />
            </div>
          ) : <div className="chart-empty">No commitments recorded</div>}
        </PanelCard>

        <PanelCard title="Money out vs cleared value" span="2"
          sub="Monthly paid-out grant value vs verified claim value">
          {hasMonthly ? (
            <BarChart labels={monthly.map((m) => monthLabel(m.month))}
              series={[
                { label: 'Cleared value', values: monthlyCleared, color: 'green' },
                { label: 'Paid out', values: monthlyPayout, color: 'blue' },
              ]}
              fmt={fmtShort} height={230} />
          ) : <div className="chart-empty">No activity recorded yet</div>}
        </PanelCard>

        <PanelCard title="Payout stages" sub="Counts and ₹ at each step of the pipeline"
          action={<Badge tone="gray">{STAGES.length} stages</Badge>}>
          <Table cols={stageCols} rows={stageRows} keyOf={(r) => r.key}
            empty="No gratification records yet" />
        </PanelCard>

        <PanelCard title="By type" sub="Grant values grouped by payout mechanism">
          {typeRows.length
            ? <Table cols={typeCols} rows={typeRows} keyOf={(r) => r.code} empty="No records yet" />
            : <EmptyState text="No grants recorded yet" />}
        </PanelCard>

        <PanelCard title="Finance by division" span="2"
          sub="How much each division has committed, paid and cleared"
          action={canOpenDivisionConsole()
            ? <Link className="btn btn-sm" to="/superadmin/divisions">Divisions</Link> : null}>
          {rows.length
            ? <Table cols={divCols} rows={rows} keyOf={(r) => r.division_id}
                empty="No provisioned divisions yet" />
            : <EmptyState text="No finance data recorded yet" />}
        </PanelCard>

        <PanelCard title="Owner insights" span="2" sub="What your payables are telling you">
          <InsightList items={insights} empty="Everything looks healthy" />
        </PanelCard>
      </div>
    </div>
  );
}