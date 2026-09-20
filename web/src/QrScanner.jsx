import { useEffect, useId, useRef, useState } from 'react';
import { Html5Qrcode } from 'html5-qrcode';

/**
 * QrScanner
 *
 * Camera-based QR scanner built on html5-qrcode. Wraps the live-camera
 * lifecycle (start/stop/cleanup) so callers only handle the decoded payload.
 * Used for the UPI QR flow (chemist page + gratification flow); manual paste
 * remains available as the fallback in those forms.
 *
 * Props:
 *   onScan(payload)   - fires once with the decoded raw QR text
 *   onError(message)  - fires on camera / scan failures (optional)
 *   onClose()         - fires when the user / caller closes the scanner
 *   fps               - frames-per-second hint (default 10)
 */
export default function QrScanner({ onScan, onError, onClose, fps = 10 }) {
  const uid = useId().replace(/:/g, '');
  const containerRef = useRef(null);
  const scannerRef = useRef(null);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(true);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;
    const scanner = new Html5Qrcode(el.id);
    scannerRef.current = scanner;

    const stop = () =>
      Promise.resolve()
        .then(() => (scanner.isScanning ? scanner.stop() : Promise.resolve()))
        .then(() => scanner.clear())
        .catch(() => {});

    scanner
      .start(
        { facingMode: 'environment' },
        { fps, qrbox: { width: 240, height: 240 } },
        (decodedText) => {
          // Success: stop the camera, hand the raw payload to the caller.
          stop().then(() => {
            if (onScan) onScan(decodedText);
          });
        },
        () => {
          /* per-frame miss logs would spam; ignore */
        },
      )
      .then(() => setStarting(false))
      .catch((err) => {
        const msg = String(err?.message || err || 'Camera unavailable');
        setError(msg);
        setStarting(false);
        if (onError) onError(msg);
      });

    return () => {
      stop();
      scannerRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="qr-scanner">
      <div id={`qr-${uid}`} ref={containerRef} className="qr-scanner-camera" />
      {starting && !error && <p className="muted qr-scanner-status">Starting camera…</p>}
      {error && (
        <p className="qr-scanner-error">
          Camera error: {error}. Use the manual paste option below instead.
        </p>
      )}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 8 }}>
        {onClose && (
          <button type="button" className="btn btn-sm" onClick={onClose}>
            Close scanner
          </button>
        )}
      </div>
    </div>
  );
}