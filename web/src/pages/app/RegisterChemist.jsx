import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../../api';
import { Field, PageHeader, Select, TextInput, toast } from '../../ui';

const BLANK = {
  name: '', shop_name: '', owner_name: '', mobile: '', alternate_mobile: '', email: '',
  gst: '', dl_number: '', upi_id: '', ocid: '', doctor_name: '', category: '', area: '',
  address: '', city: '', district: '', state: '', pin: '', latitude: '', longitude: '',
  status: 'active',
};

const FIELDS = [
  { name: 'name', label: 'Chemist / shop owner name', required: true },
  { name: 'shop_name', label: 'Shop name' },
  { name: 'owner_name', label: 'Owner name' },
  { name: 'mobile', label: 'Mobile' },
  { name: 'alternate_mobile', label: 'Alternate mobile' },
  { name: 'email', label: 'Email', type: 'email' },
  { name: 'gst', label: 'GST number' },
  { name: 'dl_number', label: 'Drug licence number' },
  { name: 'upi_id', label: 'UPI / gratification number' },
  { name: 'ocid', label: 'Doctor OCID' },
  { name: 'doctor_name', label: 'Referring doctor' },
  { name: 'category', label: 'Category' },
  { name: 'area', label: 'Area' },
];

export default function RegisterChemist({ demo }) {
  const navigate = useNavigate();
  const [f, setF] = useState({ ...BLANK });
  const [busy, setBusy] = useState(false);
  const [posts, setPosts] = useState(null);
  const [lookup, setLookup] = useState('idle');
  const [looking, setLooking] = useState(null);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));

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

  const submit = async (e) => {
    e.preventDefault();
    if (!f.name.trim()) { toast('Name is required', 'error'); return; }
    if (f.pin && !/^\d{6}$/.test(f.pin.trim())) { toast('PIN code must be 6 digits', 'error'); return; }
    setBusy(true);
    try {
      if (demo) {
        await new Promise((r) => setTimeout(r, 400));
        toast('Test mode: chemist details are valid and would be saved. Nothing was written to the database.', 'success');
        setF({ ...BLANK });
        setPosts(null);
        setLookup('idle');
        setLooking(null);
        return;
      }
      await api('/api/v1/chemists', {
        method: 'POST',
        body: { ...f, latitude: f.latitude === '' ? null : f.latitude, longitude: f.longitude === '' ? null : f.longitude },
      });
      toast('Chemist registered', 'success');
      setF({ ...BLANK });
      setPosts(null);
      setLookup('idle');
      setLooking(null);
    } catch (err) { toast(err.message, 'error'); } finally { setBusy(false); }
  };

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
      <div className="card">
        <form onSubmit={submit}>
          <h4 className="section-title">Details</h4>
          <div className="grid-2">
            {FIELDS.map((fld) => (
              <Field key={fld.name} label={fld.label} required={fld.required}>
                <TextInput type={fld.type || 'text'} value={f[fld.name] || ''} onChange={set(fld.name)} required={fld.required} />
              </Field>
            ))}
          </div>

          <h4 className="section-title" style={{ marginTop: 18 }}>Address &amp; location</h4>
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
            <p className="field-hint" style={{ marginTop: 8, color: '#b91c1c' }}>No location found for this PIN. Fill the fields manually.</p>
          )}
          {posts && posts.length > 1 && (
            <p className="field-hint" style={{ marginTop: 8 }}>{posts.length} post offices found for PIN {looking}. Pick the right one above.</p>
          )}
          <p className="field-hint" style={{ marginTop: 8 }}>New chemists are registered with status <strong>active</strong> by default.</p>

          <div className="page-actions" style={{ marginTop: 18 }}>
            <button type="button" className="btn" onClick={() => !demo && navigate('/app/chemists')}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? 'Registering…' : demo ? 'Preview registration' : 'Register chemist'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
