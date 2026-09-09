import { useEffect, useMemo, useRef, useState } from 'react';
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
  const brands = useAsync(() => api(`${base}/brands`));
  const divisions = useAsync(() => api(`${base}/divisions`));
  const [q, setQ] = useState('');
  const [editing, setEditing] = useState(null);
  const [ext, setExt] = useState(null);

  if (loading) return <Spinner label="Loading campaigns…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const rows = (data?.items || []).filter((r) => !q
    || (r.name || '').toLowerCase().includes(q.toLowerCase()));

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
    { key: 'pob_count', label: 'POBs' },
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: 'active', label: 'Active', render: (r) => (
      <button className={`btn btn-sm ${r.active ? 'btn-primary' : ''}`}
        style={{ minWidth: 64, fontSize: 12 }}
        onClick={() => toggleActive(r)}>
        {r.active ? 'ON' : 'OFF'}
      </button>
    ) },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => setExt({ id: r.id, mode: 'days', days: 30 })}>Extend</button>
        <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
        <button className="btn-link danger" onClick={() => del(r)}>Delete</button>
      </span>
    ) },
  ];

  return (
    <div>
      <PageHeader title="Campaigns" subtitle="Create and manage the division's POB schemes"
        actions={<>
          <SearchBox value={q} onChange={setQ} />
          <button className="btn btn-primary" onClick={() => setEditing({})}>+ New campaign</button>
        </>} />
      <Table cols={cols} rows={rows} keyOf={(r) => r.id} empty="No campaigns yet" />
      {editing && (
        <CampaignModal editing={editing} base={base}
          brands={brands.data?.items || []} divisions={divisions.data?.items || []}
          onClose={() => setEditing(null)} onDone={() => { setEditing(null); run(); }} />
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
  const [f, setF] = useState({
    active: true, invoice_verification_required: true, status: 'draft', scheme_type: 'others', ...editing,
  });
  const [products, setProducts] = useState(isEdit ? [] : [{}]);
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState({ logo: null, banner: null });
  const loaded = useRef(false);
  const [step, setStep] = useState(0);
  const STEPS = ['Details', 'Brand & Products', 'Timeline & Rules', 'Branding & Submissions', 'Review'];
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
      setF((prev) => ({
        ...prev,
        division_id: prev.division_id ?? detail.data.division_id ?? '',
        brand_id: prev.brand_id ?? detail.data.brand_id ?? '',
        brand_mode: brandIds.length > 1 ? 'multiple' : 'single',
        brand_ids: brandIds,
        logo_path: detail.data.logo_path ?? prev.logo_path ?? '',
        banner_path: detail.data.banner_path ?? prev.banner_path ?? '',
      }));
      setProducts((detail.data.products || []).map((x) => ({ ...x })));
    }
  }, [isEdit, detail.data]);

  const set = (k) => (e) => {
    const v = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setF((p) => ({ ...p, [k]: v }));
  };
  const setP = (i, k) => (e) => {
    const v = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    setProducts((prev) => prev.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  };
  const addRow = () => setProducts((prev) => [...prev, {}]);
  const delRow = (i) => setProducts((prev) => prev.filter((_, j) => j !== i));

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
  const isCreate = !isEdit;

  return (
    <Modal open wide title={isEdit ? `Edit ${editing.name}` : 'New campaign'} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        {step > 0 && <button className="btn" type="button" onClick={() => setStep((s) => s - 1)}>← Back</button>}
        {step < lastStep ? (
          <button className="btn btn-primary" type="button" onClick={nextStep}>Next →</button>
        ) : (
          <button className="btn btn-primary" form="camp-form" disabled={busy}>{busy ? 'Saving…' : (isCreate ? 'Create campaign' : 'Save changes')}</button>
        )}
      </>}>
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
            <Field label="Status"><Select value={f.status || 'draft'} onChange={set('status')}
              options={['draft', 'active', 'completed', 'paused'].map((o) => ({ value: o, label: o }))} /></Field>
            <div className="span-2">
              <label className="check"><input type="checkbox" checked={f.active} onChange={set('active')} /> Active</label>
            </div>
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
            <ProductBuilder products={products} brands={divBrands} setP={setP} addRow={addRow} delRow={delRow} />
          </>
        )}
        {step === 2 && (
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
        {step === 3 && (
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
        {step === 4 && (
          <div>
            <h3 className="sub-head">Review campaign</h3>
            <div className="kv-grid">
              <span>Campaign name<strong>{f.name || '—'}</strong></span>
              <span>Division<strong>{reviewDivision}</strong></span>
              <span>Brand(s)<strong>{reviewBrands}</strong></span>
              <span>Position (PTR/PTS rows)<strong>{reviewProducts.length}</strong></span>
              <span>Scheme type<strong>{f.scheme_type || 'others'}</strong></span>
              <span>Status<strong>{f.status || 'draft'}</strong></span>
              <span>Active<strong>{f.active ? 'Yes' : 'No'}</strong></span>
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
            <p className="ai-note">Review the details above — you can go back anytime to make changes before saving.</p>
          </div>
        )}
      </form>
    </Modal>
  );
}

function ProductBuilder({ products, brands, setP, addRow, delRow }) {
  return (
    <>
      <h3 className="sub-head">Campaign brands &amp; prices (PTR / PTS)</h3>
      <div className="prod-builder">
        <div className="prod-builder-head">
          <span>Brand</span><span>Product name</span><span>Pack</span>
          <span>PTR (₹)</span><span>PTS (₹)</span><span>MRP (₹)</span>
          <span>Min qty</span><span>Min POB</span><span>Max POB</span><span />
        </div>
        {products.map((p, i) => (
          <div className="prod-builder-row" key={i}>
            <select className="input" value={p.brand_id || ''} onChange={setP(i, 'brand_id')}>
              <option value="">— select —</option>
              {brands.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
            <input className="input" placeholder="Product name" value={p.name || ''} onChange={setP(i, 'name')} />
            <input className="input" placeholder="Pack" value={p.pack || ''} onChange={setP(i, 'pack')} />
            <input className="input" type="number" step="0.01" min="0" value={p.ptr ?? ''} onChange={setP(i, 'ptr')} />
            <input className="input" type="number" step="0.01" min="0" value={p.pts ?? ''} onChange={setP(i, 'pts')} />
            <input className="input" type="number" step="0.01" min="0" value={p.mrp ?? ''} onChange={setP(i, 'mrp')} />
            <input className="input" type="number" min="0" value={p.min_quantity ?? 1} onChange={setP(i, 'min_quantity')} />
            <input className="input" type="number" step="0.01" min="0" value={p.min_pob ?? 0} onChange={setP(i, 'min_pob')} />
            <input className="input" type="number" step="0.01" min="0" value={p.max_pob ?? ''} onChange={setP(i, 'max_pob')} />
            <button type="button" className="btn-link danger" onClick={() => delRow(i)} title="Remove row">✕</button>
          </div>
        ))}
        <div className="prod-builder-foot">
          <button type="button" className="btn btn-ghost btn-sm" onClick={addRow}>+ Add product</button>
          <span className="muted">Enter the brands and their PTR/PTS prices for this campaign.</span>
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
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);

  if (loading) return <Spinner label="Loading brands..." />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const divs = divisions.data?.items || [];
  const singleDivision = divisions.data ? divs.length <= 1 : false;
  const rows = (data?.items || []).filter((r) => {
    const matchQ = !q || (r.name || '').toLowerCase().includes(q.toLowerCase())
      || (r.code || '').toLowerCase().includes(q.toLowerCase());
    const matchD = !divFilter || String(r.division_id) === String(divFilter);
    return matchQ && matchD;
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

  const del = async (row) => {
    if (!window.confirm('Delete ' + row.name + '?')) return;
    try { await api(`${base}/brands/${row.id}`, { method: 'DELETE' }); toast('Deleted', 'success'); run(); }
    catch (err) { toast(err.message, 'error'); }
  };

  const set = (k) => (e) => {
    const v = e.target.value;
    setEditing((p) => ({ ...p, [k]: v === '' ? null : v }));
  };

  const cols = [
    { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
    { key: 'name', label: 'Brand' },
    { key: 'code', label: 'Code' },
    ...(singleDivision ? [] : [{ key: 'division_name', label: 'Division', render: (r) => (r.division_name
      ? <span>{r.division_name}</span>
      : <span className="muted" title="Campaigns for this brand cannot be reached from a division login link">Unassigned</span>) }]),
    { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
    { key: '_a', label: '', thClass: 'actions-th', render: (r) => (
      <span className="row-actions">
        <button className="btn-link" onClick={() => setEditing({ ...r })}>Edit</button>
        <button className="btn-link danger" onClick={() => del(r)}>Delete</button>
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