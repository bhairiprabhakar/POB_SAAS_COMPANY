import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Users from './Users';
import Hierarchy from './Hierarchy';
import Teams from './Teams';
import Roles from './Roles';
import { Icon } from '../../ui';

const TABS = [
  ['users', 'users', 'Users'],
  ['hierarchy', 'building', 'Hierarchy'],
  ['teams', 'users', 'Teams'],
  ['roles', 'lock', 'Roles & Permissions'],
];

export default function UserManagement({ base = '/api/v1' }) {
  const [params] = useSearchParams();
  const [tab, setTab] = useState(() => {
    const t = params.get('tab');
    return TABS.some(([k]) => k === t) ? t : 'users';
  });
  return (
    <div>
      <div className="tabs">
        {TABS.map(([k, icon, label]) => (
          <button key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>
            <Icon name={icon} size={14} /> {label}
          </button>
        ))}
      </div>
      {tab === 'users' && <Users base={base} />}
      {tab === 'hierarchy' && <Hierarchy base={base} />}
      {tab === 'teams' && <Teams />}
      {tab === 'roles' && <Roles base={base} />}
    </div>
  );
}