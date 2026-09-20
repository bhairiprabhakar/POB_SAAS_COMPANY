// API client for the FieldNet pharma campaign platform.
// Stores one active session (superadmin OR tenant) in localStorage, attaches
// the JWT bearer header, and transparently rotates refresh tokens on 401.

const KEY = 'pob_saas_session';
const HINT_KEY = 'pob_saas_last_login';

export class ApiError extends Error {
  constructor(status, detail, body) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

export function getSession() {
  try {
    return JSON.parse(localStorage.getItem(KEY) || 'null');
  } catch {
    return null;
  }
}

export function setSession(s) {
  localStorage.setItem(KEY, JSON.stringify(s));
  try {
    localStorage.setItem(HINT_KEY, JSON.stringify({
      kind: s.kind,
      code: s.kind === 'tenant' ? (s.division?.code || null) : null,
    }));
  } catch { /* ignore */ }
  window.dispatchEvent(new Event('pob:session'));
}

// Where should an unsigned-in visitor land? Tenant sessions return to their
// division login link (/login/<code>) so the code is never asked again after
// logout or an expired session. Falls back to plain /login otherwise.
export function tenantLoginPath(s = getSession()) {
  if (s?.kind === 'tenant' && s?.division?.code) {
    return `/login/${encodeURIComponent(s.division.code)}`;
  }
  try {
    const hint = JSON.parse(localStorage.getItem(HINT_KEY) || 'null');
    if (hint?.kind === 'tenant' && hint.code) return `/login/${encodeURIComponent(hint.code)}`;
  } catch { /* ignore */ }
  return '/login';
}

export function clearSession() {
  localStorage.removeItem(KEY);
  window.dispatchEvent(new Event('pob:logout'));
}

export function isSuperAdmin() {
  const s = getSession();
  return !!(s && s.kind === 'sa');
}

// Platform console role of the signed-in super admin (owner | full |
// campaign_admin | finance_admin | verification_admin). `full` is the legacy
// "can do everything" account; specialised roles only see their own function.
export function saRole() {
  const s = getSession();
  if (s?.kind !== 'sa') return 'none';
  return s?.user?.role || 'full';
}

async function tryRefresh() {
  const s = getSession();
  if (!s || !s.refresh) return null;
  const ep = s.kind === 'sa'
    ? '/api/v1/auth/superadmin/refresh'
    : '/api/v1/auth/refresh';
  const body = s.kind === 'sa'
    ? { refresh_token: s.refresh }
    : { refresh_token: s.refresh, division_slug: s.division?.code };
  try {
    const r = await fetch(ep, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) return null;
    const d = await r.json();
    setSession({ ...s, access: d.access_token, refresh: d.refresh_token });
    return d.access_token;
  } catch {
    return null;
  }
}

export async function api(path, opts = {}) {
  const s = getSession();
  const headers = { ...(opts.headers || {}) };
  const isForm = opts.body instanceof FormData;
  const isBlob = opts.body instanceof Blob;
  const jsonBody = !isForm && !isBlob && opts.body != null;
  if (jsonBody) headers['Content-Type'] = 'application/json';
  if (s?.access) headers['Authorization'] = `Bearer ${s.access}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeout ?? 45000);
  const signal = opts.signal || controller.signal;

  const send = async (access) => {
    const h = { ...headers };
    if (access) h['Authorization'] = `Bearer ${access}`;
    return fetch(path, {
      ...opts,
      signal,
      headers: h,
      body: opts.body == null ? undefined
        : isForm || isBlob ? opts.body
        : typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body),
    });
  };

  let r;
  try {
    r = await send(s?.access);
    if (r.status === 401 && s?.refresh) {
      const access = await tryRefresh();
      if (access) r = await send(access);
    }
  } catch (err) {
    clearTimeout(timer);
    if (!(err instanceof ApiError) && err?.name === 'AbortError') {
      throw new ApiError(0, 'Request timed out after 45s — the server may be reloading; please try again');
    }
    throw err;
  }
  clearTimeout(timer);

  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    const d = await r.json();
    if (!r.ok) {
      if (r.status === 401) clearSession();
      const msg = typeof d.detail === 'string' ? d.detail
        : d.detail && d.detail.length ? d.detail.map((e) => e.msg).join('; ')
        : d.message || `Request failed (${r.status})`;
      throw new ApiError(r.status, msg, d);
    }
    return d;
  }
  if (!r.ok) {
    if (r.status === 401) clearSession();
    throw new ApiError(r.status, `Request failed (${r.status})`);
  }
  return r.blob();
}

export function uploadFile(path, file, extra = {}) {
  const fd = new FormData();
  if (file) fd.append('file', file);
  Object.entries(extra).forEach(([k, v]) => {
    if (v !== undefined && v !== null) fd.append(k, v);
  });
  return api(path, { method: 'POST', body: fd });
}

export async function downloadFile(path, filename) {
  const blob = await api(path);
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}

export function fmtMoney(v) {
  const n = Number(v || 0);
  return new Intl.NumberFormat('en-IN', {
    style: 'currency', currency: 'INR', maximumFractionDigits: 0,
  }).format(n);
}

export function fmtDate(v) {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}

export function fmtDateTime(v) {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toLocaleString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}
