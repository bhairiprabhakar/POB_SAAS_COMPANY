import { useMemo, useState } from 'react';
import { api, getSession } from '../../api';
import {
  BarChart, DashHero, DonutChart, DonutLegend, ErrorBox, InsightList,
  MetricTile, PanelCard, PeriodSelect, ProgressBar, RankList, Spinner, Table, Tabs, useAsync,
} from '../../ui';

/* ───────────────────────────────────────────────────────────────────────────
   Analytics, scoped to whoever is looking.

   /api/v1/analytics/roi is already hierarchy-scoped server-side (see
   saas/scoping.py): an MR gets only their own rows, a manager their team, an
   admin/verifier the whole company. So all three variants below read the same
   endpoint and differ only in which cuts of it are worth showing.
   ─────────────────────────────────────────────────────────────────────────── */

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
  { days: 180, label: 'Last 180 days' },
];

const STATUS_TONE = {
  verified: 'green', approved: 'green', paid: 'green',
  pending: 'amber', pending_verification: 'amber', submitted: 'amber',
  rejected: 'red', duplicate: 'red', needs_review: 'red',
};

const SCOPE_NOTE = {
  self: 'Your own submissions only',
  team: 'You and everyone reporting to you',
  all: 'Company-wide across every user',
};

const fmtShort = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (v >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  if (v >= 1e3) return '₹' + (v / 1e3).toFixed(1) + 'k';
  return '₹' + Math.round(v).toLocaleString('en-IN');
};
const fmtNum = (n) => (Number(n) || 0).toLocaleString('en-IN');
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
const rate = (num, den) => (den ? Math.round((num / den) * 100) : 0);

/* Shared period + scope header used by all three variants. */
function AnalyticsHead({ title, subtitle, scope, days, setDays }) {
  return (
    <>
      <DashHero title={title} subtitle={subtitle}
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />} />
      {scope && (
        <div className="scope-note">
          <span>Data scope</span><strong>{SCOPE_NOTE[scope] || scope}</strong>
        </div>
      )}
    </>
  );
}

/* Funnel: submitted → verified → rewarded, with conversion at each step. */
function ConversionFunnel({ totals }) {
  const submitted = totals.pobs || 0;
  const steps = [
    { label: 'Submitted', value: submitted, tone: 'blue' },
    { label: 'Verified', value: totals.verified || 0, tone: 'green' },
    { label: 'Rewarded', value: totals.rewards_count || 0, tone: 'teal' },
  ];
  if (!submitted) return <div className="chart-empty">No submissions in this period</div>;
  return (
    <div className="funnel">
      {steps.map((s, i) => (
        <div key={s.label} className="funnel-step">
          <div className="funnel-head">
            <span>{s.label}</span>
            <strong>{fmtNum(s.value)}</strong>
          </div>
          <ProgressBar value={rate(s.value, submitted)} tone={s.tone} />
          {i > 0 && (
            <div className="funnel-note">
              {s.value > steps[i - 1].value
                ? `More than ${steps[i - 1].label.toLowerCase()} — some awards attach to earlier submissions`
                : `${rate(s.value, steps[i - 1].value)}% of ${steps[i - 1].label.toLowerCase()}`}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/* ── Admin / company-wide analytics ──────────────────────────────────────── */
function CompanyAnalytics({ days, setDays, roi }) {
  const [tab, setTab] = useState('campaigns');
  // analytics/summary.own.monthly is the caller's OWN submissions -- an admin
  // rarely submits, so the company trend has to come from the company dashboard.
  const dash = useAsync(() => api(`/api/v1/dashboards/company?days=${days}`), [days]);
  const monthly = useMemo(
    () => ((dash.data || {}).monthly_pob || []).slice()
      .sort((a, b) => String(a.month).localeCompare(String(b.month))),
    [dash.data]);
  const d = roi.data || {};
  const t = d.totals || {};
  const prev = d.prev || {};

  const statusSegs = [
    { label: 'verified', value: t.verified || 0, color: 'green' },
    { label: 'pending', value: t.pending || 0, color: 'amber' },
    { label: 'rejected', value: t.rejected || 0, color: 'red' },
  ].filter((s) => s.value > 0);

  const breakdowns = {
    campaigns: { rows: d.campaigns || [], label: (r) => r.name, color: 'blue',
      sub: (r) => `${fmtNum(r.pobs)} POBs · ${rate(r.verified, r.pobs)}% verified` },
    members: { rows: d.members || [], label: (r) => r.full_name, color: 'green',
      sub: (r) => `${fmtNum(r.pobs)} POBs · ${r.level_name || r.region || '—'}` },
    chemists: { rows: d.chemists || [], label: (r) => r.shop_name || r.name, color: 'teal',
      sub: (r) => `${fmtNum(r.pobs)} POBs · ${[r.city, r.state].filter(Boolean).join(', ') || '—'}` },
    products: { rows: d.products || [], label: (r) => r.name, color: 'amber',
      sub: (r) => `${fmtNum(r.pobs)} POBs${r.sku ? ' · ' + r.sku : ''}` },
    divisions: { rows: d.divisions || [], label: (r) => r.division, color: 'red',
      sub: (r) => `${fmtNum(r.campaigns)} campaigns · ${fmtNum(r.pobs)} POBs` },
  };
  const active = breakdowns[tab];
  const sorted = (active.rows || []).slice().sort((a, b) => (b.amount || 0) - (a.amount || 0));

  const divisionRows = (d.divisions || []).slice(0, 8);

  return (
    <>
      <AnalyticsHead
        title="Company analytics"
        subtitle="Where POB value comes from, how it converts, and what it returns."
        scope={d.scope} days={days} setDays={setDays} />

      <div className="metric-grid">
        <MetricTile label="POB value" value={fmtShort(t.amount)} icon="₹" tone="primary"
          delta={pctDelta(t.amount, prev.amount, days)}
          sub={`${fmtNum(t.pobs)} submissions`} />
        <MetricTile label="Verified value" value={fmtShort(t.verified_value)} icon="✓" tone="green"
          delta={pctDelta(t.verified_value, prev.verified_value, days)}
          sub={`${t.approval_rate ?? 0}% approval rate`} />
        <MetricTile label="Rewards paid" value={fmtShort(t.rewards_paid)} icon="🎁" tone="teal"
          delta={pctDelta(t.rewards_paid, prev.rewards_paid, days)}
          sub={`${fmtShort(t.rewards_eligible)} eligible`} />
        <MetricTile label="Return on spend" value={t.roi ? `${Number(t.roi).toFixed(2)}×` : '—'} icon="📈" tone="amber"
          sub={t.roi_basis ? `Basis: ${t.roi_basis}` : 'Verified value ÷ rewards paid'} />
      </div>

      <div className="bi-grid">
        <PanelCard title="Value trend" span="2"
          sub="POB value submitted each month against the volume behind it">
          {monthly.length ? (
            <BarChart
              labels={monthly.map((m) => monthLabel(m.month))}
              series={[
                { label: 'POBs', values: monthly.map((m) => m.count ?? m.pobs), color: 'blue' },
              ]}
              fmt={fmtNum} height={230} />
          ) : <div className="chart-empty">No monthly data yet</div>}
        </PanelCard>

        <PanelCard title="Conversion funnel" sub="Submitted → verified → rewarded">
          <ConversionFunnel totals={t} />
        </PanelCard>

        <PanelCard title="Outcome mix" sub="How submissions resolved">
          {statusSegs.length ? (
            <div className="donut-panel">
              <DonutChart segments={statusSegs} size={150} thickness={22}
                centerValue={fmtNum(t.pobs)} centerLabel="POBs" />
              <DonutLegend segments={statusSegs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">No submissions yet</div>}
        </PanelCard>

        <PanelCard title="Division performance" span="2"
          sub="Value and approval quality per division">
          {divisionRows.length ? (
            <BarChart
              labels={divisionRows.map((r) => r.division)}
              series={[
                { label: 'Verified value', values: divisionRows.map((r) => r.verified_value), color: 'green' },
                { label: 'Unverified value', values: divisionRows.map((r) => Math.max(0, (r.amount || 0) - (r.verified_value || 0))), color: 'amber' },
              ]}
              stacked fmt={fmtShort} height={220} />
          ) : <div className="chart-empty">No divisions yet</div>}
        </PanelCard>

        <PanelCard title="Top contributors" span="2"
          sub="Ranked by POB value in this period"
          action={<Tabs items={[
            { value: 'campaigns', label: 'Campaigns' },
            { value: 'members', label: 'People' },
            { value: 'chemists', label: 'Chemists' },
            { value: 'products', label: 'Products' },
            { value: 'divisions', label: 'Divisions' },
          ]} active={tab} onChange={setTab} />}>
          <RankList rows={sorted} getLabel={active.label} getValue={(r) => r.amount}
            getSub={active.sub} fmt={fmtShort} limit={10} color={active.color}
            empty="Nothing recorded in this period" />
        </PanelCard>
      </div>
    </>
  );
}

/* ── Verification agent analytics ────────────────────────────────────────── */
function AgentAnalytics({ days, setDays, roi }) {
  const ver = useAsync(() => api(`/api/v1/dashboards/verification?days=${days}`), [days]);
  const tat = useAsync(() => api('/api/v1/verification/tat/report'), []);

  const d = roi.data || {};
  const t = d.totals || {};
  const v = ver.data || {};
  const stats = v.stats || {};
  const tatRows = (tat.data || {}).items || [];

  const daily = useMemo(
    () => (v.daily || []).slice().sort((a, b) => String(a.day).localeCompare(String(b.day))),
    [v]);

  const segs = Object.entries(stats)
    .map(([k, val]) => ({ label: k.replaceAll('_', ' '), value: val, color: STATUS_TONE[k] || 'gray' }))
    .filter((s) => s.value > 0);

  const decided = (stats.approved || 0) + (stats.rejected || 0) + (stats.duplicate || 0);
  const approvalRate = decided ? Math.round((stats.approved || 0) / decided * 100) : null;

  // Only campaigns that actually produced rejections -- ranking a list of 0%
  // rows tells the agent nothing.
  const campaignRows = (d.campaigns || []).slice()
    .filter((r) => r.pobs > 0 && (r.rejected || 0) > 0)
    .sort((a, b) => rate(b.rejected, b.pobs) - rate(a.rejected, a.pobs));

  return (
    <>
      <AnalyticsHead
        title="Verification analytics"
        subtitle="Throughput, turnaround and where rejections concentrate."
        scope={d.scope} days={days} setDays={setDays} />

      <div className="metric-grid">
        <MetricTile label="Decided" value={fmtNum(decided)} icon="✓" tone="primary"
          sub={`${fmtNum(stats.pending)} still queued`} />
        <MetricTile label="Approval rate" value={approvalRate == null ? '—' : approvalRate + '%'} icon="◎" tone="green"
          sub={`${fmtNum(stats.approved)} approved`} />
        <MetricTile label="Average turnaround"
          value={v.avg_tat_hours ? `${Number(v.avg_tat_hours).toFixed(1)} h` : '—'} icon="⏱" tone="teal"
          sub="Submission to decision" />
        <MetricTile label="Value verified" value={fmtShort(t.verified_value)} icon="₹" tone="amber"
          sub={`of ${fmtShort(t.amount)} submitted`} />
      </div>

      <div className="bi-grid">
        <PanelCard title="Decision throughput" span="2"
          sub="Invoices decided per day over the last 14 active days">
          {daily.length ? (
            <BarChart labels={daily.map((r) => {
                const dt = new Date(r.day);
                return Number.isNaN(dt.getTime()) ? r.day
                  : dt.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' });
              })}
              series={[{ label: 'Decisions', values: daily.map((r) => r.count), color: 'green' }]}
              fmt={fmtNum} height={230} />
          ) : <div className="chart-empty">No decisions recorded yet</div>}
        </PanelCard>

        <PanelCard title="Outcome mix" sub="How the queue resolved">
          {segs.length ? (
            <div className="donut-panel">
              <DonutChart segments={segs} size={150} thickness={22}
                centerValue={fmtNum(Object.values(stats).reduce((s, x) => s + x, 0))} centerLabel="total" />
              <DonutLegend segments={segs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">No verifications yet</div>}
        </PanelCard>

        <PanelCard title="Verifier workload" sub="Decisions and speed per verifier">
          <Table
            cols={[
              { key: 'verifier_name', label: 'Verifier', render: (r) => <strong>{r.verifier_name || 'Unassigned'}</strong> },
              { key: 'verified', label: 'Decided', render: (r) => fmtNum(r.verified) },
              { key: 'avg_tat_hours', label: 'Avg TAT', render: (r) => `${Number(r.avg_tat_hours || 0).toFixed(1)} h` },
            ]}
            rows={tatRows.slice(0, 8)} keyOf={(r) => r.verifier_id ?? r.verifier_name}
            empty="No decisions recorded yet" />
        </PanelCard>

        <PanelCard title="Where rejections concentrate" span="2"
          sub="Campaigns ranked by rejection rate — the ones worth a rule change">
          <RankList
            rows={campaignRows}
            getLabel={(r) => r.name}
            getValue={(r) => rate(r.rejected, r.pobs)}
            getSub={(r) => `${fmtNum(r.rejected)} rejected of ${fmtNum(r.pobs)} · ${fmtShort(r.amount)}`}
            fmt={(n) => `${n}%`} limit={10} color="red"
            empty="No rejections in this period — nothing to investigate" />
        </PanelCard>
      </div>
    </>
  );
}

/* ── Field user (MR / PSR / manager) analytics ───────────────────────────── */
function PersonalAnalytics({ days, setDays, roi, monthly }) {
  const [tab, setTab] = useState('campaigns');
  const d = roi.data || {};
  const t = d.totals || {};
  const prev = d.prev || {};
  const seesOthers = d.scope === 'team' || d.scope === 'all';
  const hasReports = (d.members || []).length > 1;

  const statusSegs = [
    { label: 'verified', value: t.verified || 0, color: 'green' },
    { label: 'pending', value: t.pending || 0, color: 'amber' },
    { label: 'rejected', value: t.rejected || 0, color: 'red' },
  ].filter((s) => s.value > 0);

  const breakdowns = {
    campaigns: { rows: d.campaigns || [], label: (r) => r.name, color: 'blue',
      sub: (r) => `${fmtNum(r.pobs)} POBs · ${rate(r.verified, r.pobs)}% verified` },
    chemists: { rows: d.chemists || [], label: (r) => r.shop_name || r.name, color: 'teal',
      sub: (r) => `${fmtNum(r.pobs)} POBs · ${[r.city, r.state].filter(Boolean).join(', ') || '—'}` },
    products: { rows: d.products || [], label: (r) => r.name, color: 'amber',
      sub: (r) => `${fmtNum(r.pobs)} POBs${r.sku ? ' · ' + r.sku : ''}` },
    ...(hasReports ? { members: { rows: d.members || [], label: (r) => r.full_name, color: 'green',
      sub: (r) => `${fmtNum(r.pobs)} POBs · ${r.level_name || r.region || '—'}` } } : {}),
  };
  const active = breakdowns[tab] || breakdowns.campaigns;
  const sorted = (active.rows || []).slice().sort((a, b) => (b.amount || 0) - (a.amount || 0));

  const insights = [];
  if (t.approval_rate != null) {
    insights.push({
      tone: t.approval_rate >= 80 ? 'green' : 'amber', icon: '✓',
      title: `${t.approval_rate}% of decided submissions were approved`,
      detail: t.approval_rate >= 80 ? 'Your invoice quality is strong.'
        : 'Clearer invoice photos are the fastest way to lift this.',
    });
  }
  if ((t.pending || 0) > 0) {
    insights.push({
      tone: 'amber', icon: '◷',
      title: `${fmtNum(t.pending)} submissions still awaiting a decision`,
      detail: `${fmtShort((t.amount || 0) - (t.verified_value || 0))} of value not yet confirmed.`,
    });
  }
  const bestChemist = (d.chemists || []).slice().sort((a, b) => (b.amount || 0) - (a.amount || 0))[0];
  if (bestChemist) {
    insights.push({
      tone: 'teal', icon: '◉',
      title: `${bestChemist.shop_name || bestChemist.name} is your strongest outlet`,
      detail: `${fmtShort(bestChemist.amount)} across ${fmtNum(bestChemist.pobs)} submissions.`,
    });
  }
  if ((t.rewards_paid || 0) > 0) {
    insights.push({
      tone: 'primary', icon: '🎁',
      title: `${fmtShort(t.rewards_paid)} in rewards paid out`,
      detail: `${fmtShort(t.rewards_eligible)} eligible in total across ${fmtNum(t.rewards_count)} awards.`,
    });
  }

  return (
    <>
      <AnalyticsHead
        title={seesOthers ? 'My team analytics' : 'My analytics'}
        subtitle={seesOthers
          ? 'How your team is performing, and which accounts drive the value.'
          : 'How your submissions convert, and which accounts drive your value.'}
        scope={d.scope} days={days} setDays={setDays} />

      <div className="metric-grid">
        <MetricTile label="POB value" value={fmtShort(t.amount)} icon="₹" tone="primary"
          delta={pctDelta(t.amount, prev.amount, days)}
          sub={`${fmtNum(t.pobs)} submissions`} />
        <MetricTile label="Verified value" value={fmtShort(t.verified_value)} icon="✓" tone="green"
          delta={pctDelta(t.verified_value, prev.verified_value, days)}
          sub={`${t.approval_rate ?? 0}% approval rate`} />
        <MetricTile label="Rewards earned" value={fmtShort(t.rewards_paid)} icon="🎁" tone="teal"
          delta={pctDelta(t.rewards_paid, prev.rewards_paid, days)}
          sub={`${fmtNum(t.rewards_count)} awards`} />
        <MetricTile label="Active campaigns" value={fmtNum(t.campaigns)} icon="◎" tone="amber"
          sub={`${fmtNum((d.chemists || []).length)} chemists covered`} />
      </div>

      <div className="bi-grid">
        <PanelCard title="My submission trend" span="2"
          sub="Volume submitted each month">
          {monthly.length ? (
            <BarChart labels={monthly.map((m) => monthLabel(m.month))}
              series={[{ label: 'POBs', values: monthly.map((m) => m.count ?? m.pobs), color: 'blue' }]}
              fmt={fmtNum} height={230} />
          ) : <div className="chart-empty">No monthly data yet</div>}
        </PanelCard>

        <PanelCard title="Conversion funnel" sub="Submitted → verified → rewarded">
          <ConversionFunnel totals={t} />
        </PanelCard>

        <PanelCard title="Outcome mix" sub="Where your submissions stand">
          {statusSegs.length ? (
            <div className="donut-panel">
              <DonutChart segments={statusSegs} size={150} thickness={22}
                centerValue={fmtNum(t.pobs)} centerLabel="POBs" />
              <DonutLegend segments={statusSegs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">Nothing submitted yet</div>}
        </PanelCard>

        <PanelCard title="Top contributors" span="2"
          sub="Ranked by POB value in this period"
          action={<Tabs items={Object.keys(breakdowns).map((k) => ({
            value: k, label: k.charAt(0).toUpperCase() + k.slice(1),
          }))} active={tab} onChange={setTab} />}>
          <RankList rows={sorted} getLabel={active.label} getValue={(r) => r.amount}
            getSub={active.sub} fmt={fmtShort} limit={10} color={active.color}
            empty="Nothing recorded in this period" />
        </PanelCard>

        <PanelCard title="What this tells you" span="2" sub="Read-outs from the numbers above">
          <InsightList items={insights} empty="Not enough activity yet" />
        </PanelCard>
      </div>
    </>
  );
}

export default function Analytics() {
  const [days, setDays] = useState(0);
  const roi = useAsync(() => api(`/api/v1/analytics/roi?days=${days}`), [days]);
  const summary = useAsync(() => api(`/api/v1/analytics/summary?days=${days}`), [days]);

  const monthly = useMemo(() => {
    const own = (summary.data || {}).own || {};
    return (own.monthly || []).slice()
      .sort((a, b) => String(a.month).localeCompare(String(b.month)));
  }, [summary.data]);

  const role = (getSession()?.user?.role || '').toLowerCase();
  const isAdmin = role === 'company_admin' || role === 'division_admin';
  const isAgent = role === 'verification_agent' || role === 'verifier';

  if (roi.loading) return <Spinner label="Crunching your analytics…" />;
  if (roi.error) return <ErrorBox error={roi.error} onRetry={roi.run} />;

  if (isAgent) return <AgentAnalytics days={days} setDays={setDays} roi={roi} />;
  if (isAdmin) return <CompanyAnalytics days={days} setDays={setDays} roi={roi} />;
  return <PersonalAnalytics days={days} setDays={setDays} roi={roi} monthly={monthly} />;
}
