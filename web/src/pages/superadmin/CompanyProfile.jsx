import { useEffect, useState } from 'react';
import { api } from '../../api';
import { ErrorBox, PageHeader, TextInput, toast, useAsync } from '../../ui';

const FIELD_LABELS = {
  legal_name: 'Legal name',
  display_name: 'Display name',
  address: 'Address',
  city: 'City',
  state: 'State',
  pincode: 'PIN code',
  gstin: 'GSTIN',
  contact_number: 'Contact number',
  official_email: 'Official email',
  website: 'Website',
};

export default function CompanyProfile() {
  const { data, error, run } = useAsync(() => api('/api/v1/superadmin/company-profile'));
  const [form, setForm] = useState({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (data) setForm({ ...data });
  }, [data]);

  if (error) return <ErrorBox error={error} onRetry={run} />;
  if (!data) return <PageHeader title="Company Profile" subtitle="Loading…" />;

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const save = async (e) => {
    e.preventDefault();
    if (!form.display_name?.trim() || !form.legal_name?.trim()) {
      toast('Legal name and display name are required', 'error');
      return;
    }
    setBusy(true);
    try {
      const body = {};
      for (const k of Object.keys(FIELD_LABELS)) body[k] = (form[k] ?? '').trim();
      await api('/api/v1/superadmin/company-profile', { method: 'PUT', body });
      toast('Company profile saved', 'success');
      run();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <PageHeader title="Company Profile" subtitle="The registered business behind this platform. Shown to division admins and on official documents (GSTIN, address, contact)." />

      <form className="card" onSubmit={save}>
        <h4 className="section-title">Business identity</h4>
        <div className="grid-2">
          <TextInput label={FIELD_LABELS.legal_name} value={form.legal_name || ''} onChange={set('legal_name')} required />
          <TextInput label={FIELD_LABELS.display_name} value={form.display_name || ''} onChange={set('display_name')} required />
        </div>
        <div className="grid-2" style={{ marginTop: 10 }}>
          <TextInput label={FIELD_LABELS.official_email} value={form.official_email || ''} onChange={set('official_email')} />
          <TextInput label={FIELD_LABELS.contact_number} value={form.contact_number || ''} onChange={set('contact_number')} />
        </div>
        <div className="grid-2" style={{ marginTop: 10 }}>
          <TextInput label={FIELD_LABELS.gstin} value={form.gstin || ''} onChange={set('gstin')} />
          <TextInput label={FIELD_LABELS.website} value={form.website || ''} onChange={set('website')} />
        </div>

        <h4 className="section-title" style={{ marginTop: 18 }}>Registered address</h4>
        <TextInput label={FIELD_LABELS.address} value={form.address || ''} onChange={set('address')} />
        <div className="grid-3" style={{ marginTop: 10 }}>
          <TextInput label={FIELD_LABELS.city} value={form.city || ''} onChange={set('city')} />
          <TextInput label={FIELD_LABELS.state} value={form.state || ''} onChange={set('state')} />
          <TextInput label={FIELD_LABELS.pincode} value={form.pincode || ''} onChange={set('pincode')} />
        </div>

        <div className="form-actions" style={{ marginTop: 18 }}>
          <button className="btn btn-primary" disabled={busy}>
            {busy ? 'Saving…' : 'Save profile'}
          </button>
          <button type="button" className="btn" onClick={run}>Reset</button>
        </div>
      </form>
    </div>
  );
}