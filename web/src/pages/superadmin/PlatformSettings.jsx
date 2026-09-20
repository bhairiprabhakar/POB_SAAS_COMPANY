import { useRef, useState } from 'react';
import { api } from '../../api';
import { ErrorBox, PageHeader, TextInput, toast, useAsync } from '../../ui';

export default function PlatformSettings() {
  const { data, error, run } = useAsync(() => api('/api/v1/superadmin/platform-settings'));
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [busyUpload, setBusyUpload] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [pending, setPending] = useState(null);
  const inputRef = useRef(null);

  if (error) return <ErrorBox error={error} onRetry={run} />;
  const s = data;
  if (!s) return <PageHeader title="Platform settings" subtitle="Loading…" />;

  const save = async () => {
    const trimmed = name.trim();
    if (!trimmed) { toast('Enter a platform name', 'error'); return; }
    setBusy(true);
    try {
      await api('/api/v1/superadmin/platform-settings', { method: 'PUT', body: { platform_name: trimmed } });
      toast('Platform name saved', 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
    finally { setBusy(false); }
  };

  const upload = async (file) => {
    if (!file || busyUpload) return;
    if (!/^image\//.test(file.type)) { toast('Please choose an image file (PNG, JPG or WebP)', 'error'); return; }
    setBusyUpload(true);
    setPending(URL.createObjectURL(file));
    try {
      const fd = new FormData();
      fd.append('file', file);
      await api('/api/v1/superadmin/platform-settings/logo', { method: 'POST', body: fd });
      toast('Logo uploaded', 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
    finally { setBusyUpload(false); setPending(null); }
  };

  const remove = async () => {
    if (busyUpload) return;
    if (!window.confirm('Remove the platform logo? The platform console will fall back to its default brand mark.')) return;
    setBusyUpload(true);
    try {
      await api('/api/v1/superadmin/platform-settings/logo', { method: 'DELETE' });
      toast('Logo removed', 'success');
      run();
    } catch (err) { toast(err.message, 'error'); }
    finally { setBusyUpload(false); }
  };

  const shown = pending || s.logo_url;
  const displayName = (name.trim() || s.platform_name || 'FieldNet');

  return (
    <div>
      <PageHeader title="Platform settings" subtitle="The identity shown across the platform console and the super admin sign-in page." />

      <div className="card">
        <h4 className="section-title">Platform name</h4>
        <p className="muted">Displayed on the super admin sign-in page and in the console sidebar.</p>
        <div className="grid-2" style={{ maxWidth: 520, marginTop: 6 }}>
          <TextInput label="Platform name" value={name}
            placeholder={s.platform_name || 'FieldNet'}
            onChange={(e) => setName(e.target.value)} />
          <div style={{ display: 'flex', alignItems: 'flex-end' }}>
            <button className="btn btn-primary" onClick={save} disabled={busy || !name.trim()}>
              {busy ? 'Saving…' : 'Save name'}
            </button>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <h4 className="section-title">Platform branding</h4>
        <p className="muted">Your logo and platform name appear on the super admin sign-in page and at the top of the console sidebar.</p>
        <div className="branding-grid">
          <div className="branding-preview">
            <div className="branding-screen">
              <div className="branding-login-card">
                {shown
                  ? <img src={shown} alt="Platform logo" className="branding-logo-img" />
                  : <span className="branding-mark">{displayName.charAt(0)}</span>}
                <div className="branding-name">{displayName}</div>
                <div className="branding-fields">
                  <span className="branding-field" />
                  <span className="branding-field" />
                </div>
                <div className="branding-signin">Sign in</div>
              </div>
            </div>
            <div className="branding-caption">Super admin sign-in preview — /superadmin-login</div>
          </div>

          <div className="branding-controls">
            <div className="branding-label-row">
              <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <strong>Platform logo</strong>
                <span className="muted">PNG / JPG / WebP, max 2 MB</span>
              </span>
              {s.has_logo && (
                <button className="btn btn-danger btn-sm" onClick={remove} disabled={busyUpload}>Remove</button>
              )}
            </div>

            <div className={`dropzone logo-dropzone branding-dropzone${shown ? ' has-file' : ''}${dragging ? ' dragging' : ''}`}
              role="button" tabIndex={0} aria-label={shown ? 'Replace platform logo' : 'Upload platform logo'}
              onClick={() => inputRef.current?.click()}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') inputRef.current?.click(); }}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => { e.preventDefault(); setDragging(false); upload(e.dataTransfer.files?.[0]); }}>
              <input ref={inputRef} type="file" accept="image/png,image/jpeg,image/webp" hidden
                onChange={(e) => upload(e.target.files?.[0])} />
              {shown ? (
                <>
                  <img src={shown} alt="Platform logo preview" className="logo-dropzone-preview" />
                  <strong>{busyUpload ? 'Uploading…' : 'Replace logo'}</strong>
                </>
              ) : (
                <>
                  <span className="dropzone-icon">🖼</span>
                  <strong>{busyUpload ? 'Uploading…' : 'Upload a logo'}</strong>
                  <span className="muted" style={{ fontSize: 12 }}>Click to browse or drag &amp; drop</span>
                </>
              )}
            </div>

            <p className="muted branding-hint">
              Used to customise the platform for this installation. A transparent PNG looks best on both the sign-in card and the dark sidebar.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}