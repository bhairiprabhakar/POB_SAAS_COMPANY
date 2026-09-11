import { useMemo, useState } from 'react';
import { api } from '../../api';
import { ErrorBox, PageHeader, SearchBox, StatCard, StatSkeleton, useAsync } from '../../ui';

export default function Products() {
  const [q, setQ] = useState('');
  const { data, loading, error, run } = useAsync(() => api('/api/v1/products'));
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => {
    let out = data?.items || [];
    if (q) {
      const n = q.toLowerCase();
      out = out.filter((r) => (r.name || '').toLowerCase().includes(n)
        || (r.sku || '').toLowerCase().includes(n)
        || (r.brand_name || '').toLowerCase().includes(n)
        || (r.strength || '').toLowerCase().includes(n));
    }
    return out;
  }, [data, q]);

  if (loading) {
    return <div><PageHeader title="Products" subtitle="Campaign &amp; brand product catalogue" /><StatSkeleton n={3} /></div>;
  }
  if (error) return <ErrorBox error={error} onRetry={run} />;

  const total = (data?.items || []).length;
  const active = (data?.items || []).filter((r) => r.status === 'active').length;
  const brands = new Set((data?.items || []).map((r) => r.brand_name).filter(Boolean)).size;

  return (
    <div>
      <PageHeader title="Products"
        subtitle="Every product attached to your campaigns and brands"
        actions={<SearchBox value={q} onChange={setQ} placeholder="Search name / SKU / brand…" />} />
      <div className="stats-grid compact">
        <StatCard label="Products" value={total} tone="blue" />
        <StatCard label="Active" value={active} tone="green" />
        <StatCard label="Brands" value={brands} tone="amber" />
      </div>
      {rows.length ? (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th><th>SKU</th><th>Brand</th><th>Campaign</th><th>PTR</th><th>MRP</th><th>Min qty</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><strong>{r.name}</strong>{r.strength ? <span className="muted"> · {r.strength}</span> : null}</td>
                <td className="muted">{r.sku || '—'}</td>
                <td>{r.brand_name || '—'}</td>
                <td>{r.campaign_name || '—'}</td>
                <td>{r.ptr ? `₹${r.ptr}` : '—'}</td>
                <td>{r.mrp ? `₹${r.mrp}` : '—'}</td>
                <td>{r.min_quantity ?? '—'}</td>
                <td><span className={`badge ${r.status === 'active' ? 'badge-green' : 'badge-gray'}`}>{r.status}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="muted">No products yet — add products when creating a campaign.</p>}
    </div>
  );
}