// Shared read-only viewers for statement extraction results. Used by the
// tenant Statements page (extraction detail) and the Statement Verify
// workspace (task detail preview). Kept in one place so both views stay
// field-for-field identical.

import { EmptyState } from './ui';

export function money(n) {
  const v = Number(n) || 0;
  return v.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

export function ExtractionMeta({ extraction }) {
  if (!extraction) return null;
  const period = [extraction.statement_from_date, extraction.statement_to_date]
    .filter(Boolean).join(' → ') || '—';
  const rows = [
    ['Stockist name', extraction.stockist_name || '—'],
    ['Statement period', period],
    ['Document type', extraction.doc_type || '—'],
    ['Invoice net', money(extraction.invoice_net)],
    ['Total quantity', extraction.total_quantity != null ? String(extraction.total_quantity) : '—'],
    ['Bill number', extraction.bill_number || '—'],
    ['Bill date', extraction.bill_date || '—'],
    ['Stockist GST', extraction.stockist_gst || '—'],
    ['Stockist address', extraction.stockist_address || '—'],
  ];
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <h4 className="section-title">Extraction summary</h4>
      <table className="detail-table">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}><th>{k}</th><td>{v}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PartiesView({ parties = [], title = 'Parties & line items' }) {
  if (!parties || !parties.length) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <h4 className="section-title">{title}</h4>
      {parties.map(({ party, items }) => (
        <div className="card" key={party.id} style={{ marginTop: 12 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
            <strong>{party.name || 'Unnamed party'}</strong>
            <span className="muted">{party.area || '—'}{party.type ? ` · ${party.type}` : ''}</span>
          </div>
          {(party.dl_number || party.gst_number) && (
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {[party.dl_number && `DL: ${party.dl_number}`, party.gst_number && `GST: ${party.gst_number}`]
                .filter(Boolean).join(' · ')}
            </div>
          )}
          {party.total_amount != null && (
            <div style={{ fontSize: 13, marginTop: 4 }}>
              Total: <strong>{money(party.total_amount)}</strong>
              {party.total_quantity != null ? ` · Qty ${party.total_quantity}` : ''}
            </div>
          )}
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table className="data-table">
              <thead><tr>
                <th>Brand</th><th>Mfg</th><th>Pack</th>
                <th className="num">Qty</th><th className="num">Rate</th>
                <th className="num">Disc %</th><th className="num">Amount</th>
              </tr></thead>
              <tbody>
                {(items || []).map((i) => (
                  <tr key={i.id}>
                    <td>{i.brand || '—'}</td>
                    <td>{i.mfg || '—'}</td>
                    <td>{i.pack || '—'}</td>
                    <td className="num">{i.quantity ?? '—'}</td>
                    <td className="num">{money(i.unit_rate)}</td>
                    <td className="num">{i.discount_percent ?? '—'}</td>
                    <td className="num"><strong>{money(i.final_amount)}</strong></td>
                  </tr>
                ))}
                {(!items || !items.length) && (
                  <tr><td colSpan={7}><EmptyState text="No line items" /></td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}