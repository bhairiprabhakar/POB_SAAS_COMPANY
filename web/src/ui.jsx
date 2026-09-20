// Shared UI primitives: layout shell, data table, modal, form fields,
// stat cards, badges, toasts and a tiny data-fetching hook.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom';
import { api, fmtDateTime, fmtMoney, getSession, saRole, tenantLoginPath } from './api';

// ── Toasts ─────────────────────────────────────────────────────────────────

let toastId = 0;
const listeners = new Set();

export function toast(message, tone = 'info') {
  listeners.forEach((fn) => fn({ id: ++toastId, message, tone }));
}

export function Toaster() {
  const [items, setItems] = useState([]);
  useEffect(() => {
    const fn = (t) => {
      setItems((prev) => [...prev, t]);
      setTimeout(() => setItems((prev) => prev.filter((x) => x.id !== t.id)), 4200);
    };
    listeners.add(fn);
    return () => listeners.delete(fn);
  }, []);
  return (
    <div className="toaster">
      {items.map((t) => (
        <div key={t.id} className={`toast toast-${t.tone}`}>{t.message}</div>
      ))}
    </div>
  );
}

// ── Data fetching hook ─────────────────────────────────────────────────────

export function useAsync(fn, deps = []) {
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const [tick, setTick] = useState(0);
  const run = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    let alive = true;
    setState({ loading: true, error: null, data: null });
    fn()
      .then((data) => alive && setState({ loading: false, error: null, data }))
      .catch((err) => alive && setState({ loading: false, error: err.message, data: null }));
    return () => { alive = false; };
  }, [tick, ...deps]);
  return { ...state, run };
}

export function Spinner({ label }) {
  return (
    <div className="spinner-wrap">
      <div className="spinner" />
      {label && <span>{label}</span>}
    </div>
  );
}

// ── Skeleton loading: keep the page's structure visible while data loads ──

export function Skeleton({ w = '100%', h = 14, r = 6, className = '', style }) {
  return <div className={`skeleton ${className}`} style={{ width: w, height: h, borderRadius: r, ...style }} aria-hidden="true" />;
}

export function TableSkeleton({ cols = 6, rows = 6 }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>{Array.from({ length: cols }).map((_, i) => <th key={i}><Skeleton h={10} w="72%" /></th>)}</tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }).map((_, r) => (
            <tr key={r}>
              {Array.from({ length: cols }).map((_, c) => (
                <td key={c}><Skeleton h={13} w={c === 0 ? '55%' : c % 2 ? '86%' : '70%'} /></td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StatSkeleton({ n = 3 }) {
  return (
    <div className="stats-grid compact">
      {Array.from({ length: n }).map((_, i) => (
        <div className="stat-card" key={i}>
          <Skeleton h={12} w={96} />
          <div style={{ marginTop: 8 }}><Skeleton h={22} w={64} /></div>
        </div>
      ))}
    </div>
  );
}

export function ErrorBox({ error, onRetry }) {
  return (
    <div className="error-box">
      <strong>Error</strong>
      <span>{error}</span>
      {onRetry && <button className="btn" onClick={onRetry}>Retry</button>}
    </div>
  );
}

export function EmptyState({ text = 'Nothing here yet' }) {
  return <div className="empty-state">{text}</div>;
}

// ── Badges ─────────────────────────────────────────────────────────────────

// ── Icon system ──────────────────────────────────────────────────────────
// A small, self-contained set of outline icons (Lucide-style: 24x24, 1.8px
// stroke, round caps/joins) so nav/metric/insight chips render a consistent
// monochrome mark that inherits the tone color via currentColor — instead
// of color emoji, which renders inconsistently across OSes and breaks the
// flat, tinted icon-chip system everywhere else in the UI.
const ICON_PATHS = {
  users: <>
    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
    <path d="M16 3.13a4 4 0 0 1 0 7.75" />
  </>,
  user: <>
    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
    <circle cx="12" cy="7" r="4" />
  </>,
  gift: <>
    <rect x="3" y="8" width="18" height="4" rx="1" />
    <path d="M12 8v13" />
    <path d="M19 12v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7" />
    <path d="M7.5 8a2.5 2.5 0 0 1 0-5C11 3 12 8 12 8" />
    <path d="M16.5 8a2.5 2.5 0 0 0 0-5C13 3 12 8 12 8" />
  </>,
  'trending-up': <>
    <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" />
    <polyline points="16 7 22 7 22 13" />
  </>,
  'file-text': <>
    <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" />
    <polyline points="14 2 14 8 20 8" />
    <line x1="16" y1="13" x2="8" y2="13" />
    <line x1="16" y1="17" x2="8" y2="17" />
    <line x1="10" y1="9" x2="8" y2="9" />
  </>,
  bell: <>
    <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
    <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
  </>,
  lock: <>
    <rect x="3" y="11" width="18" height="11" rx="2" />
    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
  </>,
  settings: <>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </>,
  clipboard: <>
    <rect x="8" y="2" width="8" height="4" rx="1" />
    <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
  </>,
  receipt: <>
    <path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z" />
    <path d="M8 7h8" /><path d="M8 11h8" /><path d="M8 15h5" />
  </>,
  calendar: <>
    <rect x="3" y="4" width="18" height="18" rx="2" />
    <line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" /><line x1="3" y1="10" x2="21" y2="10" />
  </>,
  wallet: <>
    <path d="M21 12V7H5a2 2 0 0 1 0-4h14v4" />
    <path d="M3 5v14a2 2 0 0 0 2 2h16v-5" />
    <path d="M18 12a2 2 0 0 0 0 4h4v-4Z" />
  </>,
  mail: <>
    <rect x="2" y="4" width="20" height="16" rx="2" />
    <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" />
  </>,
  'map-pin': <>
    <path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z" />
    <circle cx="12" cy="10" r="3" />
  </>,
  building: <>
    <rect x="4" y="2" width="16" height="20" rx="1" />
    <path d="M9 22v-4h6v4" />
    <path d="M8 6h.01" /><path d="M12 6h.01" /><path d="M16 6h.01" />
    <path d="M8 10h.01" /><path d="M12 10h.01" /><path d="M16 10h.01" />
    <path d="M8 14h.01" /><path d="M12 14h.01" /><path d="M16 14h.01" />
  </>,
  home: <>
    <path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    <polyline points="9 22 9 12 15 12 15 22" />
  </>,
  'user-plus': <>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <line x1="19" y1="8" x2="19" y2="14" />
    <line x1="22" y1="11" x2="16" y2="11" />
  </>,
  megaphone: <>
    <path d="m3 11 18-5v12L3 14v-3z" />
    <path d="M11.6 16.8a3 3 0 1 1-5.8-1.6" />
  </>,
  'shield-check': <>
    <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
    <path d="m9 12 2 2 4-4" />
  </>,
  layers: <>
    <path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z" />
    <path d="m22 12-9.17 4.16a2 2 0 0 1-1.66 0L2 12" />
    <path d="m22 17.5-9.17 4.16a2 2 0 0 1-1.66 0L2 17.5" />
  </>,
  package: <>
    <path d="M16.5 9.4 7.55 4.24" />
    <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
    <path d="M3.3 7 12 12l8.7-5" />
    <path d="M12 22V12" />
  </>,
  percent: <>
    <line x1="19" y1="5" x2="5" y2="19" />
    <circle cx="6.5" cy="6.5" r="2.5" />
    <circle cx="17.5" cy="17.5" r="2.5" />
  </>,
  grid: <>
    <rect x="3" y="3" width="7" height="7" />
    <rect x="14" y="3" width="7" height="7" />
    <rect x="14" y="14" width="7" height="7" />
    <rect x="3" y="14" width="7" height="7" />
  </>,
};

export function Icon({ name, size = 16, className = '' }) {
  const body = ICON_PATHS[name];
  if (!body) return null;
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
      className={className} style={{ display: 'block' }}>
      {body}
    </svg>
  );
}

const TONES = {
  active: 'green', approved: 'green', verified: 'green', delivered: 'green',
  completed: 'green', paid: 'green', redeemed: 'green', sent: 'blue',
  pending: 'amber', pending_verification: 'amber', eligible: 'amber',
  needs_review: 'amber', generated: 'amber', dispatched: 'blue',
  rejected: 'red', duplicate: 'red', inactive: 'gray', disabled: 'gray',
  draft: 'gray', paused: 'gray', provisioning: 'amber', trial: 'amber',
  submitted: 'amber', default: 'gray', green: 'green',
  pending_approval: 'amber', scheduled: 'blue',
  // Identity entries so Badge/StatusBadge can also be called directly with
  // a color name (tone="indigo", tone="ai") rather than a status keyword.
  amber: 'amber', red: 'red', blue: 'blue', gray: 'gray',
  indigo: 'indigo', ai: 'ai',
};

export function Badge({ children, tone }) {
  const t = TONES[tone] || TONES[tone?.toLowerCase()] || TONES.default;
  return <span className={`badge badge-${t}`}>{children}</span>;
}

export function StatusBadge({ value }) {
  const label = String(value || '').replaceAll('_', ' ').replaceAll('pending_verification', 'pending verification');
  return <Badge tone={String(value || '').toLowerCase()}>{label}</Badge>;
}

// ── Layout ─────────────────────────────────────────────────────────────────

const PERM_GATE = {
  '/app': 'dashboard.view',
  '/app/admin': 'dashboard.view',
  '/app/pob/submit': 'pob.submit',
  '/app/pob/mine': 'pob.submit',
  '/app/pob/invoice': 'pob.submit',
  '/app/pob': 'pob.view',
  '/app/verification': 'verification.view',
  '/app/gratification': 'gratification.view',
  '/app/visits': 'visit.view',
  '/app/reports': 'report.view',
  '/app/notifications': 'notification.view',
  '/app/chemists': 'chemist.view',
  '/app/chemists/register': 'chemist.manage',
  '/app/audit': 'audit.view',
  '/app/security': 'apikey.view',
  '/app/jobs': 'job.view',
};

function hasPerm(perms, perm) {
  if (!perm) return true;
  return Array.isArray(perms) && perms.includes(perm);
}

const SA_ROLE_LABELS = {
  owner: 'Owner',
  full: 'Super Admin',
  campaign_admin: 'Campaign Admin',
  finance_admin: 'Finance Admin',
  verification_admin: 'Verification Admin',
};

function saRoleLabel(role) {
  return SA_ROLE_LABELS[role] || 'Admin';
}

// Tenant role/designation labels are open-ended (each company can rename its
// hierarchy — README notes HO→NSM→ZSM→RSM→ASM→MR is only the default), so
// rather than a fixed lookup this formats on shape: short bare-letter slugs
// (mr, asm, rsm…) are field designations → uppercase as an acronym; longer
// or underscored slugs (company_admin, verification_agent) are role
// names → humanized to Title Case. Exported so any page rendering a raw
// role/designation string formats it the same way instead of showing the
// lowercase DB slug as-is.
export function roleLabel(role) {
  if (!role) return '—';
  if (/^[a-z]+$/.test(role) && role.length <= 4) return role.toUpperCase();
  return role.split('_').map((w) => w ? w[0].toUpperCase() + w.slice(1) : w).join(' ');
}

// ── Verification status badge (nav bar) ─────────────────────────────────────
export function useMyPobStats() {
  const session = getSession();
  const canSubmit = session?.kind === 'tenant' && (session.permissions || []).includes('pob.submit');
  const [stats, setStats] = useState({ pending: 0, approved: 0, rejected: 0, submitted: 0 });
  const load = useCallback(() => {
    if (!canSubmit) return;
    api('/api/v1/pob/my-stats').then(setStats).catch(() => {});
  }, [canSubmit]);
  useEffect(() => {
    load();
    const iv = setInterval(load, 30000);
    return () => clearInterval(iv);
  }, [load]);
  return { stats, reload: load };
}

function TenantSidebar({ session, onLogout, stats, onNavigate, open, collapsed, onToggleCollapse }) {
  const p = session.permissions || [];
  const isDataEntry = !!session.user?.data_entry;
  const company = session.division || {};
  const companyLogo = company.logo_path ? `/api/v1/auth/division-logo/${company.id}` : null;
  const role = (session.user?.role || '').toLowerCase();
  const isVerifier = role === 'verification_agent' || role === 'verifier';
  const isAdmin = role === 'company_admin' || role === 'division_admin';
  const badges = {
    '/app/pob/mine': { n: stats?.pending || 0, tone: 'amber', title: 'POBs pending verification' },
    '/app/verification': { n: stats?.pending || 0, tone: 'amber', title: 'Verification queue' },
  };
  const canSee = (i) => hasPerm(p, i.perm) && (!i.entry || isDataEntry);

  // Statement extraction (Batch 3): tenant upload / verify / credits wallet.
  const statementNav = [
    { to: '/app/statements', label: 'Statements', icon: <Icon name="file-text" />, perm: 'statement.view', group: 'Statements', tone: 'teal' },
    { to: '/app/statements/verify', label: 'Statement Verify', icon: <Icon name="shield-check" />, perm: 'statement.verify', group: 'Statements', tone: 'blue' },
    { to: '/app/statements/credits', label: 'Statement Credits', icon: <Icon name="wallet" />, perm: 'statement.credits', group: 'Statements', tone: 'amber' },
  ];

  // Role-based navigation: three distinct experiences.
  // `group` drives the section headers, `tone` the coloured icon tile.
  const items = isVerifier ? [
    // ── Verification Agent ──
    { to: '/app', label: 'Dashboard', icon: <Icon name="grid" />, perm: 'dashboard.view', end: true, group: 'Core Modules', tone: 'primary' },
    { to: '/app/verification', label: 'Verification Queue', icon: <Icon name="shield-check" />, perm: 'verification.view', group: 'Core Modules', tone: 'blue' },
    { to: '/app/pob', label: 'All POB Records', icon: <Icon name="layers" />, perm: 'pob.view', group: 'Core Modules', tone: 'teal' },
    { to: '/app/analytics', label: 'Analytics', icon: <Icon name="trending-up" />, perm: 'dashboard.view', group: 'Core Modules', tone: 'green' },
    { to: '/app/notifications', label: 'Notifications', icon: <Icon name="bell" />, perm: 'notification.view', group: 'Quick Access', tone: 'amber' },
    { to: '/app/profile', label: 'My Profile', icon: <Icon name="user" />, group: 'Quick Access', tone: 'gray' },
    ...statementNav,
  ] : isAdmin ? [
    // ── Division Admin / HO ──
    { to: '/app/admin', label: 'Dashboard', icon: <Icon name="grid" />, perm: 'dashboard.view', end: true, group: 'Core Modules', tone: 'primary' },
    { to: '/app/my-division', label: 'My Division', icon: <Icon name="layers" />, perm: 'dashboard.view', group: 'Core Modules', tone: 'teal' },

    { to: '/app/user-management', label: 'Employees', icon: <Icon name="users" />, perm: 'user.view', group: 'Employees', tone: 'blue' },

    { to: '/app/catalog', label: 'Catalog', icon: <Icon name="package" />, perm: 'brand.view', group: 'Master Data', tone: 'amber' },
    { to: '/app/chemists', label: 'Chemists', icon: <Icon name="users" />, perm: 'chemist.view', group: 'Master Data', tone: 'blue' },
    { to: '/app/regions', label: 'Coverage', icon: <Icon name="map-pin" />, perm: 'user.view', group: 'Master Data', tone: 'green' },
    { to: '/app/gifts', label: 'Gifts', icon: <Icon name="gift" />, perm: 'gratification.manage', group: 'Master Data', tone: 'indigo' },

    { to: '/app/campaigns', label: 'All Campaigns', icon: <Icon name="megaphone" />, perm: 'campaign.view', group: 'Campaigns', tone: 'teal' },
    { to: '/app/masters/campaigns', label: 'Campaign Tracking', icon: <Icon name="layers" />, perm: 'campaign.view', group: 'Campaigns', tone: 'indigo' },

    { to: '/app/pob', label: 'All POB', icon: <Icon name="layers" />, perm: 'pob.view', group: 'POB', tone: 'blue' },

    { to: '/app/verification', label: 'Verification', icon: <Icon name="shield-check" />, perm: 'verification.view', group: 'Operations', tone: 'green' },
    { to: '/app/gratification', label: 'Gratification', icon: <Icon name="gift" />, perm: 'gratification.view', group: 'Operations', tone: 'indigo' },
    { to: '/app/analytics', label: 'Analytics', icon: <Icon name="trending-up" />, perm: 'dashboard.view', group: 'Operations', tone: 'red' },
    { to: '/app/reports', label: 'Reports', icon: <Icon name="file-text" />, perm: 'report.view', group: 'Operations', tone: 'teal' },

    { to: '/app/notifications', label: 'Notifications', icon: <Icon name="bell" />, perm: 'notification.view', group: 'Quick Access', tone: 'amber' },
    { to: '/app/audit', label: 'Audit', icon: <Icon name="file-text" />, perm: 'audit.view', group: 'Quick Access', tone: 'gray' },
    { to: '/app/security', label: 'Security', icon: <Icon name="lock" />, perm: 'apikey.view', group: 'Quick Access', tone: 'green' },
    { to: '/app/jobs', label: 'Background Jobs', icon: <Icon name="settings" />, perm: 'job.view', group: 'Quick Access', tone: 'amber' },
    ...statementNav,
  ] : [
    // ── Campaign Users (PSR / ASM / RSM / SM / MR / etc.) ──
    { to: '/app', label: 'Dashboard', icon: <Icon name="grid" />, perm: 'dashboard.view', end: true, group: 'Core Modules', tone: 'primary' },
    { to: '/app/chemists/register', label: 'Register Chemist', icon: <Icon name="user-plus" />, perm: 'chemist.manage', entry: true, group: 'Core Modules', tone: 'green' },
    { to: '/app/campaigns', label: 'Campaigns', icon: <Icon name="megaphone" />, perm: 'campaign.view', group: 'Core Modules', tone: 'teal' },
    { to: '/app/pob/submit', label: 'Submit POB', icon: <Icon name="clipboard" />, perm: 'pob.submit', group: 'Core Modules', tone: 'blue' },
    { to: '/app/pob/invoice', label: 'Submit Invoice', icon: <Icon name="receipt" />, perm: 'pob.submit', group: 'Core Modules', tone: 'green' },
    { to: '/app/pob/mine', label: 'My Submissions', icon: <Icon name="clipboard" />, perm: 'pob.submit', group: 'Core Modules', tone: 'teal' },
    { to: '/app/chemists', label: 'Chemists', icon: <Icon name="users" />, perm: 'chemist.view', group: 'Core Modules', tone: 'amber' },
    { to: '/app/gratification', label: 'Gratification', icon: <Icon name="gift" />, perm: 'gratification.view', group: 'Core Modules', tone: 'indigo' },
    { to: '/app/analytics', label: 'Analytics', icon: <Icon name="trending-up" />, perm: 'dashboard.view', group: 'Core Modules', tone: 'green' },
    { to: '/app/visits', label: 'Chemist Visits', icon: <Icon name="calendar" />, perm: 'visit.view', entry: true, group: 'Quick Access', tone: 'blue' },
    { to: '/app/notifications', label: 'Notifications', icon: <Icon name="bell" />, perm: 'notification.view', group: 'Quick Access', tone: 'amber' },
    { to: '/app/profile', label: 'My Profile', icon: <Icon name="user" />, group: 'Quick Access', tone: 'gray' },
    ...statementNav,
  ];
  return (
    <aside className={`sidebar${open ? ' open' : ''}${collapsed ? ' collapsed' : ''}`}>
      <div className="brand">
        {companyLogo
          ? <img src={companyLogo} className="brand-logo" alt={`${company.name} logo`}
              onError={(e) => { e.currentTarget.style.display = 'none'; }} />
          : <>
              <span className="brand-mark">{(company.name || 'F').charAt(0)}</span>
              <div>
                <strong>{company.name || 'FieldNet'}</strong>
                <small>{company.code || ''}</small>
              </div>
            </>}
        <SideCollapse collapsed={collapsed} onToggle={onToggleCollapse} />
      </div>
      <SideNav items={items.filter(canSee)} badges={badges} onNavigate={onNavigate} />
    </aside>
  );
}

/* Grouped navigation list: renders a collapsible section header for every
   group, so even single-page sections stay titled and tidy. Sections start
   collapsed except the one containing the active page. */
function SideNav({ items, badges = {}, onNavigate }) {
  const { pathname } = useLocation();
  const groups = [];
  for (const i of items) {
    const label = i.group || '';
    const last = groups[groups.length - 1];
    if (!last || last.label !== label) groups.push({ label, items: [i] });
    else last.items.push(i);
  }
  const matchActive = (i) =>
    (i.end ? pathname === i.to : pathname === i.to || (i.to.endsWith('/') && pathname.startsWith(i.to)) || pathname.startsWith(i.to + '/'));
  return (
    <nav>
      {groups.map((g) => (
        <SideGroup key={g.label} label={g.label} items={g.items}
          badges={badges} onNavigate={onNavigate}
          defaultOpen={g.items.some(matchActive)} />
      ))}
    </nav>
  );
}

function SideLink({ item, badge, onNavigate }) {
  return (
    <NavLink to={item.to} end={item.end} onClick={onNavigate} title={item.label}
      className={({ isActive }) => `side-link ${isActive ? 'active' : ''}`}>
      <span className="side-icon" data-tone={item.tone || 'gray'}>{item.icon}</span>
      <span className="side-label">{item.label}</span>
      {badge && badge.n > 0 && <span className={`side-badge ${badge.tone}`} title={badge.title}>{badge.n}</span>}
    </NavLink>
  );
}

function SideGroup({ label, items, badges, onNavigate, defaultOpen }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={`side-section ${open ? 'open' : 'closed'}`}>
      <button type="button" className="side-group-btn" onClick={() => setOpen((o) => !o)}
        aria-expanded={open} title={label}>
        <span className="side-group-label">{label}</span>
        <span className={`side-chevron${open ? ' open' : ''}`}>▾</span>
      </button>
      {items.map((i) => (
        <SideLink key={i.to} item={i} badge={badges[i.to]} onNavigate={onNavigate} />
      ))}
    </div>
  );
}

function SideCollapse({ collapsed, onToggle }) {
  if (!onToggle) return null;
  return (
    <button type="button" className="side-collapse" onClick={onToggle}
      title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
      <span className="side-collapse-icon">{collapsed ? '›' : '‹'}</span>
</button>
  );
}

function SuperSidebar({ onNavigate, open, collapsed, onToggleCollapse }) {
  const items = [
    { to: '/superadmin', label: 'Dashboard', icon: <Icon name="grid" />, end: true, group: 'Overview', tone: 'primary', roles: ['owner', 'full', 'campaign_admin', 'finance_admin', 'verification_admin', 'division_admin'] },

    { to: '/superadmin/company', label: 'Company Profile', icon: <Icon name="building" />, group: 'Company', tone: 'blue', roles: ['owner', 'full'] },

    { to: '/superadmin/divisions', label: 'All Divisions', icon: <Icon name="layers" />, group: 'Divisions', tone: 'blue', roles: ['owner', 'full', 'division_admin'] },

    { to: '/superadmin/users', label: 'All Employees', icon: <Icon name="users" />, group: 'Employees & Hierarchy', tone: 'blue', roles: ['owner', 'full'] },

    { to: '/superadmin/campaigns', label: 'Campaigns', icon: <Icon name="megaphone" />, group: 'Campaigns', tone: 'teal', roles: ['campaign_admin'] },

    { to: '/superadmin/pob', label: 'POB Operations', icon: <Icon name="clipboard" />, group: 'POB Operations', tone: 'blue', roles: ['verification_admin'] },

    { to: '/superadmin/gratification', label: 'Gratification', icon: <Icon name="gift" />, group: 'Gratification', tone: 'amber', roles: ['owner', 'full', 'finance_admin'] },

    { to: '/superadmin/finance', label: 'Finance & Payouts', icon: <Icon name="wallet" />, group: 'Gratification', tone: 'green', roles: ['owner', 'full', 'finance_admin'] },

    { to: '/superadmin/analytics', label: 'Analytics', icon: <Icon name="trending-up" />, group: 'Analytics', tone: 'primary', roles: ['owner', 'full', 'campaign_admin'] },
    { to: '/superadmin/costing', label: 'ROI', icon: <Icon name="percent" />, group: 'Analytics', tone: 'green', roles: ['owner', 'full'] },

    { to: '/superadmin/admins', label: 'Platform Admins', icon: <Icon name="lock" />, group: 'Platform', tone: 'primary', roles: ['owner'] },
    { to: '/superadmin/audit', label: 'Audit Logs', icon: <Icon name="file-text" />, group: 'Platform', tone: 'gray', roles: ['owner', 'full'] },
    { to: '/superadmin/platform-settings', label: 'Platform Settings', icon: <Icon name="settings" />, group: 'Platform', tone: 'gray', roles: ['owner', 'full'] },
    { to: '/superadmin/ai-models', label: 'AI Models', icon: <Icon name="percent" />, group: 'Platform', tone: 'blue', roles: ['owner', 'full'] },
  ];
  const role = saRole();
  const visible = items.filter((i) => !i.roles || i.roles.includes(role));
  const [brand, setBrand] = useState({ platform_name: 'FieldNet', has_logo: false });
  const [queue, setQueue] = useState(null);
  useEffect(() => {
    api('/api/v1/auth/platform-branding').then(setBrand).catch(() => {});
  }, []);
  useEffect(() => {
    let alive = true;
    const load = () => api('/api/v1/superadmin/queue-counts')
      .then((d) => alive && setQueue(d)).catch(() => {});
    load();
    const iv = setInterval(load, 60000);
    return () => { alive = false; clearInterval(iv); };
  }, []);
  const badgeFor = (to) => {
    if (!queue) return null;
    const m = {
      '/superadmin/campaigns': queue.campaigns?.['pending_approval'] || 0,
      '/superadmin/pob': queue.pob?.['pending_verification'] || 0,
      '/superadmin/gratification': queue.gratification?.eligible || 0,
    }[to];
    const titles = {
      '/superadmin/campaigns': 'campaigns awaiting approval',
      '/superadmin/pob': 'POBs pending verification',
      '/superadmin/gratification': 'gratifications eligible for payout',
    }[to];
    return m > 0 ? { n: m, tone: 'red', title: titles } : null;
  };
  const badges = {};
  for (const i of items) { const b = badgeFor(i.to); if (b) badges[i.to] = b; }
  return (
    <aside className={`sidebar${open ? ' open' : ''}${collapsed ? ' collapsed' : ''}`}>
      <div className="brand">
        {brand.has_logo
          ? <img src="/api/v1/auth/platform-logo" alt={brand.platform_name} className="brand-logo" />
          : <span className="brand-mark">{(brand.platform_name || 'F').charAt(0)}</span>}
        {/* The logo already carries the brand name -- avoid repeating it
            next to the image (matches the tenant sidebar's pattern). */}
        <div><strong>{brand.has_logo ? 'Platform Console' : brand.platform_name}</strong>
          {!brand.has_logo && <small>Platform Console</small>}</div>
        <SideCollapse collapsed={collapsed} onToggle={onToggleCollapse} />
      </div>
      <SideNav items={visible} badges={badges} onNavigate={onNavigate} />
    </aside>
  );
}

function SaBell({ unread, onDrained }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState(null);
  const ref = useRef(null);
  useEffect(() => {
    const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);
  const openAsync = async () => {
    const next = !open;
    setOpen(next);
    if (next) {
      try {
        const d = await api('/api/v1/superadmin/notifications');
        setItems(d.items || []);
      } catch { setItems([]); }
    }
  };
  const markAll = async () => {
    try { await api('/api/v1/superadmin/notifications/read-all', { method: 'POST' }); } catch {}
    if (items) setItems(items.map((n) => ({ ...n, is_read: true })));
    onDrained();
  };
  return (
    <div className="sa-bell" ref={ref}>
      <button className="bell" onClick={openAsync} title="Notifications" aria-label="Notifications">
        <Icon name="bell" size={17} />{unread > 0 && <span className="bell-dot">{unread > 99 ? '99+' : unread}</span>}
      </button>
      {open && (
        <div className="sa-bell-panel">
          <div className="sa-bell-head">
            <strong>Platform notifications</strong>
            <button className="btn-link" onClick={markAll} disabled={unread === 0}>Mark all read</button>
          </div>
          <div className="sa-bell-list">
            {items === null && <p className="muted">Loading…</p>}
            {items && items.length === 0 && <p className="muted">No notifications yet.</p>}
            {items && items.map((n) => (
              <Link key={n.id} to={n.link || '/superadmin'} className={`sa-notif ${n.is_read ? '' : 'unread'}`}
                onClick={async () => {
                  if (!n.is_read) {
                    try { await api(`/api/v1/superadmin/notifications/${n.id}/read`, { method: 'POST' }); } catch {}
                    onDrained();
                  }
                  setOpen(false);
                }}>
                <div className="sa-notif-head">
                  <strong>{n.title}</strong>
                  {!n.is_read && <span className="badge badge-blue">new</span>}
                </div>
                <div>{n.message}</div>
                <div className="muted sa-notif-time">{fmtDateTime(n.created_at)}</div>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function AppShell({ children, kind }) {
  const session = getSession();
  const navigate = useNavigate();
  const [unread, setUnread] = useState(0);
  const [menu, setMenu] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('pob_sidebar_collapsed') === '1');
  const { stats } = useMyPobStats();

  const toggleCollapse = useCallback(() => {
    setCollapsed((c) => {
      localStorage.setItem('pob_sidebar_collapsed', c ? '0' : '1');
      return !c;
    });
  }, []);

  const loadUnread = useCallback(() => {
    const ep = kind === 'sa'
      ? '/api/v1/superadmin/notifications/unread-count'
      : '/api/v1/notifications/unread-count';
    api(ep).then((d) => setUnread(d.unread)).catch(() => {});
  }, [kind]);
  useEffect(() => {
    loadUnread();
    const iv = setInterval(loadUnread, 30000);
    return () => clearInterval(iv);
  }, [loadUnread]);

  const closeMenu = useCallback(() => setMenu(false), []);

  const logout = async () => {
    const s = getSession();
    const ep = s?.kind === 'sa' ? '/api/v1/auth/superadmin/logout' : '/api/v1/auth/logout';
    const body = s?.kind === 'sa'
      ? { refresh_token: s.refresh }
      : { refresh_token: s.refresh, division_slug: s?.division?.code };
    try { await api(ep, { method: 'POST', body }); } catch { /* ignore */ }
    const dest = kind === 'sa' ? '/superadmin-login' : tenantLoginPath(s);
    localStorage.removeItem('pob_saas_session');
    navigate(dest);
  };

  return (
    <div className="shell">
      {kind === 'sa'
        ? <SuperSidebar onNavigate={closeMenu} open={menu} collapsed={collapsed} onToggleCollapse={toggleCollapse} />
        : <TenantSidebar session={session} onLogout={logout} stats={stats} onNavigate={closeMenu} open={menu}
            collapsed={collapsed} onToggleCollapse={toggleCollapse} />}
      {menu && <div className="side-backdrop" onClick={closeMenu} />}
      <div className="main-col">
        <header className="topbar">
          <div className="topbar-left">
            <button className="menu-btn" onClick={() => setMenu((v) => !v)} aria-label="Toggle navigation">☰</button>
            <div className="topbar-title">
              {kind === 'sa' ? `Platform Console · ${saRoleLabel(session?.user?.role)}` : `${session?.division?.code || ''} · ${roleLabel(session?.user?.role)}`}
            </div>
          </div>
          <div className="topbar-right">
            {kind === 'tenant' && (
              <>
                {stats.pending > 0 && (
                  <Link to="/app/pob/mine" className="ver-chip" title="Your POBs awaiting verification">
                    <span className="ver-chip-dot" /> {stats.pending} pending
                  </Link>
                )}
                {session.user?.data_entry && (
                  <Link to="/app/visits" className="topbar-qitem" title="Chemist Visits"><Icon name="calendar" size={16} /></Link>
                )}
                <Link to="/app/notifications" className="bell" title="Notifications">
                  <Icon name="bell" size={17} />{unread > 0 && <span className="bell-dot">{unread > 99 ? '99+' : unread}</span>}
                </Link>
              </>
            )}
            {kind === 'sa' && (
              <SaBell unread={unread} onDrained={loadUnread} />
            )}
            <Link to={kind === 'sa' ? '/superadmin/profile' : '/app/profile'} className="user-chip" title="View profile">
              <span className="user-avatar">{(session?.user?.full_name || session?.user?.username || '?')[0]?.toUpperCase()}</span>
              <span className="user-chip-name">{session?.user?.full_name || session?.user?.username}</span>
            </Link>
            <button className="btn btn-ghost btn-sm" onClick={logout}>Logout</button>
          </div>
        </header>
        <main className="content">{children}</main>
      </div>
    </div>
  );
}

// ── Page header / stat cards ───────────────────────────────────────────────

export function PageHeader({ title, subtitle, actions }) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function StatCard({ label, value, sub, icon, tone }) {
  return (
    <div className="stat-card" data-tone={tone || 'default'}>
      {icon && <div className="stat-icon">{icon}</div>}
      <div>
        <div className="stat-label">{label}</div>
        <div className="stat-value">{value}</div>
        {sub && <div className="stat-sub">{sub}</div>}
      </div>
    </div>
  );
}

// BI-style KPI card: label, big value, inline sparkline and a vs-period delta.
export function KpiCard({ label, value, icon, tone = 'blue', spark = [], delta, sub }) {
  return (
    <div className="kpi-card" data-tone={tone}>
      <div className="kpi-head">
        <span className="kpi-label">{label}</span>
        {icon && <span className="kpi-icon">{icon}</span>}
      </div>
      <div className="kpi-value">{value}</div>
      {(spark.length > 0 || delta) && (
        <div className="kpi-foot">
          {spark.length > 0 && <Sparkline values={spark} tone={tone} height={30} />}
          {delta && <span className="kpi-delta">{delta}</span>}
        </div>
      )}
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

/* ── Dashboard primitives ──────────────────────────────────────────────────
   The building blocks every role dashboard is assembled from: a greeting
   banner, metric tiles with a real period-over-period delta, panel cards with
   a header action, progress bars and an insight feed. */

export function DashHero({ title, subtitle, actions, art = true }) {
  return (
    <section className="dash-hero">
      <div className="dash-hero-copy">
        <h2>{title}</h2>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {art && <HeroArt />}
      {actions && <div className="dash-hero-actions">{actions}</div>}
    </section>
  );
}

/* Inline, theme-aware decoration. Drawn rather than shipped as an image so it
   recolours with the palette and costs no extra request. */
function HeroArt() {
  return (
    <svg className="dash-hero-art" viewBox="0 0 220 96" aria-hidden="true" focusable="false">
      <rect x="6" y="60" width="26" height="30" rx="4" fill="var(--primary)" opacity=".18" />
      <rect x="38" y="44" width="26" height="46" rx="4" fill="var(--primary)" opacity=".30" />
      <rect x="70" y="26" width="26" height="64" rx="4" fill="var(--primary)" opacity=".45" />
      <rect x="102" y="50" width="26" height="40" rx="4" fill="var(--teal)" opacity=".35" />
      <rect x="134" y="34" width="26" height="56" rx="4" fill="var(--green)" opacity=".35" />
      <path d="M12 54 L51 38 L83 20 L115 44 L147 28 L186 14" fill="none"
        stroke="var(--primary)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" opacity=".8" />
      <circle cx="186" cy="14" r="5" fill="var(--primary)" />
      <circle cx="115" cy="44" r="3.5" fill="var(--teal)" />
      <circle cx="51" cy="38" r="3.5" fill="var(--green)" />
    </svg>
  );
}

/* value + optional delta. `delta` is a percentage number; pass null when the
   API gives no comparable prior period so we never show an invented trend. */
export function MetricTile({ label, value, icon, tone = 'blue', delta = null,
                             deltaLabel = 'vs previous period', sub, goodWhen = 'up' }) {
  const has = delta !== null && delta !== undefined && Number.isFinite(Number(delta));
  const n = Number(delta);
  const dir = !has || Math.abs(n) < 0.05 ? 'flat' : n > 0 ? 'up' : 'down';
  const good = dir === 'flat' ? 'flat' : (dir === 'up') === (goodWhen === 'up') ? 'up' : 'down';
  return (
    <div className="metric-tile" data-tone={tone}>
      {icon && <span className="metric-icon">{icon}</span>}
      <div className="metric-body">
        <div className="metric-label">{label}</div>
        <div className="metric-value">{value}</div>
        {has ? (
          <div className="metric-foot">
            <span className={`delta ${good}`}>
              {dir === 'up' ? '↑' : dir === 'down' ? '↓' : '·'} {Math.abs(n).toFixed(1)}%
            </span>
            <span className="metric-since">{deltaLabel}</span>
          </div>
        ) : sub ? <div className="metric-foot"><span className="metric-since">{sub}</span></div> : null}
      </div>
    </div>
  );
}

export function PanelCard({ title, sub, action, children, span, className = '' }) {
  return (
    <section className={`bi-card${span ? ' span-' + span : ''} ${className}`.trim()}>
      <div className="bi-card-head">
        <div><h3>{title}</h3>{sub && <span className="muted">{sub}</span>}</div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function ProgressBar({ value = 0, tone = 'blue', showLabel = true }) {
  const pct = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
  return (
    <div className="progress-row">
      {showLabel && <span className="progress-num">{pct}%</span>}
      <span className="progress-track" role="img" aria-label={`${pct} percent`}>
        <span className="progress-fill" data-tone={tone} style={{ width: `${pct}%` }} />
      </span>
    </div>
  );
}

export function InsightList({ items = [], empty = 'Nothing noteworthy yet' }) {
  if (!items.length) return <EmptyState text={empty} />;
  return (
    <ul className="insight-list">
      {items.map((it, i) => (
        <li key={i} className="insight-row">
          <span className="insight-icon" data-tone={it.tone || 'blue'}>{it.icon || '●'}</span>
          <div>
            <div className="insight-title">{it.title}</div>
            {it.detail && <div className="insight-detail">{it.detail}</div>}
          </div>
        </li>
      ))}
    </ul>
  );
}

/* Period selector shaped like the mockup's "This Week" control. */
export function PeriodSelect({ value, onChange, options }) {
  return (
    <select className="period-select" value={value} onChange={(e) => onChange(Number(e.target.value))}>
      {options.map((o) => <option key={o.days} value={o.days}>{o.label}</option>)}
    </select>
  );
}

export function MiniBarChart({ rows, getLabel, getValue }) {
  const max = Math.max(1, ...rows.map((r) => Number(getValue(r)) || 0));
  return (
    <div className="mini-chart">
      {rows.map((r, i) => (
        <div key={i} className="mini-bar-row">
          <span className="mini-bar-label">{getLabel(r)}</span>
          <div className="mini-bar-track">
            <div className="mini-bar-fill" style={{ width: `${(Number(getValue(r)) || 0) / max * 100}%` }} />
          </div>
          <span className="mini-bar-value">{fmtMoney(getValue(r))}</span>
        </div>
      ))}
    </div>
  );
}

// ── BI chart kit (SVG, dependency-free) ─────────────────────────────────────
// Small reusable visuals used by the dashboard / analytics pages: sparklines,
// hoverable line charts, donuts and horizontal bar rankings.

// "goodWhen" controls which direction is green: 'up' for amounts/approvals,
// 'down' for TAT / rejection rates.
export function Delta({ value, suffix = '%', goodWhen = 'up', className = '' }) {
  const v = Number(value);
  if (value == null || !Number.isFinite(v)) return null;
  if (v === 0) return <span className={`delta flat ${className}`}>—</span>;
  const up = v > 0;
  const good = goodWhen === 'down' ? !up : up;
  return (
    <span className={`delta ${good ? 'up' : 'down'} ${className}`}
      title={goodWhen === 'down' ? 'Lower is better' : 'Higher is better'}>
      {up ? '▲' : '▼'}{Math.abs(v).toFixed(1)}{suffix}
    </span>
  );
}

export function Sparkline({ values = [], tone = 'blue', height = 36 }) {
  const data = values.map(Number).filter(Number.isFinite);
  if (data.length === 0) return null;
  const w = 120, h = height;
  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const px = (i) => (data.length === 1 ? w / 2 : (i / (data.length - 1)) * w);
  const py = (v) => h - 3 - ((v - min) / range) * (h - 6);
  const pts = data.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(' ');
  const last = data[data.length - 1];
  return (
    <svg className={`spark c-${tone}`} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" width="100%" height={h}
      aria-hidden="true">
      <polyline className="spark-line" points={pts} fill="none" vectorEffect="non-scaling-stroke" />
      <circle className="spark-dot" cx={px(data.length - 1).toFixed(1)} cy={py(last).toFixed(1)} r="2.4" />
    </svg>
  );
}

// Multi-series line/area chart. series = [{ name, values, color }] where color
// is a design tone key (green/amber/red/blue/teal/gray). Hover shows a guide
// + tooltip with every series value at that point.
export function LineChart({ labels = [], series = [], fmt = (n) => n, height = 200 }) {
  const plotRef = useRef(null);
  const [hover, setHover] = useState(null);
  const n = labels.length;
  const all = series.flatMap((s) => (s.values || []).map(Number)).filter(Number.isFinite);
  if (n === 0) return <div className="chart-empty">No data yet</div>;
  const max = Math.max(1, ...all) * 1.1;
  const pct = (i) => (n === 1 ? 50 : (i / (n - 1)) * 100);
  const pctY = (v) => Math.max(0, Math.min(100, 100 - (Number(v) || 0) / max * 100));
  const ticks = 4;
  const yTicks = Array.from({ length: ticks + 1 }, (_, t) => (max / ticks) * t);
  const step = Math.max(1, Math.ceil(n / 6));
  const xShown = labels.map((l, i) => (i % step === 0 || i === n - 1 ? { i, l } : null)).filter(Boolean);

  const onMove = (e) => {
    const rect = plotRef.current?.getBoundingClientRect();
    if (!rect) return;
    const p = Math.max(0, Math.min(100, (e.clientX - rect.left) / rect.width * 100));
    setHover(Math.round(p / (100 / (n - 1 || 1))));
  };

  return (
    <div className="lc" style={{ ['--lc-h']: `${height}px` }}>
      <div className="lc-y">
        {yTicks.map((t) => (
          <span key={t} style={{ bottom: `${(t / max) * 100}%` }}>{fmt(Number(t.toFixed(1)))}</span>
        ))}
      </div>
      <div className="lc-plot" ref={plotRef} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="lc-svg">
          {yTicks.map((t) => (
            <line key={t} x1="0" x2="100" y1={100 - (t / max) * 100} y2={100 - (t / max) * 100} className="lc-grid" />
          ))}
          {series.map((s) => {
            const pts = (s.values || []).map((v, i) => `${pct(i).toFixed(2)},${pctY(v).toFixed(2)}`).join(' ');
            return (
              <g key={s.name}>
                <polygon className={`lc-area c-${s.color || 'blue'}`} points={`0,100 ${pts} 100,100`} />
                <polyline className={`lc-line c-${s.color || 'blue'}`} points={pts}
                  vectorEffect="non-scaling-stroke" />
              </g>
            );
          })}
          {hover != null && (
            <line x1={pct(hover)} x2={pct(hover)} y1="0" y2="100" className="lc-guide" />
          )}
        </svg>
        {hover != null && (
          <div className="lc-tip" style={{ left: `${pct(hover)}%` }}>
            <strong>{labels[hover]}</strong>
            {series.map((s) => (
              <div key={s.name} className="lc-tip-row">
                <span className={`dot c-${s.color || 'blue'}`} />
                <span>{s.name}</span>
                <strong>{fmt(Number(s.values?.[hover]) || 0)}</strong>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="lc-x">
        {xShown.map(({ i, l }) => (
          <span key={i} style={{ left: `${pct(i)}%` }}>{l}</span>
        ))}
      </div>
    </div>
  );
}

// Donut (status / distribution mix). segments = [{label, value, color}].
export function DonutChart({ segments = [], size = 150, thickness = 20, centerValue, centerLabel }) {
  const total = segments.reduce((s, x) => s + (Number(x.value) || 0), 0);
  if (total <= 0) return <div className="chart-empty">No data yet</div>;
  const R = (size - thickness) / 2;
  const C = 2 * Math.PI * R;
  let acc = 0;
  return (
    <div className="donut-wrap">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle className="donut-track" cx={size / 2} cy={size / 2} r={R} strokeWidth={thickness} />
        {segments.map((s, i) => {
          const len = ((Number(s.value) || 0) / total) * C;
          const el = (
            <circle key={i} className={`donut-seg c-${s.color || 'blue'}`} cx={size / 2} cy={size / 2} r={R}
              strokeWidth={thickness} strokeDasharray={`${Math.max(0, len - 1.5)} ${C}`}
              strokeDashoffset={-acc} transform={`rotate(-90 ${size / 2} ${size / 2})`} />
          );
          acc += len;
          return el;
        })}
      </svg>
      {(centerLabel || centerValue != null) && (
        <div className="donut-center">
          {centerValue != null && <strong>{centerValue}</strong>}
          {centerLabel && <span>{centerLabel}</span>}
        </div>
      )}
    </div>
  );
}

// Legend used next to donuts.
export function DonutLegend({ segments = [], fmt = (n) => n }) {
  const total = segments.reduce((s, x) => s + (Number(x.value) || 0), 0);
  return (
    <div className="donut-legend">
      {segments.map((s) => (
        <div key={s.label} className="dl-row">
          <span className={`dot c-${s.color || 'blue'}`} />
          <span className="dl-label">{s.label}</span>
          <strong>{fmt(s.value)}</strong>
          <em>{total ? Math.round((Number(s.value) || 0) / total * 100) : 0}%</em>
        </div>
      ))}
    </div>
  );
}

// Horizontal bar ranking (top campaigns / brands / regions, leaderboards).
export function HBarChart({ rows = [], getLabel, getValue, fmt = (n) => n, limit = 8, color = 'blue' }) {
  const data = rows.slice(0, limit);
  const max = Math.max(1, ...data.map((r) => Number(getValue(r)) || 0));
  return (
    <div className="hbar">
      {data.length === 0 && <div className="chart-empty">No data yet</div>}
      {data.map((r, i) => (
        <div className="hbar-row" key={i}>
          <span className="hbar-label" title={getLabel(r)}>{getLabel(r)}</span>
          <div className="hbar-track">
            <div className={`hbar-fill c-${color}`} style={{ width: `${(Number(getValue(r)) || 0) / max * 100}%` }} />
          </div>
          <span className="hbar-value">{fmt(getValue(r))}</span>
        </div>
      ))}
    </div>
  );
}

// ── Modal ──────────────────────────────────────────────────────────────────

/* ── Vertical bar chart (grouped or stacked) ───────────────────────────────
   The one chart shape the kit was missing. Same axis/grid furniture as
   LineChart so the two read as one family. */
export function BarChart({ labels = [], series = [], stacked = false, fmt = (n) => n,
                           height = 220, showLegend = true }) {
  const [hover, setHover] = useState(null);
  const n = labels.length;
  if (!n || !series.length) return <div className="chart-empty">No data yet</div>;

  const totals = labels.map((_, i) =>
    stacked ? series.reduce((s, se) => s + (Number(se.values?.[i]) || 0), 0)
            : Math.max(...series.map((se) => Number(se.values?.[i]) || 0)));
  const max = Math.max(1, ...totals) * 1.12;
  const ticks = 4;
  const step = Math.max(1, Math.ceil(n / 8));

  return (
    <div className="bc" style={{ '--bc-h': height + 'px' }}>
      <div className="bc-y">
        {Array.from({ length: ticks + 1 }, (_, t) => {
          const v = (max / ticks) * (ticks - t);
          return <span key={t} style={{ top: `${(t / ticks) * 100}%` }}>{fmt(Math.round(v))}</span>;
        })}
      </div>
      <div className="bc-plot" onMouseLeave={() => setHover(null)}>
        <div className="bc-grid" aria-hidden="true">
          {Array.from({ length: ticks + 1 }, (_, t) => <i key={t} style={{ top: `${(t / ticks) * 100}%` }} />)}
        </div>
        <div className="bc-bars">
          {labels.map((lab, i) => (
            <div key={i} className={'bc-slot' + (hover === i ? ' is-hover' : '')}
              onMouseEnter={() => setHover(i)}>
              <div className={'bc-stack' + (stacked ? ' stacked' : '')}>
                {series.map((se, si) => {
                  const v = Number(se.values?.[i]) || 0;
                  return (
                    <span key={si} className={`bc-bar c-${se.color || 'blue'}`}
                      style={{ height: `${Math.max(v <= 0 ? 0 : 1.5, (v / max) * 100)}%` }}
                      title={`${se.label}: ${fmt(v)}`} />
                  );
                })}
              </div>
            </div>
          ))}
        </div>
        {hover !== null && (
          <div className="lc-tip" style={{ left: `${((hover + 0.5) / n) * 100}%` }}>
            <strong>{labels[hover]}</strong>
            {series.map((se, si) => (
              <span key={si} className="lc-tip-row">
                <span><i className={`dot c-${se.color || 'blue'}`} />{se.label}</span>
                <strong>{fmt(Number(se.values?.[hover]) || 0)}</strong>
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="bc-x">
        {labels.map((l, i) => (i % step === 0 || i === n - 1
          ? <span key={i} style={{ left: `${((i + 0.5) / n) * 100}%` }}>{l}</span> : null))}
      </div>
      {showLegend && series.length > 1 && (
        <div className="bc-legend">
          {series.map((se, si) => (
            <span key={si}><i className={`dot c-${se.color || 'blue'}`} />{se.label}</span>
          ))}
        </div>
      )}
    </div>
  );
}

/* Ranked breakdown: label, share bar, value — the workhorse for top-N views. */
export function RankList({ rows = [], getLabel, getValue, getSub, fmt = (n) => n,
                           limit = 8, color = 'blue', empty = 'No data yet' }) {
  const data = rows.slice(0, limit);
  if (!data.length) return <div className="chart-empty">{empty}</div>;
  const max = Math.max(1, ...data.map((r) => Number(getValue(r)) || 0));
  return (
    <ol className="rank-list">
      {data.map((r, i) => {
        const v = Number(getValue(r)) || 0;
        return (
          <li key={i} className="rank-row">
            <span className="rank-num">{i + 1}</span>
            <div className="rank-body">
              <div className="rank-head">
                <span className="rank-label" title={getLabel(r)}>{getLabel(r)}</span>
                <strong className="rank-value">{fmt(v)}</strong>
              </div>
              <span className="rank-track">
                <span className={`rank-fill c-${color}`} style={{ width: `${(v / max) * 100}%` }} />
              </span>
              {getSub && <div className="rank-sub">{getSub(r)}</div>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

export function Modal({ open, onClose, title, children, wide, xwide, full, footer }) {
  if (!open) return null;
  return (
    <div className={`modal-overlay${full ? ' full' : ''}`} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? 'wide' : ''}${xwide ? ' xwide' : ''}${full ? ' full' : ''}`}>
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="modal-x" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

// ── Form fields ────────────────────────────────────────────────────────────

export function Field({ label, required, hint, children, ...rest }) {
  return (
    <label className="field" {...rest}>
      <span className="field-label">{label}{required && <em>*</em>}</span>
      {children}
      {hint && <small className="field-hint">{hint}</small>}
    </label>
  );
}

export function TextInput(props) {
  const [showPw, setShowPw] = useState(false);
  if (props.type === 'password') {
    const { type, ...rest } = props;
    return (
      <div className="pw-wrap">
        <input {...rest} type={showPw ? 'text' : 'password'}
          className={`input ${props.className || ''}`} />
        <button type="button" className="pw-eye" tabIndex={-1}
          aria-label={showPw ? 'Hide password' : 'Show password'}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => setShowPw((v) => !v)}>{showPw ? '🙈' : '👁'}</button>
      </div>
    );
  }
  return <input {...props} className={`input ${props.className || ''}`} />;
}

export function Select({ options = [], placeholder, ...props }) {
  return (
    <select {...props} className={`input ${props.className || ''}`}>
      {placeholder !== false && <option value="">{placeholder || '— select —'}</option>}
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

export function TextArea(props) {
  return <textarea {...props} className={`input ${props.className || ''}`} />;
}

// ── Table ──────────────────────────────────────────────────────────────────

export function Table({ cols, rows = [], keyOf, onRowClick, empty }) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>{cols.map((c) => <th key={c.key} className={c.thClass || ''}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={cols.length}><EmptyState text={empty || 'No records'} /></td></tr>
          )}
          {rows.map((row) => (
            <tr key={keyOf ? keyOf(row) : row.id} className={onRowClick ? 'clickable' : ''}
              onClick={onRowClick ? () => onRowClick(row) : undefined}>
              {cols.map((c) => (
                <td key={c.key} className={c.tdClass || ''}>
                  {c.render ? c.render(row) : row[c.key] ?? '—'}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SearchBox({ value, onChange, placeholder }) {
  return (
    <div className="search-box">
      <span>⌕</span>
      <input value={value} onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder || 'Search…'} />
    </div>
  );
}

export function Tabs({ items, active, onChange }) {
  return (
    <div className="tabs">
      {items.map((t) => (
        <button key={t.value} className={`tab ${active === t.value ? 'active' : ''}`}
          onClick={() => onChange(t.value)}>{t.label}</button>
      ))}
    </div>
  );
}

// ── File URL helper (auth-aware) ───────────────────────────────────────────

export function useFileUrl(rel) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    if (!rel) { setUrl(null); return; }
    let alive = true;
    api(`/api/v1/storage/${rel}`)
      .then((blob) => {
        if (alive) setUrl(URL.createObjectURL(blob));
      })
      .catch(() => { if (alive) setUrl(null); });
    return () => { alive = false; };
  }, [rel]);
  return url;
}

// ── Split-pane detail layout: proof (left) + details (right) ───────────────

export function SplitDetail({ left, right, className = '' }) {
  return (
    <div className={`split-detail ${className}`}>
      <div className="split-left">{left}</div>
      <div className="split-right">{right}</div>
    </div>
  );
}

// Two-column "label / value" table, used for campaign + POB detail summaries
// that used to render as a loose kv-grid -- a real <table> reads better
// alongside the product-line and extraction tables next to it.
export function DetailTable({ rows, title }) {
  const visible = rows.filter(Boolean);
  if (!visible.length) return null;
  return (
    <>
      {title && <h4 className="section-title">{title}</h4>}
      <div className="table-wrap">
        <table className="data-table detail-table">
          <tbody>
            {visible.map(([label, value], i) => (
              <tr key={i}><th>{label}</th><td>{value ?? '—'}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ── Detail hero: a colored summary banner for campaign / POB / verification
// detail views -- replaces the old plain label/value table with a visual
// summary (big amount, status, and a grid of fact pills). Tone tints the
// left accent and the amount.
export function DetailHero({ title, subtitle, badge, primaryLabel, primary, secondary, facts = [], tone = 'blue' }) {
  return (
    <div className="detail-hero" data-tone={tone}>
      <div className="detail-hero-top">
        <div className="detail-hero-title">
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
          {badge && <div className="detail-hero-badges">{badge}</div>}
        </div>
        {primary != null && (
          <div className="detail-hero-amount">
            <span className="stat-label">{primaryLabel}</span>
            <span className="detail-hero-value">{primary}</span>
            {secondary && <span className="stat-sub">{secondary}</span>}
          </div>
        )}
      </div>
      {facts.length > 0 && (
        <div className="detail-hero-facts">
          {facts.filter(Boolean).map(([label, value]) => (
            <div className="fact-pill" key={label}>
              <span>{label}</span>
              <strong>{value ?? '—'}</strong>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// AI confidence meter -- green/amber/red bar based on the extraction score.
export function AiConfidenceBar({ value }) {
  const pct = Math.round((Number(value) || 0) * 100);
  const cls = pct >= 90 ? 'high' : pct >= 60 ? 'mid' : 'low';
  return (
    <div className="conf-wrap">
      <div className="conf-head"><span>AI confidence</span><strong className={`conf-pct ${cls}`}>{pct}%</strong></div>
      <div className="conf-bar"><div className={`conf-fill ${cls}`} style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} /></div>
    </div>
  );
}

// AI-extracted invoice rendered like the document the AI actually read: a
// receipt-style card with header fields, a line-item table and a total --
// much easier to scan than a flat list of key/value pairs.
export function AiInvoiceCard({ fields = {}, items = [], confidence, title = 'AI-extracted invoice data' }) {
  const f = fields || {};
  const amtRaw = f.invoice_amount ?? f.total_amount ?? f.grand_total ?? f.amount;
  const amt = amtRaw != null && amtRaw !== '' ? Number(amtRaw) : null;
  const list = Array.isArray(items) ? items : [];
  const total = list.reduce((s, it) => s + (Number(it.amount) || 0), 0);
  const meta = [
    ['Invoice no.', f.invoice_number != null ? String(f.invoice_number) : ''],
    ['Date', f.invoice_date != null ? String(f.invoice_date) : ''],
    ['Party / chemist', f.chemist_name != null ? String(f.chemist_name) : ''],
  ];
  return (
    <div className="ai-invoice">
      <div className="ai-invoice-head">
        <div>
          <span className="ai-invoice-tag">AI EXTRACTION</span>
          <strong>{title}</strong>
        </div>
        <AiConfidenceBar value={confidence} />
      </div>
      <div className="ai-invoice-meta">
        {meta.map(([label, v]) => (
          <div className="kv-pair" key={label}><span>{label}</span><strong>{v || '—'}</strong></div>
        ))}
        <div className="kv-pair kv-total"><span>Total</span><strong>{amt != null ? fmtMoney(amt) : '—'}</strong></div>
      </div>
      {list.length > 0 ? (
        <table className="ai-invoice-table">
          <thead>
            <tr><th>#</th><th>Item</th><th>Qty</th><th>Rate</th><th>Amount</th></tr>
          </thead>
          <tbody>
            {list.map((it, i) => (
              <tr key={i}>
                <td className="ai-inv-idx">{i + 1}</td>
                <td>{it.description}</td>
                <td>{it.qty ?? 0}</td>
                <td>{fmtMoney(it.rate)}</td>
                <td><strong>{fmtMoney(it.amount)}</strong></td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr><td colSpan="4">Invoice total</td><td><strong>{fmtMoney(total)}</strong></td></tr>
          </tfoot>
        </table>
      ) : (
        <div className="empty-state">No line items extracted from the document.</div>
      )}
    </div>
  );
}

export function ProofPane({ url, name, hint = 'Proof document', empty = 'No document attached' }) {
  const isPdf = /\.pdf$/i.test(name || '');
  const [zoom, setZoom] = useState(1);
  const [size, setSize] = useState(null);
  const bodyRef = useRef(null);
  const dragRef = useRef(null);
  const [panning, setPanning] = useState(false);
  useEffect(() => { setZoom(1); setSize(null); }, [url]);
  useEffect(() => {
    if (!url || isPdf) return;
    const img = new Image();
    img.onload = () => setSize({ w: img.naturalWidth, h: img.naturalHeight });
    img.src = url;
  }, [url, isPdf]);

  const zoomIn = () => setZoom((z) => Math.min(3, +(z + 0.25).toFixed(2)));
  const zoomOut = () => setZoom((z) => Math.max(0.5, +(z - 0.25).toFixed(2)));
  const zoomReset = () => setZoom(1);

  const onPanStart = (e) => {
    if (e.button !== 0 || !bodyRef.current) return;
    dragRef.current = {
      x: e.clientX, y: e.clientY,
      sl: bodyRef.current.scrollLeft, st: bodyRef.current.scrollTop,
    };
    setPanning(true);
    e.preventDefault();
  };
  const onPanMove = (e) => {
    if (!dragRef.current || !bodyRef.current) return;
    bodyRef.current.scrollLeft = dragRef.current.sl - (e.clientX - dragRef.current.x);
    bodyRef.current.scrollTop = dragRef.current.st - (e.clientY - dragRef.current.y);
  };
  const onPanEnd = () => { dragRef.current = null; setPanning(false); };

  const scaled = zoom > 1 && size;
  const bodyStyle = scaled ? { width: size.w * zoom, height: size.h * zoom } : undefined;
  return (
    <div className="proof-pane">
      <div className="proof-pane-head">
        <div className="proof-pane-title">
          <strong>{name || hint}</strong>
          {name && <span className="muted">{hint}</span>}
        </div>
        <div className="proof-pane-actions">
          {url && !isPdf && (
            <div className="zoom-controls">
              <button type="button" className="btn btn-sm" onClick={zoomOut} disabled={zoom <= 0.5} title="Zoom out">−</button>
              <button type="button" className="zoom-pct" onClick={zoomReset} title="Reset zoom">{Math.round(zoom * 100)}%</button>
              <button type="button" className="btn btn-sm" onClick={zoomIn} disabled={zoom >= 3} title="Zoom in">+</button>
            </div>
          )}
          {url && (
            <a className="btn btn-sm" href={url} target="_blank" rel="noreferrer">Open original</a>
          )}
        </div>
      </div>
      <div className={`proof-pane-body${scaled ? ' pannable' : ''}${panning ? ' panning' : ''}`} ref={bodyRef}
        onMouseDown={onPanStart} onMouseMove={onPanMove} onMouseUp={onPanEnd} onMouseLeave={onPanEnd}>
        {url ? (
          isPdf
            ? <iframe src={url} title={name || hint} className="proof-frame" />
            : (
              <img src={url} alt={name || hint} className="proof-img" loading="lazy" draggable={false}
                style={scaled ? { width: '100%', height: '100%', maxWidth: 'none', maxHeight: 'none', objectFit: 'fill' } : { transform: `scale(${zoom})` }} />
            )
        ) : (
          <span className="proof-empty">{empty}</span>
        )}
      </div>
    </div>
  );
}
