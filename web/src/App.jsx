import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { api, clearSession, getSession, tenantLoginPath } from './api';
import { AppShell, Toaster, toast } from './ui';
import Login from './pages/Login';
import SuperAdminLogin from './pages/SuperAdminLogin';
import ForgotPassword from './pages/ForgotPassword';
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
import Audit from './pages/app/Audit';
import Security from './pages/app/Security';
import Jobs from './pages/app/Jobs';
import SuperAnalytics from './pages/superadmin/Analytics';
import PlatformSettings from './pages/superadmin/PlatformSettings';
import ErrorBoundary from './ErrorBoundary';
import ManageCampaigns from './pages/app/ManageCampaigns';
import ManageBrands from './pages/app/ManageBrands';
import UserManagement from './pages/app/UserManagement';

function ProtectedTenant({ children }) {
  const s = getSession();
  const loc = useLocation();
  if (!s || s.kind !== 'tenant') return <Navigate to={tenantLoginPath()} replace />;
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
  return <Navigate to={tenantLoginPath()} replace />;
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
          <Route path="/superadmin-login" element={<SuperAdminLogin />} />
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
        <Route path="/superadmin/audit" element={
          <ProtectedSuper><AppShell kind="sa"><AuditLog /></AppShell></ProtectedSuper>
        } />
        <Route path="/superadmin/settings" element={
          <ProtectedSuper><AppShell kind="sa"><PlatformSettings /></AppShell></ProtectedSuper>
        } />

        <Route path="/app" element={
          <ProtectedTenant><AppShell kind="tenant"><Home /></AppShell></ProtectedTenant>
        } />
        <Route path="/app/admin" element={
          <ProtectedTenant><AppShell kind="tenant"><AdminDashboard /></AppShell></ProtectedTenant>
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
