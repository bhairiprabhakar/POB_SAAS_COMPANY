import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { BrandsTab } from './ManagementWorkspace';
import Products from './Products';

const TABS = [
  ['brands', '◉', 'Brands'],
  ['products', '📦', 'Products'],
];

export default function Catalog() {
  const [params] = useSearchParams();
  const [tab, setTab] = useState(() => {
    const t = params.get('tab');
    return TABS.some(([k]) => k === t) ? t : 'brands';
  });
  return (
    <div>
      <div className="tabs">
        {TABS.map(([k, icon, label]) => (
          <button key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            {icon} {label}
          </button>
        ))}
      </div>
      {tab === 'brands' && <BrandsTab base="/api/v1" />}
      {tab === 'products' && <Products />}
    </div>
  );
}