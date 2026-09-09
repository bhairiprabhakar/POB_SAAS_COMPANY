import { Link, useParams } from 'react-router-dom';
import { api } from '../../api';
import {
  ErrorBox, PageHeader, Spinner, StatusBadge, Table, useAsync, useFileUrl,
} from '../../ui';

function AssetImage({ rel, className, alt }) {
  const url = useFileUrl(rel);
  if (!rel || !url) return null;
  return <img src={url} className={className} alt={alt} />;
}

export default function CampaignDetail() {
  const { id } = useParams();
  const { data, loading, error } = useAsync(() => api(`/api/v1/campaigns/${id}`));

  if (loading) return <Spinner label="Loading campaign…" />;
  if (error) return <ErrorBox error={error} />;
  const c = data;
  const brandLabel = c.brand_names?.length
    ? c.brand_names.join(' + ')
    : (c.brand_name || 'No brand');

  return (
    <div>
      <PageHeader title={c.name} subtitle={`${brandLabel} · ${c.status} · scheme: ${c.scheme_type}`}
        actions={<Link className="btn" to="/app/masters/campaigns">← All campaigns</Link>} />
      {c.banner_path && (
        <div className="campaign-banner">
          <AssetImage rel={c.banner_path} className="campaign-banner-img" alt={`${c.name} banner`} />
        </div>
      )}
      <div className="campaign-detail-head">
        {c.logo_path && (
          <div className="campaign-logo">
            <AssetImage rel={c.logo_path} className="campaign-logo-img" alt={`${c.name} logo`} />
          </div>
        )}
        <div className="campaign-detail-meta">
          <div className="kv-grid">
            <span>Division <strong>{c.division_name || c.division || '—'}</strong></span>
            <span>Window <strong>{c.start_date || '—'} → {c.end_date || '—'}</strong></span>
            <span>Status <StatusBadge value={c.status} /></span>
            <span>Verification <strong>{c.invoice_verification_required ? 'Required' : 'Not required'}</strong></span>
          </div>
          {c.description && <p className="muted">{c.description}</p>}
        </div>
      </div>

      <h3 className="sub-head">Products under this campaign</h3>
      <Table cols={[
        { key: 'id', label: 'ID', render: (r) => <strong>#{r.id}</strong> },
        { key: 'name', label: 'Name' },
        { key: 'sku', label: 'SKU' },
        { key: 'ptr', label: 'PTR', render: (r) => `₹${r.ptr || 0}` },
        { key: 'pts', label: 'PTS', render: (r) => `₹${r.pts || 0}` },
        { key: 'min_quantity', label: 'Min qty' },
        { key: 'min_pob', label: 'Min POB' },
        { key: 'max_pob', label: 'Max POB' },
        { key: 'status', label: 'Status', render: (r) => <StatusBadge value={r.status} /> },
      ]} rows={c.products || []} keyOf={(r) => r.id}
        empty="No products yet" />
    </div>
  );
}
