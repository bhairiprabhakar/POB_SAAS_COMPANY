import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../../api';
import {
  Badge, EmptyState, ErrorBox, Field, Modal, PageHeader, Select,
  TextArea, TextInput, toast, useAsync,
} from '../../ui';

const TYPE_GROUPS = [
  { label: 'Text & numbers', options: [
    { value: 'short_text', label: 'Short text' },
    { value: 'long_text', label: 'Long text (paragraph)' },
    { value: 'number', label: 'Number' },
    { value: 'currency', label: 'Currency (₹)' },
    { value: 'percentage', label: 'Percentage' },
    { value: 'email', label: 'Email' },
    { value: 'phone', label: 'Phone' },
    { value: 'url', label: 'Link (URL)' },
  ] },
  { label: 'Dates', options: [
    { value: 'date', label: 'Date' },
    { value: 'date_range', label: 'Date range' },
  ] },
  { label: 'Choices', options: [
    { value: 'select', label: 'Dropdown (single choice)' },
    { value: 'radio', label: 'Radio buttons (single choice)' },
    { value: 'multiselect', label: 'Multi-select chips' },
    { value: 'checkbox_group', label: 'Checkbox group' },
    { value: 'boolean', label: 'Yes / No' },
    { value: 'rating', label: 'Rating scale' },
  ] },
  { label: 'Attachments & layout', options: [
    { value: 'file', label: 'File / document upload' },
    { value: 'section_header', label: 'Section header (no input)' },
  ] },
];
const TYPE_LABEL = Object.fromEntries(TYPE_GROUPS.flatMap((g) => g.options).map((o) => [o.value, o.label]));
const NEEDS_OPTIONS = new Set(['select', 'radio', 'multiselect', 'checkbox_group']);
const NEEDS_MINMAX = new Set(['currency', 'percentage', 'rating']);

const PRESETS = [
  { label: 'Budget Cap', field_type: 'currency', help_text: 'Approved budget ceiling for this campaign' },
  { label: 'Priority', field_type: 'select', options: [{ value: 'low', label: 'Low' }, { value: 'medium', label: 'Medium' }, { value: 'high', label: 'High' }, { value: 'critical', label: 'Critical' }] },
  { label: 'Risk Level', field_type: 'radio', options: [{ value: 'low', label: 'Low' }, { value: 'medium', label: 'Medium' }, { value: 'high', label: 'High' }] },
  { label: 'Internal Reference Code', field_type: 'short_text' },
  { label: 'Requires Manager Pre-Approval', field_type: 'boolean' },
  { label: 'Reference Document', field_type: 'file' },
  { label: 'Expected ROI %', field_type: 'percentage' },
  { label: 'Campaign Owner Contact', field_type: 'phone' },
  { label: 'Campaign Notes', field_type: 'long_text' },
  { label: 'Promo Validity Window', field_type: 'date_range' },
];

const slugify = (s) => (s || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');

export default function CampaignFieldTemplates() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/campaign-field-templates?include_inactive=true'));
  const [editing, setEditing] = useState(null);
  const [adding, setAdding] = useState(false);

  const items = (data?.items || []).slice().sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));

  const move = async (t, dir) => {
    const idx = items.findIndex((x) => x.id === t.id);
    const swapWith = items[idx + dir];
    if (!swapWith) return;
    try {
      await api(`/api/v1/campaign-field-templates/${t.id}`, { method: 'PUT', body: { sort_order: swapWith.sort_order ?? 0 } });
      await api(`/api/v1/campaign-field-templates/${swapWith.id}`, { method: 'PUT', body: { sort_order: t.sort_order ?? 0 } });
      run();
    } catch (err) { toast(err.message, 'error'); }
  };

  const toggleActive = async (t) => {
    try {
      await api(`/api/v1/campaign-field-templates/${t.id}`, { method: 'PUT', body: { active: !t.active } });
      toast(t.active ? 'Field deactivated' : 'Field activated', 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
  };

  if (loading) return null;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  return (
    <div>
      <PageHeader title="Campaign Field Templates"
        subtitle="Extra fields your division captures on every campaign — no code change needed."
        actions={<>
          <Link className="btn" to="/app/campaigns">← Back to campaigns</Link>
          <button className="btn btn-primary" onClick={() => setAdding(true)}>+ Add field</button>
        </>} />

      {items.length === 0 ? (
        <EmptyState text="No custom fields yet — add one to start capturing extra campaign details" />
      ) : (
        <div className="admin-list">
          {items.map((t, i) => (
            <div key={t.id} className="admin-row">
              <div className="admin-id">
                <strong>{t.label}</strong>
                <span>{t.field_key}</span>
              </div>
              <div className="admin-badges">
                <Badge tone="blue">{TYPE_LABEL[t.field_type] || t.field_type}</Badge>
                {t.required && <Badge tone="amber">Required</Badge>}
                <Badge tone={t.active ? 'green' : 'gray'}>{t.active ? 'Active' : 'Inactive'}</Badge>
              </div>
              <div className="admin-actions">
                <button className="btn btn-sm" disabled={i === 0} onClick={() => move(t, -1)}>↑</button>
                <button className="btn btn-sm" disabled={i === items.length - 1} onClick={() => move(t, 1)}>↓</button>
                <button className="btn btn-sm" onClick={() => setEditing({ ...t })}>Edit</button>
                <button className="btn btn-sm" onClick={() => toggleActive(t)}>{t.active ? 'Deactivate' : 'Activate'}</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {adding && (
        <FieldModal onClose={() => setAdding(false)} onDone={() => { setAdding(false); run(); }} />
      )}
      {editing && (
        <FieldModal field={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); run(); }} />
      )}
    </div>
  );
}

function FieldModal({ field, onClose, onDone }) {
  const isEdit = !!field;
  const [f, setF] = useState(field ? {
    ...field, options: field.options || [],
  } : { label: '', field_key: '', field_type: 'short_text', options: [], required: false, help_text: '', min: '', max: '' });
  const [keyTouched, setKeyTouched] = useState(isEdit);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const applyPreset = (p) => {
    setF((prev) => ({
      ...prev, label: p.label, field_key: slugify(p.label), field_type: p.field_type,
      options: p.options || [], help_text: p.help_text || '',
    }));
    setKeyTouched(false);
  };
  const setLabel = (e) => {
    const label = e.target.value;
    setF((p) => ({ ...p, label, field_key: keyTouched ? p.field_key : slugify(label) }));
  };

  const addOption = () => setF((p) => ({ ...p, options: [...p.options, { value: '', label: '' }] }));
  const updateOption = (i, k, v) => setF((p) => ({
    ...p, options: p.options.map((o, idx) => idx === i ? { ...o, [k]: v, ...(k === 'label' ? { value: o.value || slugify(v) } : {}) } : o),
  }));
  const removeOption = (i) => setF((p) => ({ ...p, options: p.options.filter((_, idx) => idx !== i) }));

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (!f.label.trim() || !f.field_key.trim()) {
      const msg = 'Label and field key are required'; setError(msg); toast(msg, 'error'); return;
    }
    if (NEEDS_OPTIONS.has(f.field_type) && f.options.filter((o) => o.label.trim()).length === 0) {
      const msg = 'Add at least one option'; setError(msg); toast(msg, 'error'); return;
    }
    setBusy(true);
    try {
      const options = NEEDS_OPTIONS.has(f.field_type)
        ? f.options.filter((o) => o.label.trim()).map((o) => ({ value: o.value || slugify(o.label), label: o.label }))
        : NEEDS_MINMAX.has(f.field_type)
          ? { min: f.min || undefined, max: f.max || undefined }
          : [];
      const body = {
        label: f.label, field_key: f.field_key, field_type: f.field_type,
        options, required: !!f.required, help_text: f.help_text || null,
      };
      if (isEdit) await api(`/api/v1/campaign-field-templates/${field.id}`, { method: 'PUT', body });
      else await api('/api/v1/campaign-field-templates', { method: 'POST', body });
      toast(isEdit ? 'Field updated' : 'Field added', 'success');
      onDone();
    } catch (err) {
      const msg = err.message || 'Save failed'; setError(msg); toast(msg, 'error');
    } finally { setBusy(false); }
  };

  return (
    <Modal open wide title={isEdit ? `Edit "${field.label}"` : 'Add a campaign field'} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" form="field-form" disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
      </>}>
      <form id="field-form" onSubmit={submit}>
        {error && <ErrorBox error={error} />}
        {!isEdit && (
          <>
            <h5 className="form-section">Quick add</h5>
            <div className="row" style={{ gap: 6, flexWrap: 'wrap', marginBottom: 14 }}>
              {PRESETS.map((p) => (
                <button key={p.label} type="button" className="btn btn-sm" onClick={() => applyPreset(p)}>{p.label}</button>
              ))}
            </div>
          </>
        )}
        <h5 className="form-section">Field</h5>
        <div className="grid-2">
          <Field label="Label" required><TextInput value={f.label} onChange={setLabel} required autoFocus /></Field>
          <Field label="Field key" required hint="Used internally — auto-generated from the label">
            <TextInput value={f.field_key} onChange={(e) => { setKeyTouched(true); setF((p) => ({ ...p, field_key: slugify(e.target.value) })); }} required />
          </Field>
          <Field label="Type" required>
            <Select value={f.field_type} onChange={(e) => setF((p) => ({ ...p, field_type: e.target.value, options: [] }))}
              placeholder={false}
              options={TYPE_GROUPS.flatMap((g) => g.options)} />
          </Field>
          {f.field_type !== 'section_header' && (
            <Field label="Required"><label className="check"><input type="checkbox" checked={!!f.required}
              onChange={(e) => setF((p) => ({ ...p, required: e.target.checked }))} /> This field is required</label></Field>
          )}
          <Field label="Help text" className="span-2"><TextArea rows={2} value={f.help_text || ''}
            onChange={(e) => setF((p) => ({ ...p, help_text: e.target.value }))} /></Field>
        </div>

        {NEEDS_OPTIONS.has(f.field_type) && (
          <>
            <h5 className="form-section">Options</h5>
            {f.options.map((o, i) => (
              <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
                <TextInput placeholder="Option label" value={o.label} onChange={(e) => updateOption(i, 'label', e.target.value)} />
                <button type="button" className="btn btn-sm btn-danger" onClick={() => removeOption(i)}>Remove</button>
              </div>
            ))}
            <button type="button" className="btn btn-sm" onClick={addOption}>+ Add option</button>
          </>
        )}

        {NEEDS_MINMAX.has(f.field_type) && (
          <>
            <h5 className="form-section">Range (optional)</h5>
            <div className="grid-2">
              <Field label="Min"><TextInput type="number" value={f.min || ''} onChange={(e) => setF((p) => ({ ...p, min: e.target.value }))} /></Field>
              <Field label="Max"><TextInput type="number" value={f.max || ''} onChange={(e) => setF((p) => ({ ...p, max: e.target.value }))} /></Field>
            </div>
          </>
        )}
      </form>
    </Modal>
  );
}
