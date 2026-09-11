import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { api, clearSession, getSession, tenantLoginPath } from './api';
import { AppShell, Toaster, toast } from './ui';
import Login from './pages/Login';
import SuperAdminLogin from './pages/SuperAdminLogin';
import Register from './pages/Register';
import ForgotPassword from './pages/ForgotPassword';
import ChangePassword from './pages/ChangePassword';
import SuperDashboard from './pages/superadmin/Dashboard';
import Divisions from './pages/superadmin/Divisions';
import DivisionDetail from './pages/superadmin/DivisionDetail';
import AuditLog from './pages/superadmin/AuditLog';
import Home from './pages/app/Home';
import AdminDashboard from './pages/app/AdminDashboard';
import SubmitPob from './pages/app/SubmitPob';
import InvoiceProof from './pages/app/InvoiceProof';
import Pobs from './pages/app/Pobs';
import Verification from './pages/app/Verification';
import VerificationAgent from './pages/app/VerificationAgent';
import Gratification from './pages/app/Gratification';
import Visits from './pages/app/Visits';
import Reports from './pages/app/Reports';
import Analytics from './pages/app/Analytics';
import Notifications from './pages/app/Notifications';
import Campaigns from './pages/app/Campaigns';
import CampaignDetail from './pages/app/CampaignDetail';
import Masters from './pages/app/Masters';
import Chemists from './pages/app/Chemists';
import RegisterChemist from './pages/app/RegisterChemist';
import Profile from './pages/app/Profile';
import Onboarding from './pages/app/Onboarding';
import Audit from './pages/app/Audit';
import Security from './pages/app/Security';
import Jobs from './pages/app/Jobs';
import SuperAnalytics from './pages/superadmin/Analytics';
import Costing from './pages/superadmin/Costing';
import SuperFinance from './pages/superadmin/Finance';
import PlatformSettings from './pages/superadmin/PlatformSettings';
import CompanyProfile from './pages/superadmin/CompanyProfile';
import SuperCampaigns from './pages/superadmin/Campaigns';
import PobOperations from './pages/superadmin/PobOperations';
import SuperGratification from './pages/superadmin/Gratification';
import SuperUsers from './pages/superadmin/Users';
import PlatformAdmins from './pages/superadmin/PlatformAdmins';
import MyDivision from './pages/app/MyDivision';
import Teams from './pages/app/Teams';
import Regions from './pages/app/Regions';
import Products from './pages/app/Products';
import Gifts from './pages/app/Gifts';
import ErrorBoundary from './ErrorBoundary';
import ManageCampaigns from './pages/app/ManageCampaigns';
import ManageBrands from './pages/app/ManageBrands';
import Catalog from './pages/app/Catalog';
import UserManagement from './pages/app/UserManagement';

function ProtectedTenant({ children }) {
  const s = getSession();
  const loc = useLocation();
  if (!s || s.kind !== 'tenant') return <Navigate to={tenantLoginPath()} replace />;
  // First-login onboarding gates a freshly invited division admin behind
  // TOTP enrollment + profile completion before the dashboard is reachable.
  const onboarding = s.user?.onboarding;
  if (onboarding && !loc.pathname.startsWith('/app/onboarding')) {
    return <Navigate to={`/app/onboarding/${onboarding}`} replace />;
  }
  if (!onboarding && loc.pathname.startsWith('/app/onboarding')) {
    return <Navigate to="/app" replace />;
  }
  // Masters (divisions/brands/campaigns/products/gifts/gratification types)
  // are super-admin-managed; division admins don't get them. The Chemists page
  // is a first-class nav item (scoped per division), so it stays accessible.
  // Campaign tracking + the campaign detail page (banner/logo) are read-only
  // views, so those stay open to division admins too.
  const role = (s.user?.role || '').toLowerCase();
  if (role === 'division_admin' && loc.pathname.startsWith('/app/masters') &&
      !loc.pathname.startsWith('/app/masters/campaigns')) {
    return <Navigate to="/app" replace />;
  }
  // Organization (hierarchy / users / roles & permissions) is managed by the
  // super admin from the platform console, so tenant users can't open it.
  if (loc.pathname.startsWith('/app/org')) {
    return <Navigate to="/app" replace />;
  }
  return children;
}

function ProtectedSuper({ children }) {
  const s = getSession();
  if (!s || s.kind !== 'sa') return <Navigate to="/superadmin-login" replace />;
  const role = s.user?.role || 'full';
  const home = { campaign_admin: '/superadmin/campaigns', finance_admin: '/superadmin/gratification', verification_admin: '/superadmin/pob' }[role];
  if (home) {
    const path = window.location.pathname;
    const allowed = [home, '/superadmin'];
    if (role === 'campaign_admin') allowed.push('/superadmin/analytics');
    if (role === 'finance_admin') allowed.push('/superadmin/finance');
    if (!allowed.includes(path)) return <Navigate to={home} replace />;
  }
  return children;
}

function SessionWatcher() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const refresh = () => setTick((t) => t + 1);
    const logout = () => {
      setTick((t) => t + 1);
      toast('Your session has expired. Please sign in again.', 'info');
    };
    window.addEventListener('pob:session', refresh);
    window.addEventListener('pob:logout', logout);
    return () => {
      window.removeEventListener('pob:session', refresh);
      window.removeEventListener('pob:logout', logout);
    };
  }, []);
  // keep storage in sync across tabs
  useEffect(() => {
    const onStorage = (e) => { if (e.key === 'pob_saas_session') setTick((t) => t + 1); };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);
  return null;
}

function LoginOrApp() {
  const s = getSession();
  if (s?.kind === 'sa') return <Navigate to="/superadmin" replace />;
  if (s?.kind === 'tenant') return <Navigate to="/app" replace />;
  return <StartGate />;
}

// Unsigned visitors on "/" land on registration when the platform has no
// company yet (first run), otherwise on the company sign-in page.
function StartGate() {
  const [regOpen, setRegOpen] = useState(null);
  useEffect(() => {
    api('/api/v1/auth/register-status')
      .then((d) => setRegOpen(Boolean(d.registration_open)))
      .catch(() => setRegOpen(false));
  }, []);
  if (regOpen === null) return null;
  return regOpen ? <Navigate to="/register" replace /> : <Navigate to={tenantLoginPath()} replace />;
}

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <SessionWatcher />
        <Toaster />
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/login/:divisionSlug" element={<Login />} />
          <Route path="/forgot-password" element={<ForgotPassword />} />
          <Route path="/change-password" element={<ChangePassword />} />
          <Route path="/superadmin-login" element={<SuperAdminLogin />} />
          <Route path="/register" element={<Register />} />
          <Route path="/" element={<LoginOrApp />} />

        <Route path="/superadmin" element={
          <ProtectedSuper><AppShell kind="sa"><SuperDashboard /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/divisions" element={
          <ProtectedSuper><AppShell kind="sa"><Divisions /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/divisions/:did" element={
          <ProtectedSuper><AppShell kind="sa"><DivisionDetail /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/analytics" element={
          <ProtectedSuper><AppShell kind="sa"><SuperAnalytics /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/campaigns" element={
          <ProtectedSuper><AppShell kind="sa"><SuperCampaigns /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/pob" element={
          <ProtectedSuper><AppShell kind="sa"><PobOperations /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/gratification" element={
          <ProtectedSuper><AppShell kind="sa"><SuperGratification /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/users" element={
          <ProtectedSuper><AppShell kind="sa"><SuperUsers /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/admins" element={
          <ProtectedSuper><AppShell kind="sa"><PlatformAdmins /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/costing" element={
          <ProtectedSuper><AppShell kind="sa"><Costing /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/finance" element={
          <ProtectedSuper><AppShell kind="sa"><SuperFinance /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/audit" element={
          <ProtectedSuper><AppShell kind="sa"><AuditLog /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/company" element={
          <ProtectedSuper><AppShell kind="sa"><CompanyProfile /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/platform-settings" element={
          <ProtectedSuper><AppShell kind="sa"><PlatformSettings /></AppShell></ProtectedSuper>
        } />

        <Route path="/app/onboarding/:step" element={
          <ProtectedTenant><AppShell kind="tenant"><Onboarding /></AppShell></ProtectedTenant>
        } />
        <Route path="/app" element={
          <ProtectedTenant><AppShell kind="tenant"><Home /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/admin" element={
          <ProtectedTenant><AppShell kind="tenant"><AdminDashboard /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/my-division" element={
          <ProtectedTenant><AppShell kind="tenant"><MyDivision /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/teams" element={
          <ProtectedTenant><AppShell kind="tenant"><Teams /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/catalog" element={
          <ProtectedTenant><AppShell kind="tenant"><Catalog /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/regions" element={
          <ProtectedTenant><AppShell kind="tenant"><Regions /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/products" element={
          <ProtectedTenant><AppShell kind="tenant"><Products /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/gifts" element={
          <ProtectedTenant><AppShell kind="tenant"><Gifts /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/pob/submit" element={
          <ProtectedTenant><AppShell kind="tenant"><SubmitPob /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/pob/invoice" element={
          <ProtectedTenant><AppShell kind="tenant"><InvoiceProof /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/pob/mine" element={
          <ProtectedTenant><AppShell kind="tenant"><Pobs mine /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/pob" element={
          <ProtectedTenant><AppShell kind="tenant"><Pobs /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/verification" element={
          <ProtectedTenant><AppShell kind="tenant">{
            (getSession()?.user?.role || '').toLowerCase() === 'verifier' ||
            (getSession()?.user?.role || '').toLowerCase() === 'verification_agent'
              ? <VerificationAgent />
              : <Verification />
          }</AppShell></ProtectedTenant>
        } />
        <Route path="/app/gratification" element={
          <ProtectedTenant><AppShell kind="tenant"><Gratification /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/visits" element={
          <ProtectedTenant><AppShell kind="tenant"><Visits /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/reports" element={
          <ProtectedTenant><AppShell kind="tenant"><Reports /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/analytics" element={
          <ProtectedTenant><AppShell kind="tenant"><Analytics /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/notifications" element={
          <ProtectedTenant><AppShell kind="tenant"><Notifications /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/campaigns" element={
          <ProtectedTenant><AppShell kind="tenant"><ManageCampaigns /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/brands" element={
          <ProtectedTenant><AppShell kind="tenant"><ManageBrands /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/user-management" element={
          <ProtectedTenant><AppShell kind="tenant"><UserManagement /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/masters/campaigns" element={
          <ProtectedTenant><AppShell kind="tenant"><Campaigns /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/masters/campaigns/:id" element={
          <ProtectedTenant><AppShell kind="tenant"><CampaignDetail /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/masters/:kind" element={
          <ProtectedTenant><AppShell kind="tenant"><Masters /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/chemists" element={
          <ProtectedTenant><AppShell kind="tenant"><Chemists /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/chemists/register" element={
          <ProtectedTenant><AppShell kind="tenant"><RegisterChemist /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/audit" element={
          <ProtectedTenant><AppShell kind="tenant"><Audit /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/security" element={
          <ProtectedTenant><AppShell kind="tenant"><Security /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/jobs" element={
          <ProtectedTenant><AppShell kind="tenant"><Jobs /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/profile" element={
          <ProtectedTenant><AppShell kind="tenant"><Profile /></AppShell></ProtectedTenant>
        } />

        <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
