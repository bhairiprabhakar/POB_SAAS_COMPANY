import { useState } from 'react';
import Users from './Users';
import Hierarchy from './Hierarchy';
import Roles from './Roles';

const TABS = [
  ['users', '👥', 'Users'],
  ['hierarchy', '☰', 'Hierarchy'],
  ['roles', '⚖', 'Roles & Permissions'],
];

export default function UserManagement({ base = '/api/v1' }) {
  const [tab, setTab] = useState('users');
  return (
    <div>
      <div className="tabs">
        {TABS.map(([k, icon, label]) => (
          <button key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            {icon} {label}
          </button>
        ))}
      </div>
      {tab === 'users' && <Users base={base} />}
      {tab === 'hierarchy' && <Hierarchy base={base} />}
      {tab === 'roles' && <Roles base={base} />}
    </div>
  );
}