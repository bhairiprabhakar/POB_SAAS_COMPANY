/* SSR smoke test: bundle every admin page with esbuild into one node-runnable
 * file, then server-render each under a MemoryRouter. Catches top-level module
 * errors and initial-render crashes (the state a user sees first). */
import './src/mock_env.js';
import { renderToString } from 'react-dom/server';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import Home from './src/pages/app/Home.jsx';
import Analytics from './src/pages/app/Analytics.jsx';
import Pobs from './src/pages/app/Pobs.jsx';
import Verification from './src/pages/app/Verification.jsx';
import SubmitPob from './src/pages/app/SubmitPob.jsx';
import InvoiceProof from './src/pages/app/InvoiceProof.jsx';
import RegisterChemist from './src/pages/app/RegisterChemist.jsx';
import Gratification from './src/pages/app/Gratification.jsx';
import Visits from './src/pages/app/Visits.jsx';
import Reports from './src/pages/app/Reports.jsx';
import Notifications from './src/pages/app/Notifications.jsx';
import Campaigns from './src/pages/app/Campaigns.jsx';
import CampaignDetail from './src/pages/app/CampaignDetail.jsx';
import Masters from './src/pages/app/Masters.jsx';
import Chemists from './src/pages/app/Chemists.jsx';
import Profile from './src/pages/app/Profile.jsx';
import Audit from './src/pages/app/Audit.jsx';
import Security from './src/pages/app/Security.jsx';
import Jobs from './src/pages/app/Jobs.jsx';
import Users from './src/pages/app/Users.jsx';
import Roles from './src/pages/app/Roles.jsx';
import Hierarchy from './src/pages/app/Hierarchy.jsx';
import Login from './src/pages/Login.jsx';
import SuperAdminLogin from './src/pages/SuperAdminLogin.jsx';

localStorage.setItem('pob_saas_session', JSON.stringify({
  kind: 'tenant', company: { code: 'ACME', name: 'Acme' },
  user: { username: 'admin', role: 'company_admin' },
  permissions: ['dashboard.view', 'pob.submit', 'pob.verify', 'campaign.view', 'brand.view',
    'product.view', 'chemist.view', 'gratification.view', 'job.view'],
}));

const pages = [
  ['Home', Home], ['Analytics', Analytics], ['Pobs', Pobs], ['Verification', Verification],
  ['SubmitPob', SubmitPob], ['InvoiceProof', InvoiceProof], ['RegisterChemist', RegisterChemist],
  ['Gratification', Gratification], ['Visits', Visits], ['Reports', Reports],
  ['Notifications', Notifications], ['Campaigns', Campaigns], ['CampaignDetail', CampaignDetail],
  ['Masters', Masters], ['Chemists', Chemists], ['Profile', Profile], ['Audit', Audit],
  ['Security', Security], ['Jobs', Jobs], ['Users', Users], ['Roles', Roles],
  ['Hierarchy', Hierarchy], ['Login', Login], ['SuperAdminLogin', SuperAdminLogin],
];

let failed = 0;
for (const [name, Comp] of pages) {
  try {
    const html = renderToString(
      React.createElement(MemoryRouter, { initialEntries: ['/app'] },
        React.createElement(Comp)),
    );
    console.log(`PASS  ${name}  (${html.length} chars)`);
  } catch (err) {
    failed++;
    console.log(`FAIL  ${name}  -> ${err.message}`);
    console.log((err.stack || '').split('\n').slice(0, 6).join('\n'));
  }
}
console.log(failed === 0 ? '\nALL PAGES RENDER' : `\n${failed} PAGE(S) FAILED`);
process.exit(failed === 0 ? 0 : 1);
