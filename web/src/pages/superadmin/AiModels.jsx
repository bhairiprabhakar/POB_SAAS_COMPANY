import { useEffect, useState } from 'react';
import { api } from '../../api';
import {
  Badge, ErrorBox, Field, PageHeader, Select, Spinner, TextInput, toast, useAsync,
} from '../../ui';

// Model settings: which Gemini model handles each file category + the editable
// USD pricing table. Backed by /api/v1/superadmin/ai-models (platform DB).

export default function AiModels() {
  const { data, loading, error, run } = useAsync(() => api('/api/v1/superadmin/ai-models'));
  const [overrides, setOverrides] = useState(null);
  const [prices, setPrices] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveLabel, setSaveLabel] = useState('');
  const [newModel, setNewModel] = useState({ model_id: '', label: '', input: '', output: '' });

  useEffect(() => {
    if (!data) return;
    setOverrides(data.routing || {});
    setPrices((data.models || []).map((m) => ({
      ...m,
      input: m.input != null ? String(m.input) : '',
      output: m.output != null ? String(m.output) : '',
    })));
  }, [data]);

  if (loading) return <Spinner label="Loading model settings…" />;
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const categories = data.categories || [];
  const models = data.models || [];
  const envDefaults = data.env_defaults || {};

  const act = async (fn, label) => {
    setSaving(true);
    setSaveLabel(label);
    try { const r = await fn(); toast(r.message || 'Saved', 'success'); run(); }
    catch (e) { toast(e.message, 'error'); }
    finally { setSaving(false); setSaveLabel(''); }
  };

  const saveRouting = () => act(() =>
    api('/api/v1/superadmin/ai-models/routing', { method: 'POST', body: { overrides } }),
    'Saving routing…');

  const savePricing = () => {
    if (newModel.model_id && !newModel.label) { toast('Label is required for a new model', 'error'); return; }
    const rows = (prices || []).map(({ model_id, label, input, output }) => ({
      model_id, label,
      input: input === '' ? '' : input,
      output: output === '' ? '' : output,
    }));
    const body = { rows };
    if (newModel.model_id.trim()) {
      body.new_model = {
        model_id: newModel.model_id.trim(),
        label: newModel.label.trim(),
        input: newModel.input === '' ? '' : newModel.input,
        output: newModel.output === '' ? '' : newModel.output,
      };
    }
    act(() => api('/api/v1/superadmin/ai-models/pricing', { method: 'POST', body }), 'Saving pricing…');
  };

  const deleteModel = (mid) => act(() =>
    api('/api/v1/superadmin/ai-models/pricing/delete', { method: 'POST', body: { model_id: mid } }),
    'Deleting…');

  const addNewModel = () => {
    if (!newModel.model_id.trim()) { toast('Model ID required', 'error'); return; }
    setPrices((prev) => [...(prev || []), {
      model_id: newModel.model_id.trim(),
      label: newModel.label.trim() || newModel.model_id.trim(),
      input: newModel.input,
      output: newModel.output,
      source: 'custom',
    }]);
    setNewModel({ model_id: '', label: '', input: '', output: '' });
  };

  const setPrice = (mid, key, value) => setPrices((prev) =>
    prev.map((p) => (p.model_id === mid ? { ...p, [key]: value } : p)));

  const modelOptions = models.map((m) => ({ value: m.model_id, label: m.label }));

  return (
    <div>
      <PageHeader title="AI Models"
        subtitle="Which Gemini model extracts each document type, and what it costs."
        actions={saving ? <Badge tone="blue">{saveLabel}</Badge> : <Badge tone="gray">Applies to the next document</Badge>} />

      {/* Routing */}
      <div className="card" style={{ marginTop: 12, padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
          <h4 className="section-title" style={{ margin: 0 }}>Model routing by category</h4>
          <button className="btn btn-primary" disabled={saving} onClick={saveRouting}>
            {saving && saveLabel === 'Saving routing…' ? 'Saving…' : 'Save routing'}
          </button>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Category</th><th>Model</th><th>Default (.env)</th></tr></thead>
            <tbody>
              {categories.map((c) => {
                const cur = overrides?.[c.key] || '';
                return (
                  <tr key={c.key}>
                    <td>
                      <strong>{c.label}</strong>
                      <div className="muted" style={{ fontSize: 12 }}>{c.desc}</div>
                    </td>
                    <td>
                      <Select className="input" value={cur} placeholder={false}
                        onChange={(e) => setOverrides((prev) => ({ ...(prev || {}), [c.key]: e.target.value }))}
                        options={[{ value: '', label: '— follow .env default —' }, ...modelOptions]} />
                    </td>
                    <td><code>{envDefaults[c.key] || '—'}</code>
                      {cur ? <div className="muted" style={{ fontSize: 11 }}>override active</div> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Pricing */}
      <div className="card" style={{ marginTop: 14, padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
          <h4 className="section-title" style={{ margin: 0 }}>Pricing (USD per 1M tokens)</h4>
          <button className="btn btn-primary" disabled={saving} onClick={savePricing}>
            {saving && saveLabel === 'Saving pricing…' ? 'Saving…' : 'Save pricing'}
          </button>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr>
              <th>Model</th><th>Source</th>
              <th className="num">Input $</th><th className="num">Output $</th><th />
            </tr></thead>
            <tbody>
              {(prices || []).map((m) => (
                <tr key={m.model_id}>
                  <td>
                    <strong>{m.label || m.model_id}</strong>
                    <div className="muted" style={{ fontSize: 11 }}><code>{m.model_id}</code></div>
                  </td>
                  <td><Badge tone={m.source === 'custom' ? 'blue' : 'gray'}>{m.source}</Badge></td>
                  <td><TextInput className="pricing-in" value={m.input}
                    onChange={(e) => setPrice(m.model_id, 'input', e.target.value)} placeholder="default" /></td>
                  <td><TextInput className="pricing-in" value={m.output}
                    onChange={(e) => setPrice(m.model_id, 'output', e.target.value)} placeholder="default" /></td>
                  <td>{m.source === 'custom' ? (
                    <button className="btn btn-sm btn-danger" disabled={saving} onClick={() => deleteModel(m.model_id)}>Delete</button>
                  ) : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Blank prices fall back to the shipped catalog or a conservative estimate. Saved prices win.
        </p>

        <div className="card" style={{ marginTop: 12, padding: 14, background: 'var(--bg2, #F5F5F7)' }}>
          <strong>Add a new model</strong>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10, marginTop: 8 }}>
            <Field label="Model ID"><TextInput value={newModel.model_id}
              onChange={(e) => setNewModel((m) => ({ ...m, model_id: e.target.value }))} placeholder="gemini-x.y-model" /></Field>
            <Field label="Label"><TextInput value={newModel.label}
              onChange={(e) => setNewModel((m) => ({ ...m, label: e.target.value }))} placeholder="Gemini Display Name" /></Field>
            <Field label="Input $ / 1M"><TextInput value={newModel.input}
              onChange={(e) => setNewModel((m) => ({ ...m, input: e.target.value }))} placeholder="e.g. 1.50" /></Field>
            <Field label="Output $ / 1M"><TextInput value={newModel.output}
              onChange={(e) => setNewModel((m) => ({ ...m, output: e.target.value }))} placeholder="e.g. 9.00" /></Field>
            <Field label=" "><button className="btn" onClick={addNewModel}>+ Add to table</button></Field>
          </div>
        </div>
      </div>
    </div>
  );
}