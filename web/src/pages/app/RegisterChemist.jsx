import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../../api';
import { Field, Modal, PageHeader, Select, TextInput, toast } from '../../ui';

const BLANK = {
  name: '', shop_name: '', owner_name: '', mobile: '', alternate_mobile: '', email: '',
  gst: '', dl_number: '', ocid: '', doctor_name: '', category: '', area: '',
  address: '', city: '', district: '', state: '', pin: '', latitude: '', longitude: '',
  attachment_type: '', potential_category: '', institution_name: '', institution_type: '',
  institution_department: '', institution_contact_person: '', institution_address: '',
  monthly_business_potential: '', estimated_monthly_sales: '', brand_potential: '',
  strategic_importance: '', last_visit_date: '', visit_frequency: '',
  status: 'active',
};

const STEPS = ['Details', 'Address & location', 'Classification & potential', 'Review'];

const FIELDS = [
  { name: 'name', label: 'Chemist / shop owner name', required: true },
  { name: 'shop_name', label: 'Shop name' },
  { name: 'owner_name', label: 'Owner name' },
  { name: 'mobile', label: 'Mobile' },
  { name: 'alternate_mobile', label: 'Alternate mobile' },
  { name: 'email', label: 'Email', type: 'email' },
  { name: 'gst', label: 'GST number' },
  { name: 'dl_number', label: 'Drug licence number' },
  { name: 'ocid', label: 'Doctor OCID' },
  { name: 'doctor_name', label: 'Referring doctor' },
  { name: 'category', label: 'Category' },
  { name: 'area', label: 'Area' },
];

const DETAIL_LABELS = { ...Object.fromEntries(FIELDS.map((x) => [x.name, x.label])), status: 'Status' };

const ADDR_LABELS = {
  address: 'Address', pin: 'PIN code', city: 'City', district: 'District', state: 'State',
  area: 'Area / zone', latitude: 'Latitude', longitude: 'Longitude',
};

const CLASS_LABELS = {
  attachment_type: 'Attachment type', potential_category: 'Potential category',
  institution_name: 'Institution name', institution_type: 'Institution type',
  institution_department: 'Department', institution_contact_person: 'Contact person',
  institution_address: 'Institution address', monthly_business_potential: 'Monthly business potential (INR)',
  estimated_monthly_sales: 'Estimated monthly sales (INR)', brand_potential: 'Brand potential',
  strategic_importance: 'Strategic importance', visit_frequency: 'Visit frequency',
  last_visit_date: 'Last visit date',
};

const masterName = (masters, kind, code) =>
  ((masters[kind] || []).find((m) => m.code === code)?.name) || code || '—';

function ReviewRows({ f, labels, masters }) {
  const rows = Object.entries(labels).filter(([k]) => {
    const v = f[k];
    return v != null && String(v).trim() !== '';
  });
  if (!rows.length) return <p className="muted" style={{ fontSize: 13 }}>Nothing provided.</p>;
  return (
    <div className="kv-grid">
      {rows.map(([k, label]) => {
        let v = f[k];
        if (k === 'attachment_type') v = masterName(masters, 'attachment_types', v);
        if (k === 'potential_category') v = masterName(masters, 'potential_categories', v);
        return <span key={k}>{label}<strong>{v}</strong></span>;
      })}
    </div>
  );
}

export default function RegisterChemist({ demo }) {
  const navigate = useNavigate();
  const [f, setF] = useState({ ...BLANK });
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [posts, setPosts] = useState(null);
  const [lookup, setLookup] = useState('idle');
  const [looking, setLooking] = useState(null);
  const [dups, setDups] = useState(null);
  const [masters, setMasters] = useState({ attachment_types: [], potential_categories: [] });
  const [guide, setGuide] = useState([]);
  const set = (k) => (e) => {
    let v = e.target.value;
    if (k === 'mobile' || k === 'alternate_mobile') v = v.replace(/\D/g, '').slice(0, 10);
    setF((p) => ({ ...p, [k]: v }));
  };

  useEffect(() => {
    api('/api/v1/chemist-masters').then(setMasters).catch(() => {});
  }, []);

  // Show live campaigns' target states / chemist types / categories so the
  // field user registers the right profile while a rollout is running.
  useEffect(() => {
    api('/api/v1/campaigns?active=true')
      .then((d) => {
        const rows = (d.items || []).filter((c) =>
          ['active', 'scheduled', 'pending_approval'].includes(c.status) &&
          ((c.eligible_states || []).length || (c.eligible_chemist_attachment_types || []).length ||
           (c.eligible_chemist_potential_categories || []).length));
        setGuide(rows.map((c) => ({
          name: c.name,
          states: c.eligible_states || [],
          types: c.eligible_chemist_attachment_types || [],
          cats: c.eligible_chemist_potential_categories || [],
        })));
      })
      .catch(() => setGuide([]));
  }, []);

  const applyPostOffice = (po) => {
    setF((p) => ({ ...p, city: po.city, district: po.district, state: po.state }));
  };

  const lookupPin = async (pin) => {
    const value = (pin ?? f.pin ?? '').trim();
    if (!/^\d{6}$/.test(value)) { toast('Enter a valid 6-digit PIN code', 'error'); return; }
    setLookup('loading');
    try {
      const r = await api(`/api/v1/pincode/${value}`);
      setPosts(r.items || []);
      setLooking(value);
      setLookup(r.items.length ? 'done' : 'error');
      if (r.items.length) applyPostOffice(r.items[0]);
    } catch (err) {
      setPosts(null);
      setLookup('error');
      toast(err.message, 'error');
    }
  };

  const detectLocation = () => {
    if (!navigator.geolocation) { toast('Geolocation not supported by this browser', 'error'); return; }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setF((p) => ({
          ...p,
          latitude: pos.coords.latitude.toFixed(6),
          longitude: pos.coords.longitude.toFixed(6),
        }));
        toast('Location captured', 'success');
      },
      () => toast('Could not detect location', 'error'),
      { enableHighAccuracy: true, timeout: 10000 },
    );
  };

  useEffect(() => {
    detectLocation();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const validate = (s) => {
    if (s === 0 && !f.name.trim()) { toast('Name is required', 'error'); return false; }
    if (s === 1 && f.pin && !/^\d{6}$/.test(f.pin.trim())) { toast('PIN code must be 6 digits', 'error'); return false; }
    return true;
  };

  const nextStep = () => {
    if (!validate(step)) return;
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  };

  const backStep = () => setStep((s) => Math.max(s - 1, 0));

  const submit = async (e) => {
    e.preventDefault();
    if (!validate(step)) return;
    if (!f.name.trim()) { toast('Name is required', 'error'); return; }
    setBusy(true);
    try {
      if (demo) {
        await new Promise((r) => setTimeout(r, 400));
        toast('Test mode: chemist details are valid and would be saved. Nothing was written to the database.', 'success');
        reset();
        return;
      }
      const body = { ...f, latitude: f.latitude === '' ? null : f.latitude, longitude: f.longitude === '' ? null : f.longitude };
      const r = await api('/api/v1/chemists', { method: 'POST', body });
      if (r?.ok === false) { setDups(r.duplicates || []); return; }
      toast('Chemist registered', 'success');
      reset();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const reset = () => {
    setF({ ...BLANK });
    setStep(0);
    setPosts(null);
    setLookup('idle');
    setLooking(null);
    setDups(null);
  };

  const forceRegister = async () => {
    setBusy(true);
    try {
      await api('/api/v1/chemists', {
        method: 'POST',
        body: { ...f, duplicate_checks: [], latitude: f.latitude === '' ? null : f.latitude, longitude: f.longitude === '' ? null : f.longitude },
      });
      toast('Chemist registered', 'success');
      reset();
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

  const reviewSections = [
    { title: 'Details', labels: DETAIL_LABELS },
    { title: 'Address & location', labels: ADDR_LABELS },
    { title: 'Classification & potential', labels: CLASS_LABELS },
  ];

  return (
    <div>
      {demo && (
        <div className="scope-banner" data-tone="amber">
          <span className="scope-dot" />
          <div>
            <strong>Test mode — how your team registers a chemist</strong>
            <small>This is a live preview. Nothing you enter here is saved to the database.</small>
          </div>
        </div>
      )}
      <PageHeader title="Register Chemist"
        subtitle="Add a retail chemist / pharmacy. Enter the 6-digit PIN to auto-fetch city, district & state from the India Post pincode API."
        actions={!demo && <button className="btn" onClick={() => navigate('/app/chemists')}>View chemists</button>} />
      {guide.length > 0 && (
        <div className="scope-banner" data-tone="blue">
          <span className="scope-dot" />
          <div>
            <strong>Live campaigns are targeting these chemist profiles</strong>
            <small>Register chemists that match, so they are eligible for POBs.</small>
            {guide.map((g) => {
              const typeNames = g.types.map((code) => (masters.attachment_types || []).find((m) => m.code === code)?.name || code);
              const catNames = g.cats.map((code) => (masters.potential_categories || []).find((m) => m.code === code)?.name || code);
              return (
                <div key={g.name} className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                  <strong>{g.name}</strong>
                  {g.states.length ? ` · States: ${g.states.join(', ')}` : ' · Any state'}
                  {typeNames.length ? ` · Types: ${typeNames.join(', ')}` : ''}
                  {catNames.length ? ` · Categories: ${catNames.join(', ')}` : ''}
                </div>
              );
            })}
          </div>
        </div>
      )}
      <div className="card">
        <div className="stepper">
          {STEPS.map((label, i) => (
            <button key={label} type="button" className={`step${i === step ? ' active' : ''}${i < step ? ' done' : ''}${i < step ? ' clickable' : ''}`}
              onClick={() => i < step && setStep(i)} disabled={i > step}>
              <span className="step-num">{i < step ? '✓' : i + 1}</span>
              <span className="step-label">{label}</span>
            </button>
          ))}
        </div>
        <form onSubmit={submit}>
          {step === 0 && (
            <>
              <h4 className="section-title">Details</h4>
              <div className="grid-2">
                {FIELDS.map((fld) => (
                  <Field key={fld.name} label={fld.label} required={fld.required}>
                    <TextInput type={fld.type || 'text'} value={f[fld.name] || ''} onChange={set(fld.name)} required={fld.required} />
                  </Field>
                ))}
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                UPI / gratification payout number isn't captured here — once this chemist is saved,
                use <strong>Scan UPI</strong> on their record to scan their QR code (or enter the VPA
                manually), so it's verified against their name before any payout can use it.
              </p>
            </>
          )}

          {step === 1 && (
            <>
              <h4 className="section-title">Address &amp; location</h4>
              <div className="grid-2">
                <Field label="Address" className="span-2">
                  <TextInput value={f.address || ''} onChange={set('address')} />
                </Field>
                <Field label="PIN code" hint="On a 6-digit PIN the city, district & state are fetched from the India Post pincode API">
                  <TextInput value={f.pin || ''} maxLength={6} placeholder="e.g. 500001"
                    onChange={set('pin')}
                    onBlur={(e) => { const v = e.target.value.trim(); if (/^\d{6}$/.test(v) && v !== looking) lookupPin(v); }} />
                </Field>
                <Field label="Location (post office)">
                  <Select value={f.city || ''} onChange={(e) => {
                    const po = posts?.find((p) => p.city === e.target.value);
                    setF((p) => ({ ...p, city: e.target.value, district: po?.district || p.district, state: po?.state || p.state }));
                  }}
                    options={(posts || []).map((p) => ({ value: p.city, label: `${p.city} — ${p.district}` }))} />
                </Field>
                <Field label="City">
                  <TextInput value={f.city || ''} onChange={set('city')} />
                </Field>
                <Field label="District">
                  <TextInput value={f.district || ''} onChange={set('district')} />
                </Field>
                <Field label="State">
                  <TextInput value={f.state || ''} onChange={set('state')} />
                </Field>
                <Field label="Area / zone" hint="Optional sub-locality">
                  <TextInput value={f.area || ''} onChange={set('area')} />
                </Field>
                <Field label="Latitude">
                  <TextInput type="number" step="any" value={f.latitude || ''} onChange={set('latitude')} placeholder="e.g. 17.3850" />
                </Field>
                <Field label="Longitude">
                  <TextInput type="number" step="any" value={f.longitude || ''} onChange={set('longitude')} placeholder="e.g. 78.4867" />
                </Field>
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                <button type="button" className="btn" onClick={() => lookupPin()} disabled={lookup === 'loading'}>
                  {lookup === 'loading' ? 'Fetching…' : 'Fetch from PIN'}
                </button>
                <button type="button" className="btn" onClick={detectLocation}>📍 Use my location</button>
              </div>
              {lookup === 'error' && !posts && (
                <p className="field-hint" style={{ marginTop: 8, color: 'var(--red-text)' }}>No location found for this PIN. Fill the fields manually.</p>
              )}
              {posts && posts.length > 1 && (
                <p className="field-hint" style={{ marginTop: 8 }}>{posts.length} post offices found for PIN {looking}. Pick the right one above.</p>
              )}
            </>
          )}

          {step === 2 && (
            <>
              <h4 className="section-title">Classification &amp; potential</h4>
              <p className="field-hint" style={{ marginBottom: 12 }}>
                Match the profiles your live campaigns are targeting. New chemists are registered with status <strong>active</strong> by default.
              </p>
              <div className="grid-2">
                <Field label="Attachment type" hint="Hospital, retail, chain, online pharmacy…">
                  <Select value={f.attachment_type || ''} onChange={set('attachment_type')}
                    options={(masters.attachment_types || []).map((m) => ({ value: m.code, label: `${m.name} (${m.code})` }))} />
                </Field>
                <Field label="Potential category">
                  <Select value={f.potential_category || ''} onChange={set('potential_category')}
                    options={(masters.potential_categories || []).map((m) => ({ value: m.code, label: `${m.name} (${m.code})` }))} />
                </Field>
                <Field label="Institution name" hint="For hospital / nursing home chemists">
                  <TextInput value={f.institution_name || ''} onChange={set('institution_name')} />
                </Field>
                <Field label="Institution type">
                  <TextInput value={f.institution_type || ''} onChange={set('institution_type')} />
                </Field>
                <Field label="Department">
                  <TextInput value={f.institution_department || ''} onChange={set('institution_department')} />
                </Field>
                <Field label="Contact person">
                  <TextInput value={f.institution_contact_person || ''} onChange={set('institution_contact_person')} />
                </Field>
                <Field label="Institution address" className="span-2">
                  <TextInput value={f.institution_address || ''} onChange={set('institution_address')} />
                </Field>
                <Field label="Monthly business potential (INR)">
                  <TextInput type="number" min="0" value={f.monthly_business_potential || ''} onChange={set('monthly_business_potential')} />
                </Field>
                <Field label="Estimated monthly sales (INR)">
                  <TextInput type="number" min="0" value={f.estimated_monthly_sales || ''} onChange={set('estimated_monthly_sales')} />
                </Field>
                <Field label="Brand potential">
                  <TextInput value={f.brand_potential || ''} onChange={set('brand_potential')} />
                </Field>
                <Field label="Strategic importance">
                  <TextInput value={f.strategic_importance || ''} onChange={set('strategic_importance')} />
                </Field>
                <Field label="Visit frequency">
                  <Select value={f.visit_frequency || ''} onChange={set('visit_frequency')} placeholder="— select —"
                    options={['daily', 'weekly', 'monthly', 'quarterly'].map((o) => ({ value: o, label: o }))} />
                </Field>
                <Field label="Last visit date">
                  <TextInput type="date" value={f.last_visit_date || ''} onChange={set('last_visit_date')} />
                </Field>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <h4 className="section-title">Review &amp; confirm</h4>
              <p className="field-hint" style={{ marginBottom: 12 }}>
                Check the details before registering this chemist. Everything is saved to the division master.
              </p>
              {reviewSections.map((s) => (
                <div key={s.title} style={{ marginBottom: 16 }}>
                  <h5 style={{ margin: '0 0 8px', fontSize: 13, color: 'var(--text-2)' }}>{s.title}</h5>
                  <ReviewRows f={f} labels={s.labels} masters={masters} />
                </div>
              ))}
            </>
          )}

          <div className="page-actions" style={{ marginTop: 18 }}>
            {step > 0 && (
              <button type="button" className="btn" onClick={backStep}>← Back</button>
            )}
            {step < STEPS.length - 1 && (
              <button type="button" className="btn btn-primary" onClick={nextStep}>Next →</button>
            )}
            {step === STEPS.length - 1 && (
              <button type="submit" className="btn btn-primary" disabled={busy}>
                {busy ? 'Registering…' : demo ? 'Preview registration' : 'Register chemist'}
              </button>
            )}
          </div>
        </form>
      </div>

      {dups && (
        <Modal open title="A chemist like this already exists" onClose={() => setDups(null)}
          footer={
            <>
              <button className="btn" onClick={() => !demo && navigate('/app/chemists')}>View existing</button>
              <button className="btn btn-primary" onClick={forceRegister} disabled={busy}>
                {busy ? 'Registering…' : 'Register anyway (new shop)'}
              </button>
            </>
          }>
          <p className="muted">We found existing chemist(s) matching the details you entered. Re-check to avoid registering the same shop twice.</p>
          <div className="card" style={{ marginTop: 12 }}>
            {dups.map((d, i) => (
              <div key={i} className="dup-item" style={{ padding: '8px 0', borderBottom: i < dups.length - 1 ? '1px solid var(--border)' : 'none' }}>
                <strong>{d.chemist.name}</strong>{d.chemist.shop_name ? ` — ${d.chemist.shop_name}` : ''}
                {d.chemist.city ? `, ${d.chemist.city}` : ''}
                <div className="muted" style={{ fontSize: 12 }}>
                  Matched on {d.rule.replace('_', ' ')}{d.chemist.mobile ? ` · ☎ ${d.chemist.mobile}` : ''} · #{d.chemist.id}
                </div>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}