import { useState } from 'react';
import { BrandsTab } from './ManagementWorkspace';
import Products from './Products';

const TABS = [
  ['brands', '◉', 'Brands'],
  ['products', '📦', 'Products'],
];

export default function Catalog() {
  const [tab, setTab] = useState('brands');
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