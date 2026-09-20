import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, fmtDate, getSession } from '../../api';
import {
  DashHero, DonutChart, DonutLegend, ErrorBox, Icon, InsightList, LineChart, MetricTile,
  PanelCard, PeriodSelect, ProgressBar, Spinner, StatusBadge, Table, useAsync,
} from '../../ui';
import HomeAdmin from './AdminDashboard';
import AgentDashboard from './AgentDashboard';

const PERIODS = [
  { days: 0, label: 'All time' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
];

const STATUS_TONE = {
  verified: 'green', approved: 'green', completed: 'green', paid: 'green',
  pending: 'amber', pending_verification: 'amber', submitted: 'amber', eligible: 'amber',
  rejected: 'red', duplicate: 'red', needs_review: 'red',
};

const fmtShort = (n) => {
  const v = Number(n) || 0;
  if (v >= 1e7) return '₹' + (v / 1e7).toFixed(2) + ' Cr';
  if (v >= 1e5) return '₹' + (v / 1e5).toFixed(2) + ' L';
  if (v >= 1e3) return '₹' + (v / 1e3).toFixed(1) + 'k';
  return '₹' + Math.round(v).toLocaleString('en-IN');
};
const fmtNum = (n) => (Number(n) || 0).toLocaleString('en-IN');
/* "All time" has no preceding window, so a delta there would be meaningless —
   return null and MetricTile falls back to the plain sub-label. */
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
const greeting = () => {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
};

function PersonalDashboard() {
  const [days, setDays] = useState(0);
  const summary = useAsync(() => api('/api/v1/analytics/summary?days=' + days), [days]);
  const mine = useAsync(() => api('/api/v1/pob/mine?limit=10'), []);

  const d = summary.data || {};
  const own = d.own || {};
  const ownPrev = d.own_prev || {};
  const team = d.team || {};
  const teamPrev = d.team_prev || {};
  const members = d.members || [];
  const me = d.me || {};
  const today = own.today || {};
  const isManager = d.scope !== 'self' && members.length > 0;

  const recent = useMemo(() => {
    const r = mine.data;
    const list = Array.isArray(r) ? r : (r?.items || []);
    return list.slice(0, 8);
  }, [mine.data]);

  const monthly = useMemo(
    () => (own.monthly || []).slice().sort((a, b) => String(a.month).localeCompare(String(b.month))),
    [own]);

  const statusSegs = useMemo(() => Object.entries(own.by_status || {})
    .map(([k, v]) => ({ label: k.replaceAll('_', ' '), value: v, color: STATUS_TONE[k] || 'gray' }))
    .filter((s) => s.value > 0), [own]);

  const insights = useMemo(() => {
    const out = [];
    const dPobs = pctDelta(own.pobs, ownPrev.pobs, days);
    if (dPobs != null && Math.abs(dPobs) >= 1) {
      const up = dPobs > 0;
      out.push({
        tone: up ? 'green' : 'amber', icon: up ? '↑' : '↓',
        title: `Your submissions are ${up ? 'up' : 'down'} ${Math.abs(dPobs).toFixed(1)}%`,
        detail: up ? 'Keep the momentum going this period.' : 'Below your previous period — worth a push.',
      });
    }
    if ((own.pending || 0) > 0) {
      out.push({
        tone: 'amber', icon: '◷',
        title: `${fmtNum(own.pending)} submission${own.pending > 1 ? 's are' : ' is'} awaiting verification`,
        detail: 'No action needed from you — the verification team is reviewing these.',
      });
    }
    if ((own.rejected || 0) > 0) {
      out.push({
        tone: 'red', icon: '!',
        title: `${fmtNum(own.rejected)} submission${own.rejected > 1 ? 's were' : ' was'} rejected`,
        detail: 'Open each one to see the reason and resubmit with corrected proof.',
      });
    }
    if (own.approval_rate != null) {
      const good = own.approval_rate >= 80;
      out.push({
        tone: good ? 'green' : 'teal', icon: '✓',
        title: `Your approval rate is ${own.approval_rate}%`,
        detail: good ? 'Well above the bar — your invoice quality is strong.'
          : 'Attach clearer invoices to lift this figure.',
      });
    }
    if ((own.gratifications || 0) > 0) {
      out.push({
        tone: 'primary', icon: '🎁',
        title: `${fmtNum(own.gratifications)} gratification${own.gratifications > 1 ? 's' : ''} earned`,
        detail: 'Track payout status on the Gratification page.',
      });
    }
    return out;
  }, [own, ownPrev, days]);

  const recentCols = [
    {
      key: 'id',
      label: 'Submission',
      render: (r) => (
        <Link to="/app/pob/mine" className="cell-link">
          <strong>{r.invoice_number || `POB #${r.id}`}</strong>
          <span className="muted cell-sub">{r.chemist_name || r.shop_name || '—'}</span>
        </Link>
      ),
    },
    { key: 'campaign_name', label: 'Campaign', render: (r) => r.campaign_name || <span className="muted">—</span> },
    { key: 'created_at', label: 'Submitted', render: (r) => fmtDate(r.created_at) },
    { key: 'pob_amount', label: 'Value', render: (r) => <strong>{fmtShort(r.pob_amount)}</strong> },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
  ];

  const memberCols = [
    { key: 'full_name', label: 'Team member', render: (r) => (
      <span className="cell-link"><strong>{r.full_name}</strong>
        <span className="muted cell-sub">{r.level_name || r.region || '—'}</span></span>
    ) },
    { key: 'pobs', label: 'POBs', render: (r) => fmtNum(r.pobs) },
    { key: 'amount', label: 'POB value', render: (r) => <strong>{fmtShort(r.amount)}</strong> },
    { key: 'verified', label: 'Verified', render: (r) => <span style={{ color: 'var(--green)' }}>{fmtNum(r.verified)}</span> },
    { key: 'approval_rate', label: 'Approval', render: (r) => (
      <ProgressBar value={r.approval_rate || 0}
        tone={(r.approval_rate || 0) >= 75 ? 'green' : (r.approval_rate || 0) >= 50 ? 'amber' : 'red'} />
    ) },
  ];

  if (summary.loading) return <Spinner label="Loading your dashboard…" />;
  if (summary.error) return <ErrorBox error={summary.error} onRetry={summary.run} />;

  return (
    <div>
      <DashHero
        title={`${greeting()}, ${me.full_name || me.username || 'there'} 👋`}
        subtitle={isManager
          ? `Your performance plus ${members.length} team member${members.length > 1 ? 's' : ''}.`
          : "Here's how your field activity is tracking."}
        actions={<PeriodSelect value={days} onChange={setDays} options={PERIODS} />}
      />

      <div className="metric-grid">
        <MetricTile label="POBs submitted" value={fmtNum(own.pobs)} icon="≣" tone="primary"
          delta={pctDelta(own.pobs, ownPrev.pobs, days)}
          sub={`${fmtNum(own.verified)} verified · ${fmtNum(own.pending)} pending`} />
        <MetricTile label="POB value" value={fmtShort(own.amount)} icon="₹" tone="green"
          delta={pctDelta(own.amount, ownPrev.amount, days)}
          sub={`Invoice value ${fmtShort(own.invoice_value)}`} />
        <MetricTile label="Verified" value={fmtNum(own.verified)} icon="✓" tone="teal"
          delta={pctDelta(own.verified, ownPrev.verified, days)}
          sub={`${own.approval_rate ?? 0}% approval rate`} />
        <MetricTile label="Rejected" value={fmtNum(own.rejected)} icon="✕" tone="red"
          delta={pctDelta(own.rejected, ownPrev.rejected, days)} goodWhen="down"
          sub={own.rejected ? 'Resubmit with corrected proof' : 'Nothing rejected'} />
      </div>

      <div className="metric-grid">
        <MetricTile label="Awaiting verification" value={fmtNum(own.pending)} icon="◷" tone="amber"
          sub="With the verification team" />
        <MetricTile label="Approval rate" value={(own.approval_rate ?? 0) + '%'} icon="◎" tone="green"
          sub={`${fmtNum(own.verified)} of ${fmtNum((own.verified || 0) + (own.rejected || 0))} decided`} />
        <MetricTile label="Chemist visits" value={fmtNum(own.visits)} icon={<Icon name="calendar" size={19} />} tone="blue"
          sub="Logged this period" />
        <MetricTile label="Gratifications" value={fmtNum(own.gratifications)} icon={<Icon name="gift" size={19} />} tone="indigo"
          sub={`${fmtNum(own.gratifications_pending)} pending · ${fmtNum(own.gratifications_completed)} completed`} />
      </div>

      <PanelCard title="Today at a glance" sub="Your field activity so far today" className="today-glance">
        <div className="metric-grid">
          <MetricTile label="POBs today" value={fmtNum(today.pobs)} icon="≣" tone="primary" sub={`${fmtNum(today.verified)} verified · ${fmtNum(today.pending)} pending`} />
          <MetricTile label="Value today" value={fmtShort(today.amount)} icon="₹" tone="green" sub="Submitted today" />
          <MetricTile label="Chemist visits today" value={fmtNum(today.visits)} icon="📅" tone="blue" sub="Visits logged" />
          <MetricTile label="Rejected today" value={fmtNum(today.rejected)} icon="✕" tone="red" sub={today.rejected ? 'Resubmit with corrected proof' : 'Nothing rejected'} />
        </div>
      </PanelCard>

      <div className="bi-grid">
        <PanelCard title="My submission trend" sub="POBs you submitted per month">
          <LineChart
            labels={monthly.map((m) => monthLabel(m.month))}
            series={[{ label: 'POBs', values: monthly.map((m) => m.count ?? m.pobs), color: 'blue' }]}
            fmt={fmtNum}
            height={210}
          />
        </PanelCard>

        <PanelCard title="My status mix" sub="Where your submissions stand"
          action={<Link className="btn-link" to="/app/pob/mine">View all →</Link>}>
          {statusSegs.length ? (
            <div className="donut-panel">
              <DonutChart segments={statusSegs} size={158} thickness={22}
                centerValue={fmtNum(own.pobs)} centerLabel="total" />
              <DonutLegend segments={statusSegs} fmt={fmtNum} />
            </div>
          ) : <div className="chart-empty">Nothing submitted yet</div>}
        </PanelCard>

        <PanelCard title="Recent submissions" span="2"
          sub="Your latest POB entries and where each one sits"
          action={<Link className="btn btn-sm btn-primary" to="/app/pob/submit">Submit POB</Link>}>
          {mine.error
            ? <ErrorBox error={mine.error} onRetry={mine.run} />
            : <Table cols={recentCols} rows={recent} keyOf={(r) => r.id}
                empty="You haven't submitted anything yet" />}
        </PanelCard>

        {isManager && (
          <PanelCard title="Team performance" span="2"
            sub={`${members.length} direct report${members.length > 1 ? 's' : ''} · ${fmtShort(team.amount)} team POB value`}
            action={<Link className="btn-link" to="/app/analytics">Full analytics →</Link>}>
            <Table cols={memberCols}
              rows={members.slice().sort((a, b) => (b.amount || 0) - (a.amount || 0)).slice(0, 10)}
              keyOf={(r) => r.id} empty="No team members found" />
          </PanelCard>
        )}

        <PanelCard title={<span><span className="ai-dot">✦</span>Top insights</span>} span="2"
          sub="What to act on next">
          <InsightList items={insights} empty="Nothing needs your attention" />
        </PanelCard>
      </div>

      <div className="quick-actions">
        <Link to="/app/pob/submit" className="btn btn-primary">Submit POB</Link>
        <Link to="/app/pob/invoice" className="btn">Submit Invoice</Link>
        <Link to="/app/chemists/register" className="btn">Register Chemist</Link>
        <Link to="/app/visits" className="btn">Chemist Visits</Link>
        <Link to="/app/gratification" className="btn">Gratification</Link>
      </div>
    </div>
  );
}

export default function Home() {
  const role = (getSession()?.user?.role || '').toLowerCase();
  if (role === 'company_admin' || role === 'division_admin') return <HomeAdmin />;
  if (role === 'verification_agent' || role === 'verifier') return <AgentDashboard />;
  return <PersonalDashboard />;
}
