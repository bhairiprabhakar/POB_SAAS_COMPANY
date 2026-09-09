import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, fmtDate, fmtMoney } from '../../api';
import {
  ErrorBox, PageHeader, SearchBox, Spinner, StatusBadge, useAsync,
} from '../../ui';

export default function Campaigns() {
  const nav = useNavigate();
  const { data, loading, error, run } = useAsync(() => api('/api/v1/campaigns/tracking'));
  const [q, setQ] = useState('');
  const [expanded, setExpanded] = useState(null);

  const groups = useMemo(() => {
    if (!data?.items) return [];
    if (!q) return data.items;
    const n = q.toLowerCase();
    const out = [];
    for (const g of data.items) {
      const campaigns = g.campaigns.filter((c) =>
        c.name?.toLowerCase().includes(n)
        || c.brand_name?.toLowerCase().includes(n) || c.division_name?.toLowerCase().includes(n));
      if (campaigns.length) out.push({ ...g, campaigns });
    }
    return out;
  }, [data, q]);

  if (loading) return <Spinner label="Loading campaigns…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      <PageHeader title="Campaigns" subtitle="Divisions, campaigns and the team hierarchy under each"
        actions={<SearchBox value={q} onChange={setQ} />} />
      <div className="track-legend">
        <span className="lg-num"><b>#</b> POBs</span>
        <span className="lg-num money"><b>₹</b> Value</span>
        <span className="lg-num ok"><b>✓</b> Verified</span>
        <span className="lg-num warn"><b>◌</b> Pending</span>
        <span className="lg-num bad"><b>✕</b> Rejected</span>
      </div>
      {groups.length === 0 && <div className="empty-state">No campaigns yet</div>}
      {groups.map((g) => (
        <section key={g.division.id ?? 'none'} className="division-group">
          <h2 className="division-head">{g.division.name}
            <span className="muted">{g.campaigns.length} campaign{g.campaigns.length === 1 ? '' : 's'}</span>
          </h2>
          {g.campaigns.map((c) => (
            <CampaignCard key={c.id} c={c} expanded={expanded === c.id}
              onToggle={() => setExpanded(expanded === c.id ? null : c.id)}
                onOpen={() => nav('/app/masters/campaigns/' + c.id)} />
          ))}
        </section>
      ))}
    </div>
  );
}

function CampaignCard({ c, expanded, onToggle, onOpen }) {
  return (
    <div className={`camp-card${expanded ? ' open' : ''}`}>
      <div className="camp-head" onClick={onToggle}>
        <div className="camp-main">
          <strong>{c.name}</strong>
          <span className="muted">
            {(c.brand_names?.length ? c.brand_names.join(' + ') : (c.brand_name || '—'))}
            {' · '}{c.division_name || c.division || '—'}
          </span>
        </div>
        <div className="camp-meta">
          <span className="camp-dates">{fmtDate(c.start_date)} → {fmtDate(c.end_date)}</span>
          <StatusBadge value={c.status} />
          <span>{c.product_count} products</span>
          <span>{c.pob_count} POBs</span>
        </div>
        <span className="camp-chev">{expanded ? '▾' : '▸'}</span>
      </div>
      {expanded && (
        <div className="camp-body">
          <MemberTree members={c.members} />
          <div className="camp-actions">
            <button className="btn btn-sm" onClick={onOpen}>View details</button>
          </div>
        </div>
      )}
    </div>
  );
}

function MemberTree({ members }) {
  const byParent = useMemo(() => {
    const map = {};
    members.forEach((m) => {
      if (m.parent_id && members.some((x) => x.id === m.parent_id)) {
        (map[m.parent_id] = map[m.parent_id] || []).push(m);
      }
    });
    return map;
  }, [members]);
  const roots = useMemo(() =>
    members.filter((m) => !m.parent_id || !byParent[m.parent_id]), [members, byParent]);
  const node = (m) => (
    <li key={m.id}>
      <div className="member-row">
        <span className="member-name">
          {m.full_name}
          <em>{m.level_name || m.role_name || 'member'}</em>
        </span>
        <span className="member-stats">
          <span className="lg-num"><b>{m.pobs || 0}</b></span>
          <span className="lg-num money"><b>{fmtMoney(m.amount)}</b></span>
          <span className="lg-num ok"><b>{m.verified || 0}</b></span>
          <span className="lg-num warn"><b>{m.pending || 0}</b></span>
          <span className="lg-num bad"><b>{m.rejected || 0}</b></span>
        </span>
      </div>
      {byParent[m.id] && <ul>{byParent[m.id].map(node)}</ul>}
    </li>
  );
  return <ul className="member-tree">{roots.map(node)}</ul>;
}
