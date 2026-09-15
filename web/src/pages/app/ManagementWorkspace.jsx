import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, fmtDate } from '../../api';
import {
  ErrorBox, Field, Modal, PageHeader, SearchBox, Select, Spinner, StatusBadge,
  Table, TextArea, TextInput, toast, useAsync, useFileUrl,
} from '../../ui';
import Users from './Users';
import Hierarchy from './Hierarchy';
import Roles from './Roles';

function AssetPreview({ rel, className, alt }) {
  const url = useFileUrl(rel);
  if (!rel || !url) return null;
  return <img src={url} className={className} alt={alt} />;
}

// Perspective 7: the masters must be configured before a campaign can exist.
// `platform` masters are set up by the company owner from the platform console;
// `tenant` items are division-owned data (field teams / chemist network).
const CONFIG_LINKS = {
  hierarchy: null, employees: '/app/user-management', brands: '/app/brands',
  chemists: '/app/chemists', regions: '/app/user-management', states: '/app/chemists',
  gifts: null,
};

// Rollout-scope picker for the campaign wizard (states served as dropdowns,
// not as a free-text field, so the eligibility check matches at POB time).
const INDIAN_STATES = [
  'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chhattisgarh', 'Goa',
  'Gujarat', 'Haryana', 'Himachal Pradesh', 'Jharkhand', 'Karnataka', 'Kerala',
  'Madhya Pradesh', 'Maharashtra', 'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland',
  'Odisha', 'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura',
  'Uttar Pradesh', 'Uttarakhand', 'West Bengal', 'Andaman and Nicobar Islands',
  'Chandigarh', 'Dadra and Nagar Haveli and Daman and Diu', 'Delhi',
  'Jammu and Kashmir', 'Ladakh', 'Lakshadweep', 'Puducherry',
];

function StatePicker({ value = [], onChange, options = [] }) {
  const [open, setOpen] = useState(false);
  const [srch, setSrch] = useState('');
  const ref = useRef(null);
  useEffect(() => {
    const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);
  const shown = options.filter((s) => !srch || s.toLowerCase().includes(srch.toLowerCase()));
  const allSelected = shown.length > 0 && shown.every((s) => value.includes(s));
  const toggleAll = () => {
    if (allSelected) onChange(value.filter((s) => !shown.includes(s)));
    else onChange([...new Set([...value, ...shown])]);
  };
  return (
    <div className="role-picker" style={{ position: 'relative' }} ref={ref}>
      <button type="button" className="seg span-2" style={{ justifyContent: 'space-between' }}
        onClick={() => setOpen((o) => !o)}>
        <span>{value.length ? `${value.length} state(s) selected` : 'Select states'}</span>
        <span className="muted">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 20,
          background: 'var(--bg-2)', border: '1px solid var(--border)', borderRadius: 'var(--radius)',
          boxShadow: '0 2px 8px rgba(0,0,0,.2)', marginTop: 4, padding: 8, maxHeight: 300, overflow: 'auto' }}>
          <div className="role-picker">
            <input className="input" placeholder="Search states…" value={srch}
              onChange={(e) => setSrch(e.target.value)} />
            <div className="seg">
              <button type="button" className={`seg${allSelected ? ' active' : ''}`} onClick={toggleAll}>
                {allSelected ? 'Clear all' : 'Select all'}
              </button>
              <button type="button" className="seg" onClick={() => { onChange([]); setSrch(''); }}>
                Clear
              </button>
            </div>
            {shown.map((s) => {
              const sel = value.includes(s);
              return (
                <label key={s} className="check">
                  <input type="checkbox" checked={sel}
                    onChange={() => onChange(sel ? value.filter((x) => x !== s) : [...value, s])} />
                  {s}
                </label>
              );
            })}
            {shown.length === 0 && <span className="muted">No states match</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function ConfigChecklist({ data, compact }) {
  if (!data) return null;
  const { ready, complete, items } = data;
  return (
    <div className={`config-checklist${ready ? ' ok' : ''}`}>
      <div className="config-checklist-head">
        <strong>{
          ready ? 'Configuration complete — campaigns can be created ✓'
            : 'Set up these masters before creating a campaign'
        }</strong>
        {!ready && <span className="muted">Campaigns need at least one brand before they can be created — the rest of these masters are optional guidance.</span>}
        {ready && !complete && <span className="muted">Add hierarchy, territories and gratitude breadth for a fully configured company.</span>}
      </div>
      <div className="config-checklist-items">
        {items.map((it) => {
          const uri = CONFIG_LINKS[it.key];
          return (
            <div key={it.key} className={`config-item${it.ready ? ' ok' : ''}`}>
              <span className="config-dot">{it.ready ? '✓' : '·'}</span>
              <span className="config-label">{it.label}</span>
              <span className="muted">({it.count})</span>
              {it.where === 'tenant' && !compact && (
                <span className="muted config-where">{it.ready ? 'ready' : 'add data'}</span>
              )}
              {it.where === 'tenant' && compact && uri && it.ready ? (
                <Link className="btn-link" to={uri}>view</Link>
              ) : it.where === 'platform' ? (
                <span className="muted config-where">platform master</span>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function ManagementWorkspace({ base, title, subtitle, back }) {
  const [tab, setTab] = useState('campaigns');
  return (
    <div>
      <PageHeader title={title}
        subtitle={subtitle}
        actions={back && <>{back}</>} />
      <div className="tabs">
        {[['campaigns', 'Campaigns'], ['brands', 'Brands'], ['divisions', 'Divisions'],
          ['users', 'Users'], ['hierarchy', 'Hierarchy'], ['roles', 'Roles & Permissions']].map(([k, label]) => (
          <button key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>{label}</button>
        ))}
      </div>
      {tab === 'campaigns' && <CampaignsTab base={base} />}
      {tab === 'brands' && <BrandsTab base={base} />}
      {tab === 'divisions' && <DivisionsTab base={base} />}
      {tab === 'users' && <Users base={base} />}
      {tab === 'hierarchy' && <Hierarchy base={base} />}
      {tab === 'roles' && <Roles base={base} />}
    </div>
  );
}

export function CampaignsTab({ base }) {
  const { data, loading, error, run } = useAsync(() => api(`${base}/campaigns`));
  const readiness = useAsync(() => api(`${base}/campaigns/readiness`));
  const brands = useAsync(() => api(`${base}/brands`));
  const divisions = useAsync(() => api(`${base}/divisions`));
  const [params, setParams] = useSearchParams();
  const statusFilter = params.get('status') || '';
  const [q, setQ] = useState('');
  const [manualEdit, setManualEdit] = useState(null);
  const [ext, setExt] = useState(null);
  const editing = manualEdit !== null ? manualEdit : (params.get('new') === '1' ? {} : null);

  const STATUS_TABS = [
    ['', 'All'], ['draft', 'Draft'], ['pending_approval', 'Pending Approval'],
    ['scheduled', 'Scheduled'], ['active', 'Active'], ['completed', 'Completed'], ['rejected', 'Rejected'],
  ];
  const setStatusFilter = (s) => {
    const next = new URLSearchParams(params);
    if (s) next.set('status', s); else next.delete('status');
    setParams(next, { replace: true });
  };
  const openCreate = () => {
    const next = new URLSearchParams(params);
    next.set('new', '1');
    setParams(next, { replace: true });
    setManualEdit({});
  };
  const closeCreate = () => {
    const next = new URLSearchParams(params);
    next.delete('new');
    setParams(next, { replace: true });
    setManualEdit(null);
  };
  const openEdit = (r) => setManualEdit({ ...r });

  if (loading) return <Spinner label="Loading campaigns…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const rows = (data?.items || []).filter((r) => {
    if (statusFilter && r.status !== statusFilter) return false;
    return !q || (r.name || '').toLowerCase().includes(q.toLowerCase());
  });

  const del = async (row) => {
    if (!window.confirm(`Delete campaign ${row.name}?`)) return;
    try {
      await api(`${base}/campaigns/${row.id}`, { method: 'DELETE' });
      toast('Campaign deleted', 'success'); run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const extend = async (e) => {
    e.preventDefault();
    try {
      const body = ext.mode === 'days' ? { days: Number(ext.days) } : { end_date: ext.end_date };
      const r = await api(`${base}/campaigns/${ext.id}/extend`, { method: 'POST', body });
      toast(`Campaign extended to ${r.end_date}`, 'success');
      setExt(null); run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const toggleActive = async (row) => {
    try {
      const r = await api(`${base}/campaigns/${row.id}/toggle-active`, { method: 'PATCH' });
      toast(`Campaign ${r.active ? 'activated' : 'deactivated'}`, 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const submitForApproval = async (row) => {
    if (!window.confirm(`Submit "${row.name}" for approval? It will be locked until the approver decides.`)) return;
    try {
      await api(`${base}/campaigns/${row.id}/submit`, { method: 'POST' });
      toast('Submitted for approval', 'success'); run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const withdraw = async (row) => {
    if (!window.confirm(`Withdraw "${row.name}" back to draft?`)) return;
    try {
      await api(`${base}/campaigns/${row.id}/withdraw`, { method: 'POST' });
      toast('Withdrawn to draft', 'success'); run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const divs = divisions.data?.items || [];
  const singleDivision = divisions.data ? divs.length <= 1 : false;

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'name', label: 'Campaign' },
    { key: 'brand_name', label: 'Brand' },
    ...(singleDivision ? [] : [{ key: 'division_name', label: 'Division' }]),
    { key: 'start_date', label: 'Start', render: (r) => fmtDate(r.start_date) },
    { key: 'end_date', label: 'End', render: (r) => fmtDate(r.end_date) },
    { key: 'product_count', label: 'Products' },
    {
      key: 'execute_scope', label: 'Execute scope', render: (r) => {
        const a = r.assignment || {};
        const count = a.assigned_count;
        if (a.open) return <span className="muted">All employees</span>;
        if (r.status === 'draft' || r.status === 'pending_approval') return <span className="muted">{count || 0} employee(s)</span>;
        return <span>{count} employee(s)</span>;
      },
    },
    { key: 'pob_count', label: 'POBs' },
    { key: 'status', label: 'Status', render: (r) => (
      <span style={{ display: 'inline-flex', flexDirection: 'column', gap: 2 }}>
        <StatusBadge value={r.status} />
        {r.status === 'rejected' && r.rejection_note && (
          <span className="muted" style={{ fontSize: 11 }} title={r.rejection_note}>Rejected: {r.rejection_note.slice(0, 48)}{r.rejection_note.length > 48 ? '…' : ''}</span>
        )}
      </span>
    ) },
    { key: 'active', label: 'Active', render: (r) => (
      <button className={`btn btn-sm ${r.active ? 'btn-primary' : ''}`}
        style={{ minWidth: 64, fontSize: 12 }}
        onClick={() => toggleActive(r)}>
        {r.active ? 'ON' : 'OFF'}
      </button>
    ) },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        {r.status === 'draft' || r.status === 'rejected' ? (
          <button className="btn-link" onClick={() => submitForApproval(r)}>{r.status === 'rejected' ? 'Resubmit' : 'Submit'}</button>
        ) : null}
        {r.status === 'pending_approval' && (
          <button className="btn-link" onClick={() => withdraw(r)}>Withdraw</button>
        )}
        <button className="btn-link" onClick={() => setExt({ id: r.id, mode: 'days', days: 30 })}>Extend</button>
        <button className="btn-link" onClick={() => openEdit(r)}>Edit</button>
        <button className="btn-link danger" onClick={() => del(r)}>Delete</button>
      </span>
    ) },
  ];

  return (
    <div>
      {readiness.data && !readiness.data.ready && (
        <ConfigChecklist data={readiness.data} />
      )}
      <PageHeader title="Campaigns" subtitle="Create and manage the division's POB schemes"
        actions={<>
          <SearchBox value={q} onChange={setQ} />
          <button className="btn btn-primary" onClick={openCreate}>+ New campaign</button>
        </>} />
      <div className="tabs">
        {STATUS_TABS.map(([id, label]) => (
          <button key={id} className={`tab${statusFilter === id ? ' active' : ''}`} onClick={() => setStatusFilter(id)}>
            {label}
          </button>
        ))}
      </div>
      <Table cols={cols} rows={rows} keyOf={(r) => r.id} empty="No campaigns yet" />
      {editing && (
        <CampaignModal editing={editing} base={base}
          brands={brands.data?.items || []} divisions={divisions.data?.items || []}
          onClose={closeCreate} onDone={() => { closeCreate(); run(); }} />
      )}
      {ext && (
        <Modal open title={`Extend campaign #${ext.id}`} onClose={() => setExt(null)}
          footer={<>
            <button className="btn" onClick={() => setExt(null)}>Cancel</button>
            <button className="btn btn-primary" form="ext-form">Extend</button>
          </>}>
          <form id="ext-form" className="grid-2" onSubmit={extend}>
            <Field label="Mode">
              <select className="input" value={ext.mode}
                onChange={(e) => setExt((p) => ({ ...p, mode: e.target.value }))}>
                <option value="days">Add days</option>
                <option value="date">Set new end date</option>
              </select>
            </Field>
            {ext.mode === 'days' ? (
              <Field label="Days" required>
                <TextInput type="number" min="1" value={ext.days ?? ''} required
                  onChange={(e) => setExt((p) => ({ ...p, days: e.target.value }))} />
              </Field>
            ) : (
              <Field label="New end date" required>
                <TextInput type="date" value={ext.end_date || ''} required
                  onChange={(e) => setExt((p) => ({ ...p, end_date: e.target.value }))} />
              </Field>
            )}
          </form>
        </Modal>
      )}
    </div>
  );
}

function CampaignModal({ editing, base, brands, divisions, onClose, onDone }) {
  const isEdit = !!editing.id;
  const detail = useAsync(
    () => (isEdit ? api(`${base}/campaigns/${editing.id}`) : Promise.resolve({ products: [] })),
    [isEdit, editing.id, base]);
  const roles = useAsync(() => api(`${base}/roles`), [base]);
  const users = useAsync(() => api(`${base}/users`), [base]);
  const readiness = useAsync(() => api(`${base}/campaigns/readiness`), [isEdit, base]);
  const chemistMasters = useAsync(() => api('/api/v1/chemist-masters'), [isEdit, base]);
  const [f, setF] = useState({
    active: true, invoice_verification_required: true, status: 'draft', scheme_type: 'others',
    assignment: { mode: 'all', regions: [], employee_ids: [], manager_id: '' }, ...editing,
    eligible_states: [], eligible_chemist_attachment_types: [], eligible_chemist_potential_categories: [],
  });
  const [products, setProducts] = useState([]);
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState({ logo: null, banner: null });
  const loaded = useRef(false);
  const [step, setStep] = useState(0);
  const STEPS = ['Details', 'Brand & Products', 'Rollout Scope', 'Timeline & Rules', 'Assignment', 'Branding & Submissions', 'Review'];
  const lastStep = STEPS.length - 1;

  const nextStep = () => {
    if (step === 0 && !(f.name || '').trim()) {
      toast('Campaign name is required', 'error');
      return;
    }
    setStep((s) => Math.min(s + 1, lastStep));
  };

  useEffect(() => {
    if (isEdit && detail.data && !loaded.current) {
      loaded.current = true;
      const brandIds = Array.isArray(detail.data.brand_ids)
        ? detail.data.brand_ids.map(Number).filter(Boolean)
        : (detail.data.brand_id ? [Number(detail.data.brand_id)] : []);
      const asg = detail.data.assignment || {};
      const rules = asg.rules || [];
      const modes = [...new Set(rules.map((r) => r.mode))];
      const assignment = (modes.length !== 1 || rules.some((r) => r.mode === 'all'))
        ? { mode: 'all', regions: [], employee_ids: [], manager_id: '' }
        : {
            mode: modes[0],
            regions: rules.filter((r) => r.region).map((r) => r.region),
            employee_ids: rules.filter((r) => r.employee_id).map((r) => Number(r.employee_id)),
            manager_id: (rules.find((r) => r.manager_id)?.manager_id
              ? String(rules.find((r) => r.manager_id).manager_id) : ''),
          };
      setF((prev) => ({
        ...prev,
        assignment,
        division_id: prev.division_id ?? detail.data.division_id ?? '',
        brand_id: prev.brand_id ?? detail.data.brand_id ?? '',
        brand_mode: brandIds.length > 1 ? 'multiple' : 'single',
        brand_ids: brandIds,
        logo_path: detail.data.logo_path ?? prev.logo_path ?? '',
        banner_path: detail.data.banner_path ?? prev.banner_path ?? '',
        eligible_states: detail.data.eligible_states || [],
        eligible_chemist_attachment_types: detail.data.eligible_chemist_attachment_types || [],
        eligible_chemist_potential_categories: detail.data.eligible_chemist_potential_categories || [],
      }));
      setProducts((detail.data.products || []).map((x) => ({ ...x })));
    }
  }, [isEdit, detail.data]);

  const set = (k) => (e) => {
    const v = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setF((p) => ({ ...p, [k]: v }));
  };

  const roleItems = roles.data?.items || [];
  const allowed = (f.upload_roles || '').split(',').map((s) => s.trim()).filter(Boolean);
  const toggleRole = (name) => {
    const next = allowed.includes(name) ? allowed.filter((r) => r !== name) : [...allowed, name];
    setF((p) => ({ ...p, upload_roles: next.length ? next.join(',') : '' }));
  };

  const brandIds = (f.brand_ids || []).map(Number).filter(Boolean);
  const setBrandMode = (mode) => {
    setF((p) => ({
      ...p,
      brand_mode: mode,
      brand_id: mode === 'single' ? (brandIds[0] ?? p.brand_id ?? '') : (brandIds[0] ?? p.brand_id ?? ''),
    }));
  };
  const toggleBrand = (bid) => {
    const next = brandIds.includes(bid)
      ? brandIds.filter((x) => x !== bid)
      : [...brandIds, bid];
    setF((p) => ({
      ...p,
      brand_ids: next,
      brand_id: next.length ? next[0] : p.brand_id,
    }));
  };

  const windowPreview = useMemo(() => {
    const addMonths = (iso, m) => {
      if (!iso) return null;
      const [y0, m0, d0] = String(iso).slice(0, 10).split('-').map(Number);
      const y = y0 + Math.floor((m0 - 1 + m) / 12);
      const mo = ((m0 - 1 + m) % 12 + 12) % 12;
      const last = new Date(y, mo + 1, 0).getDate();
      return new Date(y, mo, Math.min(d0, last));
    };
    const fmt = (d) => d
      ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
      : '—';
    if (!f.start_date && !f.end_date) return null;
    const floor = f.start_date ? addMonths(f.start_date, 0) : null;
    if (floor) floor.setDate(floor.getDate() - (Number(f.pre_grace_days) || 0));
    let ceil = f.end_date ? addMonths(f.end_date, Number(f.grace_months) || 0) : null;
    if (ceil) ceil.setDate(ceil.getDate() + (Number(f.grace_days) ?? 15));
    return `${fmt(floor)} → ${fmt(ceil)}`;
  }, [f.start_date, f.end_date, f.pre_grace_days, f.grace_months, f.grace_days]);

  const uploadAsset = async (campaignId, kind, file) => {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await api(`${base}/campaigns/${campaignId}/asset?kind=${kind}`, { method: 'POST', body: fd });
      setF((p) => ({ ...p, [kind === 'logo' ? 'logo_path' : 'banner_path']: r.path }));
      toast(kind === 'logo' ? 'Logo uploaded' : 'Banner uploaded', 'success');
    } catch (err) { toast(err.message, 'error'); }
  };

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const brandIds = (f.brand_ids || []).map(Number).filter(Boolean);
      const payload = {
        ...f,
        assignment: f.assignment && f.assignment.mode ? f.assignment : { mode: 'all' },
        brand_id: brandIds.length ? brandIds[0] : (f.brand_id || null),
        brand_ids: brandIds,
        products: products.filter((p) => (p.name || '').trim() || p.brand_id),
      };
      let id = editing.id;
      if (isEdit) await api(`${base}/campaigns/${editing.id}`, { method: 'PUT', body: payload });
      else id = (await api(`${base}/campaigns`, { method: 'POST', body: payload })).id;
      if (files.logo) await uploadAsset(id, 'logo', files.logo);
      if (files.banner) await uploadAsset(id, 'banner', files.banner);
      toast(isEdit ? 'Campaign updated' : 'Campaign created', 'success');
      onDone();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const divBrands = f.division_id
    ? brands.filter((b) => String(b.division_id) === String(f.division_id) || !b.division_id)
    : brands;
  const brandOpts = divBrands.map((b) => ({ value: b.id, label: b.name }));
  const divOpts = divisions.map((d) => ({ value: d.id, label: d.name }));
  const autoDivision = divisions.length === 1 ? divisions[0] : null;
  const singleDivision = Boolean(autoDivision);

  useEffect(() => {
    if (autoDivision && !isEdit) {
      setF((p) => (p.division_id ? p : { ...p, division_id: String(autoDivision.id) }));
    }
  }, [autoDivision, isEdit]);

  const reviewBrands = brandIds.length
    ? brandIds.map((id) => brands.find((b) => Number(b.id) === Number(id))?.name).filter(Boolean).join(', ')
    : (brands.find((b) => Number(b.id) === Number(f.brand_id))?.name || '—');
  const reviewDivision = divisions.find((d) => String(d.id) === String(f.division_id))?.name || '—';
  const reviewProducts = products.filter((p) => (p.name || '').trim() || p.brand_id);
  const reviewRoles = f.upload_roles === '*' || !f.upload_roles ? 'All roles' : f.upload_roles;
  const _attName = (code) => (chemistMasters.data?.attachment_types || []).find((m) => m.code === code)?.name || code;
  const _catName = (code) => (chemistMasters.data?.potential_categories || []).find((m) => m.code === code)?.name || code;
  const reviewStates = (f.eligible_states || []).length ? f.eligible_states.join(', ') : 'Any state';
  const reviewChemistTypes = (f.eligible_chemist_attachment_types || []).length
    ? (f.eligible_chemist_attachment_types || []).map(_attName).join(', ') : 'Any type';
  const reviewChemistCats = (f.eligible_chemist_potential_categories || []).length
    ? (f.eligible_chemist_potential_categories || []).map(_catName).join(', ') : 'Any category';
  const reviewAssignment = (() => {
    const a = (isEdit && detail.data?.assignment) || f.assignment || {};
    if (a.mode === 'all' || !a.mode) return 'All eligible employees';
    if (a.mode === 'region') return `Region(s): ${(a.regions || []).join(', ') || '—'}`;
    if (a.mode === 'employee') {
      const byId = new Map((users.data?.items || []).map((u) => [Number(u.id), u.full_name]));
      const names = (a.employee_ids || []).map((id) => byId.get(Number(id)) || `#${id}`);
      return `Selected employees(${names.length}): ${names.join(', ') || '—'}`;
    }
    if (a.mode === 'hierarchy') {
      const mgr = (users.data?.items || []).find((u) => String(u.id) === String(a.manager_id));
      return `Hierarchy: ${mgr ? mgr.full_name + (mgr.hierarchy_level_name ? ` (${mgr.hierarchy_level_name})` : '') : '—'} + subordinates`;
    }
    return '—';
  })();
  const employeeOpts = (users.data?.items || []).filter((u) => u.status !== 'inactive');
  const regionOpts = [...new Set(employeeOpts.map((u) => u.region).filter(Boolean))].sort();
  const withSubordinates = new Set(employeeOpts.map((u) => u.parent_id).filter(Boolean));
  const descendantsOf = (id) => {
    const children = {};
    employeeOpts.forEach((u) => { children[u.parent_id] = children[u.parent_id] || []; children[u.parent_id].push(u.id); });
    const seen = new Set();
    const walk = (pid) => { (children[pid] || []).forEach((uid) => { if (!seen.has(uid)) { seen.add(uid); walk(uid); } }); };
    walk(id);
    return seen.size;
  };
  const isCreate = !isEdit;
  const blocked = isCreate && readiness.data && !readiness.data.ready;

  return (
    <Modal open wide title={isEdit ? `Edit ${editing.name}` : 'New campaign'} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        {!blocked && step > 0 && <button className="btn" type="button" onClick={() => setStep((s) => s - 1)}>← Back</button>}
        {blocked ? (
          <button className="btn btn-primary" disabled={busy}>Configure masters first</button>
        ) : step < lastStep ? (
          <button className="btn btn-primary" type="button" onClick={nextStep}>Next →</button>
        ) : (
          <button className="btn btn-primary" form="camp-form" disabled={busy}>{busy ? 'Saving…' : (isCreate ? 'Create campaign' : 'Save changes')}</button>
        )}
      </>}>
      {blocked ? (
        <ConfigChecklist data={readiness.data} compact />
      ) : (<>
      <div className="stepper">
        {STEPS.map((label, i) => (
          <div key={label} className={`step${i === step ? ' active' : ''}${i < step ? ' done' : ''}`}>
            <span className="step-num">{i < step ? '✓' : i + 1}</span>
            <span className="step-label">{label}</span>
          </div>
        ))}
      </div>
      <form id="camp-form" onSubmit={submit}>
        {step === 0 && (
          <div className="grid-2">
            <Field label="Campaign name" required><TextInput value={f.name || ''} onChange={set('name')} required /></Field>
            {!singleDivision && (
              <Field label="Division"><Select value={f.division_id || ''} onChange={set('division_id')} options={divOpts} /></Field>
            )}
            <Field label="Scheme type"><Select value={f.scheme_type || 'others'} onChange={set('scheme_type')}
              options={['cashback', 'upi', 'voucher', 'gift', 'coupon', 'points', 'physical_gift', 'others'].map((o) => ({ value: o, label: o }))} /></Field>
            {isCreate ? (
              <div className="span-2">
                <p className="ai-note">
                  New campaigns start as <strong>Draft</strong>. After saving, submit the campaign for approval — it
                  becomes executable only once the platform approver approves it. This guards the PTR / POB value /
                  reward configuration on this screen.
                </p>
              </div>
            ) : (
              <>
                <Field label="Status"><Select value={f.status || 'draft'} onChange={set('status')}
                  options={['draft', 'completed', 'paused'].map((o) => ({ value: o, label: o }))} /><span className="muted" style={{ fontSize: 11 }}>Go-live happens through the approval flow.</span></Field>
                <div className="span-2">
                  <label className="check"><input type="checkbox" checked={f.active} onChange={set('active')} /> Active</label>
                </div>
              </>
            )}
            <Field label="Description" className="span-2"><TextArea rows={2} value={f.description || ''} onChange={set('description')} /></Field>
            <Field label="Terms & conditions" className="span-2"><TextArea rows={2} value={f.terms_conditions || ''} onChange={set('terms_conditions')} /></Field>
          </div>
        )}
        {step === 1 && (
          <>
            <Field label="Brand selection" hint={f.brand_mode === 'multiple'
              ? `${brandIds.length} brand(s) selected`
              : 'Single brand — the primary brand for this campaign'}>
              <div className="segmented">
                <button type="button" className={`seg${f.brand_mode !== 'multiple' ? ' active' : ''}`}
                  onClick={() => setBrandMode('single')}>Single brand</button>
                <button type="button" className={`seg${f.brand_mode === 'multiple' ? ' active' : ''}`}
                  onClick={() => setBrandMode('multiple')}>Multiple brands</button>
              </div>
            </Field>
            <Field label="Brand"
              hint={singleDivision
                ? `Showing ${autoDivision.name} division brands${f.brand_mode === 'multiple' ? ' — pick one or more' : ''}`
                : (f.division_id
                  ? `Showing brands in the selected division${f.brand_mode === 'multiple' ? ' — pick one or more' : ''}`
                  : 'Pick a division first to narrow this list')}>
              {f.brand_mode === 'multiple' ? (
                <div className="role-picker">
                  {divBrands.map((b) => (
                    <label key={b.id} className="check">
                      <input type="checkbox" checked={brandIds.includes(b.id)} onChange={() => toggleBrand(b.id)} />
                      {b.name}{!b.division_id && <span className="muted"> · unassigned</span>}
                    </label>
                  ))}
                  {divBrands.length === 0 && <span className="muted">No brands in this division yet</span>}
                </div>
              ) : (
                <Select value={f.brand_id || ''} onChange={set('brand_id')} options={brandOpts} />
              )}
            </Field>
            <ProductBuilder base={base} products={products} setProducts={setProducts} brands={brands} divBrands={divBrands}
              singleBrand={f.brand_mode === 'single' ? f.brand_id : null} />
          </>
        )}
        {step === 2 && (
          <div>
            <Field label="States to rollout" className="span-2"
              hint={(f.eligible_states || []).length
                ? `${f.eligible_states.length} state(s) selected`
                : 'No states selected — chemists in any state are accepted'}>
              <StatePicker value={f.eligible_states || []} onChange={(v) => setF((p) => ({ ...p, eligible_states: v }))}
                options={INDIAN_STATES} />
            </Field>
            <Field label="Chemist types (attachment)" className="span-2"
              hint={(f.eligible_chemist_attachment_types || []).length
                ? `${f.eligible_chemist_attachment_types.length} type(s) selected`
                : 'No types selected — all chemist types are accepted'}>
              <div className="role-picker">
                {(chemistMasters.data?.attachment_types || []).map((m) => {
                  const sel = (f.eligible_chemist_attachment_types || []).includes(m.code);
                  return (
                    <label key={m.code} className="check">
                      <input type="checkbox" checked={sel}
                        onChange={() => setF((p) => ({
                          ...p,
                          eligible_chemist_attachment_types: sel
                            ? (p.eligible_chemist_attachment_types || []).filter((x) => x !== m.code)
                            : [...(p.eligible_chemist_attachment_types || []), m.code],
                        }))} />
                      {m.name}
                    </label>
                  );
                })}
                {(chemistMasters.data?.attachment_types || []).length === 0 && (
                  <span className="muted">No chemist types configured yet</span>
                )}
              </div>
            </Field>
            <Field label="Chemist categories (potential)" className="span-2"
              hint={(f.eligible_chemist_potential_categories || []).length
                ? `${f.eligible_chemist_potential_categories.length} categor(y/ies) selected`
                : 'No categories selected — all potential categories are accepted'}>
              <div className="role-picker">
                {(chemistMasters.data?.potential_categories || []).map((m) => {
                  const sel = (f.eligible_chemist_potential_categories || []).includes(m.code);
                  return (
                    <label key={m.code} className="check">
                      <input type="checkbox" checked={sel}
                        onChange={() => setF((p) => ({
                          ...p,
                          eligible_chemist_potential_categories: sel
                            ? (p.eligible_chemist_potential_categories || []).filter((x) => x !== m.code)
                            : [...(p.eligible_chemist_potential_categories || []), m.code],
                        }))} />
                      {m.name}
                    </label>
                  );
                })}
                {(chemistMasters.data?.potential_categories || []).length === 0 && (
                  <span className="muted">No potential categories configured yet</span>
                )}
              </div>
            </Field>
            <div className="span-2">
              <p className="ai-note">
                This is the <strong>rollout scope</strong> for your brand products. MR / ASM register chemists
                on the ground — this scope tells them which states and chemist categories to focus on, and a
                POB is only accepted from a chemist that fits it. Leave a section empty to accept any.
              </p>
            </div>
          </div>
        )}
        {step === 3 && (
          <div className="grid-2">
            <Field label="Start date"><TextInput type="date" value={f.start_date || ''} onChange={set('start_date')} /></Field>
            <Field label="End date"><TextInput type="date" value={f.end_date || ''} onChange={set('end_date')} /></Field>
            <Field label="Period type" hint="none — invoice any time in window">
              <Select value={f.period_type || 'none'} onChange={set('period_type')}
                options={['none', 'monthly', 'quarterly'].map((o) => ({ value: o, label: o }))} />
            </Field>
            <Field label="Pre-launch grace days" hint="Accept invoices issued this many days before the campaign start (for new launches)">
              <TextInput type="number" min="0" value={f.pre_grace_days ?? 0} onChange={set('pre_grace_days')} />
            </Field>
            <Field label="Invoice grace months" hint="Months after the campaign ends during which invoice proofs are accepted">
              <Select value={String(f.grace_months ?? 0)} onChange={(e) => setF((p) => ({ ...p, grace_months: Number(e.target.value) }))}
                options={Array.from({ length: 13 }, (_, i) => ({ value: String(i), label: String(i) }))} />
            </Field>
            <Field label="Invoice grace days" hint="Extra days on top of the months after the campaign ends (default 15)">
              <TextInput type="number" min="0" value={f.grace_days ?? 15} onChange={set('grace_days')} />
            </Field>
            <div className="span-2">
              <p className="ai-note">
                Accepted invoice window: <strong>{windowPreview || 'set start/end dates to preview'}</strong>
              </p>
            </div>
            <div className="span-2">
              <label className="check"><input type="checkbox" checked={f.invoice_verification_required} onChange={set('invoice_verification_required')} /> Invoice verification required</label>
            </div>
          </div>
        )}
        {step === 4 && (
          <div className="grid-2">
            <Field label="Who can execute this campaign?" className="span-2"
              hint="The audience allowed to submit POBs for this campaign. Choose to keep it open, restrict by region, pick specific employees, or give a manager and their whole team.">
              <div className="segmented">
                <button type="button" className={`seg${f.assignment?.mode === 'all' ? ' active' : ''}`}
                  onClick={() => setF((p) => ({ ...p, assignment: { mode: 'all', regions: [], employee_ids: [], manager_id: '' } }))}>
                  A · All eligible</button>
                <button type="button" className={`seg${f.assignment?.mode === 'region' ? ' active' : ''}`}
                  onClick={() => setF((p) => ({ ...p, assignment: { mode: 'region', regions: [], employee_ids: [], manager_id: '' } }))}>
                  B · Region</button>
                <button type="button" className={`seg${f.assignment?.mode === 'employee' ? ' active' : ''}`}
                  onClick={() => setF((p) => ({ ...p, assignment: { mode: 'employee', regions: [], employee_ids: [], manager_id: '' } }))}>
                  C · Employees</button>
                <button type="button" className={`seg${f.assignment?.mode === 'hierarchy' ? ' active' : ''}`}
                  onClick={() => setF((p) => ({ ...p, assignment: { mode: 'hierarchy', regions: [], employee_ids: [], manager_id: '' } }))}>
                  D · Hierarchy</button>
              </div>
            </Field>
            {f.assignment?.mode === 'all' && (
              <div className="span-2">
                <p className="ai-note">Open to every eligible employee in the division (division-wide). Upload-role rules still apply on top of this audience.</p>
              </div>
            )}
            {f.assignment?.mode === 'region' && (
              <Field label="Region(s)" className="span-2"
                hint={regionOpts.length ? 'Active regions across the tenant employees' : 'No regions found on active users yet'}>
                {regionOpts.length ? (
                  <div className="role-picker">
                    {regionOpts.map((r) => {
                      const sel = (f.assignment?.regions || []).includes(r);
                      return (
                        <label key={r} className="check">
                          <input type="checkbox" checked={sel}
                            onChange={() => setF((p) => ({
                              ...p, assignment: {
                                ...p.assignment,
                                regions: sel ? (p.assignment.regions || []).filter((x) => x !== r) : [...(p.assignment.regions || []), r],
                              },
                            }))} />
                          {r}
                        </label>
                      );
                    })}
                  </div>
                ) : <span className="muted">No regions found on active users yet</span>}
              </Field>
            )}
            {f.assignment?.mode === 'employee' && (
              <Field label="Select employees" className="span-2"
                hint={(f.assignment?.employee_ids || []).length
                  ? `${f.assignment.employee_ids.length} employee(s) selected`
                  : 'Pick one or more employees allowed to execute this campaign'}>
                <div className="role-picker">
                  {employeeOpts.map((u) => {
                    const sel = (f.assignment?.employee_ids || []).includes(Number(u.id));
                    return (
                      <label key={u.id} className="check">
                        <input type="checkbox" checked={sel}
                          onChange={() => setF((p) => ({
                            ...p, assignment: {
                              ...p.assignment,
                              employee_ids: sel
                                ? (p.assignment.employee_ids || []).filter((x) => Number(x) !== Number(u.id))
                                : [...(p.assignment.employee_ids || []), Number(u.id)],
                            },
                          }))} />
                        {u.full_name}
                        {u.hierarchy_level_name && <span className="muted"> · {u.hierarchy_level_name}</span>}
                        {u.region && <span className="muted"> · {u.region}</span>}
                      </label>
                    );
                  })}
                  {employeeOpts.length === 0 && <span className="muted">No active employees yet — add users in the Users tab first</span>}
                </div>
              </Field>
            )}
            {f.assignment?.mode === 'hierarchy' && (
              <Field label="Manager" className="span-2"
                hint="Anyone reporting to this manager (directly or indirectly) in the division is included" required>
                <select className="input" value={f.assignment?.manager_id || ''}
                  onChange={(e) => setF((p) => ({ ...p, assignment: { ...p.assignment, manager_id: e.target.value } }))}
                  required>
                  <option value="">— select a manager —</option>
                  {employeeOpts.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.full_name}{u.hierarchy_level_name ? ` (${u.hierarchy_level_name})` : ''}
                      {withSubordinates.has(u.id) ? ` · ${descendantsOf(u.id)} subordinate(s)` : ''}
                    </option>
                  ))}
                </select>
                <button type="button" className="btn btn-ghost btn-sm"
                  onClick={() => setF((p) => ({ ...p, assignment: { ...p.assignment, manager_id: '' } }))}>
                  Clear selection</button>
              </Field>
            )}
          </div>
        )}
        {step === 5 && (
          <div className="grid-2">
            <Field label="Campaign page logo" className="span-2"
              hint={files.logo ? 'Uploaded when you Save' : (f.logo_path ? 'Current logo' : 'No logo yet')}>
              {files.logo
                ? <img src={URL.createObjectURL(files.logo)} className="asset-preview" alt="New logo preview" />
                : <AssetPreview rel={f.logo_path} className="asset-preview" alt="Current logo" />}
              <input type="file" accept="image/*" onChange={(e) => setFiles((p) => ({ ...p, logo: e.target.files?.[0] || null }))} />
            </Field>
            <Field label="Campaign page banner" className="span-2"
              hint={files.banner ? 'Uploaded when you Save' : (f.banner_path ? 'Current banner' : 'No banner yet')}>
              {files.banner
                ? <img src={URL.createObjectURL(files.banner)} className="asset-preview banner" alt="New banner preview" />
                : <AssetPreview rel={f.banner_path} className="asset-preview banner" alt="Current banner" />}
              <input type="file" accept="image/*" onChange={(e) => setFiles((p) => ({ ...p, banner: e.target.files?.[0] || null }))} />
            </Field>
            <Field label="Who can submit POBs (campaign people credentials)" className="span-2"
              hint={f.upload_roles ? (f.upload_roles === '*' ? 'Anyone can submit' : f.upload_roles) : 'Leave blank to allow all roles'}>
              <div className="role-picker">
                {roleItems.map((r) => (
                  <label key={r.id} className="check">
                    <input type="checkbox" checked={allowed.includes(r.name)} onChange={() => toggleRole(r.name)} />
                    {r.name}
                  </label>
                ))}
              </div>
            </Field>
          </div>
        )}
        {step === 6 && (
          <div>
            <h3 className="sub-head">Review campaign</h3>
            <div className="kv-grid">
              <span>Campaign name<strong>{f.name || '—'}</strong></span>
              <span>Division<strong>{reviewDivision}</strong></span>
              <span>Brand(s)<strong>{reviewBrands}</strong></span>
              <span>Position (PTR/PTS rows)<strong>{reviewProducts.length}</strong></span>
              <span>Scheme type<strong>{f.scheme_type || 'others'}</strong></span>
              <span>Status<strong>{isCreate ? 'Draft (awaiting approval)' : (f.status || 'draft')}</strong></span>
              <span>Execute scope<strong>{reviewAssignment}</strong></span>
              <span>Rollout states<strong>{reviewStates}</strong></span>
              <span>Rollout chemist types<strong>{reviewChemistTypes}</strong></span>
              <span>Rollout chemist categories<strong>{reviewChemistCats}</strong></span>
              <span>Run dates<strong>{f.start_date || '—'} → {f.end_date || '—'}</strong></span>
              <span>Invoice window<strong>{windowPreview || '—'}</strong></span>
              <span>Period type<strong>{f.period_type || 'none'}</strong></span>
              <span>Pre-launch grace<strong>{f.pre_grace_days ?? 0} days</strong></span>
              <span>Invoice grace<strong>{f.grace_months ?? 0} mo + {f.grace_days ?? 15} days</strong></span>
              <span>Verification<strong>{f.invoice_verification_required ? 'Required' : 'Not required'}</strong></span>
              <span>Submit rights<strong>{reviewRoles}</strong></span>
              <span>Logo<strong>{files.logo ? 'Uploaded' : (f.logo_path ? 'Uploaded' : 'None')}</strong></span>
              <span>Banner<strong>{files.banner ? 'Uploaded' : (f.banner_path ? 'Uploaded' : 'None')}</strong></span>
              <span>Terms & conditions<strong>{f.terms_conditions ? 'Included' : 'None'}</strong></span>
            </div>
            <p className="ai-note">Review the details above — you can go back anytime to make changes before saving. After saving, submit the campaign for approval from the Campaigns page.</p>
          </div>
        )}
      </form>
      </>)}
    </Modal>
  );
}

function ProductBuilder({ base, products, setProducts, brands, divBrands, singleBrand }) {
  const masters = useAsync(() => api(`${base}/products`), [base]);
  const [q, setQ] = useState('');
  const [quick, setQuick] = useState(false);
  const [n, setN] = useState({ brand_id: '', name: '', ptr: '', pts: '', mrp: '' });
  const [busy, setBusy] = useState(false);

  const all = masters.data?.items || [];
  const scoped = singleBrand ? all.filter((p) => p && String(p.brand_id) === String(singleBrand)) : all;
  const selectedIds = new Set((products || []).map((p) => p.id).filter(Boolean));
  const filtered = scoped.filter((p) => !q || (p.name || '').toLowerCase().includes(q.toLowerCase())
    || (p.brand_name || '').toLowerCase().includes(q.toLowerCase())
    || (p.sku || '').toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => (selectedIds.has(b.id) ? 1 : 0) - (selectedIds.has(a.id) ? 1 : 0));

  const toggle = (m) => {
    if (selectedIds.has(m.id)) setProducts((prev) => prev.filter((p) => p.id !== m.id));
    else setProducts((prev) => [...prev, {
      ...m,
      min_quantity: m.min_quantity ?? 1,
      min_pob: m.min_pob ?? 0,
      max_pob: m.max_pob ?? null,
      scheme_eligibility: m.scheme_eligibility ?? true,
    }]);
  };

  const setCon = (p, k, v) => setProducts((prev) => prev.map((x) => (x === p ? { ...x, [k]: v } : x)));

  const addQuick = async (e) => {
    e.preventDefault();
    if (!(n.name || '').trim()) { toast('Product name is required', 'error'); return; }
    setBusy(true);
    try {
      const r = await api(`${base}/products`, {
        method: 'POST',
        body: {
          brand_id: n.brand_id || null,
          name: n.name,
          ptr: n.ptr === '' ? 0 : Number(n.ptr),
          pts: n.pts === '' ? 0 : Number(n.pts),
          mrp: n.mrp === '' ? 0 : Number(n.mrp),
          status: 'active',
        },
      });
      await masters.run();
      const created = [{ ...(await api(`${base}/products?q=${encodeURIComponent(n.name)}`)).items?.find((p) => p.id === r.id) }];
      setProducts((prev) => [...prev, ...created].filter(Boolean));
      setN({ brand_id: '', name: '', ptr: '', pts: '', mrp: '' });
      setQuick(false);
      toast('Product added to catalogue and selected', 'success');
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  return (
    <>
      <h3 className="sub-head">Campaign products</h3>
      <p className="muted">Pick from your division's master product catalogue — the campaign will offer exactly the products you select.</p>
      <div className="prod-builder">
        <SearchBox value={q} onChange={setQ} placeholder="Search your division's products…" />
        <div className="prod-picker">
          {masters.loading && <div className="muted" style={{ padding: 12 }}>Loading products…</div>}
          {!masters.loading && filtered.length === 0 && (
            <div className="muted" style={{ padding: 12 }}>
              No matching products. Add them on the Products page, or use “New product” below.
            </div>
          )}
          {filtered.map((m) => {
            const sel = selectedIds.has(m.id);
            return (
              <label key={m.id} className={`prod-picker-row${sel ? ' sel' : ''}`}>
                <input type="checkbox" checked={sel} onChange={() => toggle(m)} />
                <span className="prod-picker-name"><strong>{m.name}</strong>
                  <span className="muted cell-sub">{[m.brand_name, m.pack, m.sku].filter(Boolean).join(' · ')}</span>
                </span>
                <span className="muted prod-picker-price">{m.ptr ? `PTR ₹${m.ptr}` : ''}</span>
              </label>
            );
          })}
        </div>
        {quick && (
          <form className="prod-picker-quick" onSubmit={addQuick}>
            <select className="input" value={n.brand_id || ''} onChange={(e) => setN((p) => ({ ...p, brand_id: e.target.value }))}>
              <option value="">No brand</option>
              {divBrands.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
            <input className="input" placeholder="Product name" value={n.name || ''} onChange={(e) => setN((p) => ({ ...p, name: e.target.value }))} />
            <input className="input" type="number" step="0.01" min="0" placeholder="PTR" value={n.ptr ?? ''} onChange={(e) => setN((p) => ({ ...p, ptr: e.target.value }))} />
            <input className="input" type="number" step="0.01" min="0" placeholder="MRP" value={n.mrp ?? ''} onChange={(e) => setN((p) => ({ ...p, mrp: e.target.value }))} />
            <button className="btn btn-primary btn-sm" disabled={busy}>{busy ? 'Saving…' : 'Add & select'}</button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setQuick(false)}>Cancel</button>
          </form>
        )}
        {products.length > 0 && (
          <div className="prod-picker-selected">
            <span className="muted">Selected ({products.length}) — set this campaign's constraints per product:</span>
            {products.map((p) => (
              <div key={p.id || p._k} className="prod-constraint-row">
                <span className="prod-picker-name"><strong>{p.name || 'Unnamed'}</strong>
                  {p.brand_name || p.ptr ? <span className="muted cell-sub">
                    {[p.brand_name, p.ptr ? `PTR ₹${p.ptr}` : ''].filter(Boolean).join(' · ')}
                  </span> : null}
                </span>
                <span className="prod-constraint-inputs">
                  <label>Min qty
                    <input className="input" type="number" min="1" value={p.min_quantity ?? 1}
                      onChange={(e) => setCon(p, 'min_quantity', Number(e.target.value))} />
                  </label>
                  <label>Min POB
                    <input className="input" type="number" step="0.01" min="0" value={p.min_pob ?? 0}
                      onChange={(e) => setCon(p, 'min_pob', Number(e.target.value))} />
                  </label>
                  <label>Max POB
                    <input className="input" type="number" step="0.01" min="0" value={p.max_pob ?? ''}
                      onChange={(e) => setCon(p, 'max_pob', e.target.value === '' ? null : Number(e.target.value))} />
                  </label>
                  <label className="check">Scheme eligible
                    <input type="checkbox" checked={!!p.scheme_eligibility}
                      onChange={(e) => setCon(p, 'scheme_eligibility', e.target.checked)} />
                  </label>
                  <button type="button" className="chip-x" onClick={() => setProducts((prev) => prev.filter((x) => x !== p))}>✕</button>
                </span>
              </div>
            ))}
          </div>
        )}
        <div className="prod-builder-foot">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setQuick(true)}>+ New product (add to catalogue)</button>
          <span className="muted">Selected products are added to this campaign automatically.</span>
        </div>
      </div>
    </>
  );
}

export function BrandsTab({ base }) {
  const { data, loading, error, run } = useAsync(() => api(`${base}/brands`));
  const divisions = useAsync(() => api(`${base}/divisions`));
  const [q, setQ] = useState('');
  const [divFilter, setDivFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [editing, setEditing] = useState(null);
  const [viewBrand, setViewBrand] = useState(null);
  const [busy, setBusy] = useState(false);

  const brandProducts = useAsync(
    () => viewBrand ? api(`${base}/products?brand_id=${viewBrand.id}`) : Promise.resolve({ items: [] }),
    [viewBrand?.id, run]);

  if (loading) return <Spinner label="Loading brands..." />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const divs = divisions.data?.items || [];
  const singleDivision = divisions.data ? divs.length <= 1 : false;
  const rows = (data?.items || []).filter((r) => {
    const matchQ = !q || (r.name || '').toLowerCase().includes(q.toLowerCase())
      || (r.code || '').toLowerCase().includes(q.toLowerCase());
    const matchD = !divFilter || String(r.division_id) === String(divFilter);
    const matchS = !statusFilter || r.status === statusFilter;
    return matchQ && matchD && matchS;
  });

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (editing.id) await api(`${base}/brands/${editing.id}`, { method: 'PUT', body: editing });
      else await api(`${base}/brands`, { method: 'POST', body: editing });
      toast(editing.id ? 'Updated' : 'Created', 'success');
      setEditing(null); run();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const toggleStatus = async (row) => {
    const next = row.status === 'active' ? 'inactive' : 'active';
    try {
      await api(`${base}/brands/${row.id}`, { method: 'PUT', body: { status: next } });
      toast(next === 'active' ? 'Brand activated' : 'Brand deactivated', 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const set = (k) => (e) => {
    const v = e.target.value;
    setEditing((p) => ({ ...p, [k]: v === '' ? null : v }));
  };

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'name', label: 'Brand' },
    { key: 'code', label: 'Code' },
    { key: 'product_count', label: 'Products', render: (r) => r.product_count ?? 0 },
    ...(singleDivision ? [] : [{ key: 'division_name', label: 'Division', render: (r) => (r.division_name
      ? <span>{r.division_name}</span>
      : <span className="muted" title="Campaigns for this brand cannot be reached from a division login link">Unassigned</span>) }]),
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => setViewBrand(r)}>View</button>
        <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
        <button className="btn-link" onClick={() => toggleStatus(r)}>{r.status === 'active' ? 'Deactivate' : 'Activate'}</button>
      </span>
    ) },
  ];

  const unassigned = (data?.items || []).filter((b) => !b.division_id).length;

  return (
    <div>
      <PageHeader title="Brands" subtitle="Each brand belongs to a division. Campaigns are then run against that division's brands."
        actions={<>
          <SearchBox value={q} onChange={setQ} />
          {!singleDivision && (
            <Select value={divFilter} onChange={(e) => setDivFilter(e.target.value)}
              placeholder="All divisions"
              options={divs.map((d) => ({ value: d.id, label: d.name }))} />
          )}
          <Select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
            placeholder="All statuses"
            options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} />
          <button className="btn btn-primary" onClick={() => setEditing({})}>+ Add</button>
        </>} />
      {!singleDivision && unassigned > 0 && (
        <div className="scope-note">
          <span>{unassigned} brand{unassigned > 1 ? 's have' : ' has'} no division</span>
          <strong>assign one so its campaigns belong to a division</strong>
        </div>
      )}
      <Table cols={cols} rows={rows} keyOf={(r) => r.id} empty="No brands" />
      {editing && (
        <Modal open wide title={editing.id ? 'Edit Brand' : 'New Brand'} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="brand-form" disabled={busy}>{busy ? 'Saving...' : 'Save'}</button>
          </>}>
          <form id="brand-form" className="grid-2" onSubmit={submit}>
            <Field label="Name" required><TextInput value={editing.name || ''} onChange={set('name')} required /></Field>
            <Field label="Code"><TextInput value={editing.code || ''} onChange={set('code')} /></Field>
            {!singleDivision && (
              <Field label="Division" hint="Which division promotes this brand">
                <Select value={editing.division_id || ''} onChange={set('division_id')}
                  placeholder="Unassigned"
                  options={divs.map((d) => ({ value: d.id, label: d.name }))} />
              </Field>
            )}
            <Field label="Status"><Select value={editing.status || 'active'} onChange={set('status')}
              options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} /></Field>
            <Field label="Description" className="span-2"><TextArea rows={3} value={editing.description || ''} onChange={set('description')} /></Field>
          </form>
        </Modal>
      )}
      {viewBrand && (
        <Modal open wide title={`Products — ${viewBrand.name}`} onClose={() => setViewBrand(null)}
          footer={<button className="btn" onClick={() => setViewBrand(null)}>Close</button>}>
          {brandProducts.loading ? <Spinner label="Loading products..." /> : (
            <Table cols={[
              { key: 'name', label: 'Product' },
              { key: 'sku', label: 'SKU', render: (r) => <code>{r.sku || '—'}</code> },
              { key: 'ptr', label: 'PTR', thClass: 'num', tdClass: 'num', render: (r) => r.ptr ? `₹${r.ptr}` : '—' },
              { key: 'mrp', label: 'MRP', thClass: 'num', tdClass: 'num', render: (r) => r.mrp ? `₹${r.mrp}` : '—' },
              { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
            ]} rows={brandProducts.data?.items || []} keyOf={(r) => r.id} empty="No products for this brand yet" />
          )}
        </Modal>
      )}
    </div>
  );
}

function DivisionsTab({ base }) {
  const { data, loading, error, run } = useAsync(() => api(`${base}/divisions`));
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);

  if (loading) return <Spinner label="Loading divisions..." />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const rows = (data?.items || []).filter((r) => !q
    || (r.name || '').toLowerCase().includes(q.toLowerCase())
    || (r.code || '').toLowerCase().includes(q.toLowerCase()));

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      if (editing.id) await api(`${base}/divisions/${editing.id}`, { method: 'PUT', body: editing });
      else await api(`${base}/divisions`, { method: 'POST', body: editing });
      toast(editing.id ? 'Updated' : 'Created', 'success');
      setEditing(null); run();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const del = async (row) => {
    if (!window.confirm('Delete ' + row.name + '?')) return;
    try { await api(`${base}/divisions/${row.id}`, { method: 'DELETE' }); toast('Deleted', 'success'); run(); }
    catch (err) { toast(err.message, 'error'); }
  };

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'name', label: 'Name' },
    { key: 'code', label: 'Code' },
    { key: 'brand_count', label: 'Brands' },
    { key: 'campaign_count', label: 'Campaigns' },
    { key: 'user_count', label: 'Users' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
        <button className="btn-link danger" onClick={() => del(r)}>Delete</button>
      </span>
    ) },
  ];

  const set = (k) => (e) => {
    const v = e.target.value;
    setEditing((p) => ({ ...p, [k]: v === '' ? null : v }));
  };

  return (
    <div>
      <PageHeader title="Internal divisions" subtitle="Organise users and campaigns into internal business units."
        actions={<>
          <SearchBox value={q} onChange={setQ} />
          <button className="btn btn-primary" onClick={() => setEditing({})}>+ Add</button>
        </>} />
      <Table cols={cols} rows={rows} keyOf={(r) => r.id} empty="No divisions" />
      {editing && (
        <Modal open wide title={editing.id ? 'Edit Division' : 'New Division'} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
            <button className="btn btn-primary" form="div-form" disabled={busy}>{busy ? 'Saving...' : 'Save'}</button>
          </>}>
          <form id="div-form" className="grid-2" onSubmit={submit}>
            <Field label="Name" required><TextInput value={editing.name || ''} onChange={set('name')} required /></Field>
            <Field label="Code"><TextInput value={editing.code || ''} onChange={set('code')} /></Field>
            <Field label="Status"><Select value={editing.status || 'active'} onChange={set('status')}
              options={['active', 'inactive'].map((o) => ({ value: o, label: o }))} /></Field>
            <Field label="Description" className="span-2"><TextArea rows={3} value={editing.description || ''} onChange={set('description')} /></Field>
          </form>
        </Modal>
      )}
    </div>
  );
}