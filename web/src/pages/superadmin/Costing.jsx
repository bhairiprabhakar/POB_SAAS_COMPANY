import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../api';
import {
  BarChart, Badge, DashHero, ErrorBox, MetricTile, PanelCard, PeriodSelect,
  ProgressBar, Spinner, Table, useAsync,
} from '../../ui';

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
  { days: 365, label: 'This year' },
];

const CURR_SYMBOL = { USD: '$', INR: '₹', EUR: '€', GBP: '£' };

const MODEL_LABELS = {
  'gemini-2.5-flash': 'Gemini 2.5 Flash',
  'gemini-2.5-flash-lite': 'Gemini 2.5 Flash Lite',
  'gemini-3.5-flash': 'Gemini 3.5 Flash',
  'gemini-flash-latest': 'Gemini Flash (latest)',
  'gemini-2.5-pro': 'Gemini 2.5 Pro',
  'gemini-3-flash-preview': 'Gemini 3 Flash Preview',
  'gemini-3.1-flash-lite': 'Gemini 3.1 Flash Lite',
  'gemini-3.1-pro-preview': 'Gemini 3.1 Pro Preview',
};
const modelLabel = (m) => MODEL_LABELS[m] || m || '—';

const fmtNum = (n) => (Number(n) || 0).toLocaleString('en-IN');
const fmtTokens = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return fmtNum(v);
};
const fmtCost = (v, cur = 'USD') => {
  const s = CURR_SYMBOL[cur] || (cur ? cur + ' ' : '');
  const n = Number(v) || 0;
  if (n && n < 0.01) return s + n.toFixed(4);
  return s + n.toLocaleString('en-IN', { maximumFractionDigits: n < 1 ? 4 : 2 });
};
const monthLabel = (m) => {
  const [y, mm] = String(m || '').split('-');
  if (!y || !mm) return m;
  return new Date(Number(y), Number(mm) - 1, 1)
    .toLocaleDateString('en-IN', { month: 'short', year: '2-digit' });
};

export default function Costing() {
  const [days, setDays] = useState(30);
  const [divFilter, setDivFilter] = useState(0);
  const { data, loading, error, run } = useAsync(
    () => api('/api/v1/superadmin/costing?days=' + days), [days]);

  const d = data || {};
  const summary = d.summary || {};
  const currency = d.currency || 'USD';
  const divisions = d.by_division || [];
  const models = d.by_model || [];
  const users = d.by_user || [];
  const monthly = d.monthly || [];
  const recent = d.recent || [];

  const totalCost = Number(summary.cost) || 0;
  const totalCall = Number(summary.calls) || 0;

  const filteredUsers = useMemo(
    () => (divFilter ? users.filter((u) => u.division_id === divFilter) : users),
    [users, divFilter]);

  const modelCols = [
    { key: 'model', label: 'Model', render: (r) => (
      <div className="cell-link"><strong>{modelLabel(r.model)}</strong>
        <span className="muted cell-sub">{r.calls} calls</span></div>
    ) },
    { key: 'input', label: 'Input tokens', render: (r) => fmtTokens(r.input_tokens), thClass: 'num' },
    { key: 'output', label: 'Output tokens', render: (r) => fmtTokens(r.output_tokens), thClass: 'num' },
    { key: 'cost', label: `Cost (${currency})`, render: (r) => <strong>{fmtCost(r.cost, currency)}</strong>, thClass: 'num' },
    { key: 'share', label: 'Share', render: (r) => (
      <ProgressBar value={totalCost ? (r.cost || 0) / totalCost * 100 : 0} tone="primary" />) },
  ];

  const divCols = [
    { key: 'name', label: 'Division', render: (r) => (
      <Link to={`/superadmin/divisions/${r.division_id}`} className="cell-link">
        <strong>{r.name}</strong>
        <span className="muted cell-sub">{r.code}</span>
      </Link>
    ) },
    { key: 'calls', label: 'Calls', render: (r) => fmtNum(r.calls), thClass: 'num' },
    { key: 'input', label: 'Input tokens', render: (r) => fmtTokens(r.input_tokens), thClass: 'num' },
    { key: 'output', label: 'Output tokens', render: (r) => fmtTokens(r.output_tokens), thClass: 'num' },
    { key: 'cost', label: `Cost (${currency})`, render: (r) => <strong>{fmtCost(r.cost, currency)}</strong>, thClass: 'num' },
    { key: 'models', label: 'Models', render: (r) => (
      <span className="model-chips">
        {r.models?.map((m) => (
          <span key={m.model} className="model-chip">{modelLabel(m.model)}<b>{m.calls}</b></span>
        ))}
      </span>
    ) },
  ];

  const userCols = [
    { key: 'who', label: 'User', render: (r) => (
      <div className="cell-link"><strong>{r.full_name}</strong>
        <span className="muted cell-sub">@{r.username} · {r.role}</span></div>
    ) },
    { key: 'division', label: 'Division', render: (r) => (
      <Link to={`/superadmin/divisions/${r.division_id}`} className="cell-link">
        {r.division_name}<span className="muted cell-sub">{r.division_code}</span>
      </Link>
    ) },
    { key: 'calls', label: 'Calls', render: (r) => fmtNum(r.calls), thClass: 'num' },
    { key: 'input', label: 'Input', render: (r) => fmtTokens(r.input_tokens), thClass: 'num' },
    { key: 'output', label: 'Output', render: (r) => fmtTokens(r.output_tokens), thClass: 'num' },
    { key: 'cost', label: `Cost (${currency})`, render: (r) => <strong>{fmtCost(r.cost, currency)}</strong>, thClass: 'num' },
  ];

  const recCols = [
    { key: 'ts', label: 'When', render: (r) => <span className="muted">{r.created_at}</span> },
    { key: 'div', label: 'Division', render: (r) => (
      <span>{r.division_name}<span className="muted cell-sub"> · {r.division_code}</span></span>
    ) },
    { key: 'user', label: 'User', render: (r) => <>{r.full_name}<span className="muted cell-sub"> {r.username && `@${r.username}`}</span></> },
    { key: 'model', label: 'Model', render: (r) => modelLabel(r.model) },
    { key: 'tokens', label: 'Tokens (in → out)', render: (r) => (
      <span className="mono">{fmtTokens(r.input_tokens)} → {fmtTokens(r.output_tokens)}</span>
    ), thClass: 'num' },
    { key: 'cost', label: `Cost`, render: (r) => <strong>{fmtCost(r.cost, currency)}</strong>, thClass: 'num' },
    { key: 'status', label: 'Status', render: (r) => (
      <Badge tone={r.status === 'error' ? 'red' : 'green'}>{r.status}</Badge> ) },
    { key: 'inv', label: 'Invoice', render: (r) => (
      <span className="muted">{r.invoice_number || '—'}{r.filename && <em className="cell-sub"> {r.filename}</em>}</span>
    ) },
  ];

  if (loading) return <Spinner label="Aggregating Gemini usage across every division tenant…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      <DashHero title="AI usage & cost"
        subtitle="Gemini invoice-extraction tokens and spend, by division, by user and by model."
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />} />

      <div className="metric-grid">
        <MetricTile label={`AI spend (${currency})`} value={fmtCost(totalCost, currency)} icon="₹" tone="primary"
          sub={summary.avg_cost ? `${fmtCost(summary.avg_cost, currency)} average per call` : 'No calls in period yet'} />
        <MetricTile label="Extraction calls" value={fmtNum(totalCall)} icon="⚡" tone="teal"
          sub={`${fmtNum(summary.reported_divisions)} division tenant${summary.reported_divisions === 1 ? '' : 's'} reporting`} />
        <MetricTile label="Input tokens" value={fmtTokens(summary.input_tokens)} icon="↳" tone="blue"
          sub="Prompt / document tokens sent to Gemini" />
        <MetricTile label="Output tokens" value={fmtTokens(summary.output_tokens)} icon="↪" tone="green"
          sub="Extraction JSON tokens generated" />
      </div>

      <div className="bi-grid">
        <PanelCard title="Spend by model" sub="Which Gemini models are driving cost"
          action={<Badge tone="gray">{models.length} model{models.length === 1 ? '' : 's'}</Badge>}>
          <Table cols={modelCols} rows={models} keyOf={(r) => r.model} empty="No Gemini calls recorded yet" />
        </PanelCard>

        <PanelCard title="Spend trend" sub={`Extraction cost per month in ${currency}`}>
          {monthly.length ? (
            <BarChart labels={monthly.map((m) => monthLabel(m.month))}
              series={[{ label: `Cost (${currency})`, values: monthly.map((m) => m.cost), color: 'blue' }]}
              fmt={(v) => fmtCost(v, currency)} height={210} />
          ) : <div className="chart-empty">No usage recorded in this period</div>}
        </PanelCard>

        <PanelCard title="Cost by division" span="2"
          sub="Every division tenant's extraction spend, ranked by cost" action={
            <Link className="btn btn-sm" to="/superadmin/divisions">Divisions</Link>}>
          <Table cols={divCols} rows={divisions} keyOf={(r) => r.division_id}
            empty="No extraction usage recorded yet" />
        </PanelCard>

        <PanelCard title="Cost by user" span="2"
          sub="Extraction spend per user across divisions"
          action={<select className="period-select" value={divFilter}
            onChange={(e) => setDivFilter(Number(e.target.value))}>
            <option value={0}>All divisions</option>
            {divisions.map((dv) => <option key={dv.division_id} value={dv.division_id}>{dv.name}</option>)}
          </select>}>
          <Table cols={userCols} rows={filteredUsers} keyOf={(r) => `${r.division_id}-${r.user_id}`}
            empty="No extraction usage recorded yet" />
        </PanelCard>

        <PanelCard title="Recent extraction calls" span="2"
          sub="Individual Gemini calls with tokens, model and verdict">
          <Table cols={recCols} rows={recent} keyOf={(r) => `${r.division_id}-${r.id}`}
            empty="No calls recorded in this period" />
        </PanelCard>
      </div>
    </div>
  );
}