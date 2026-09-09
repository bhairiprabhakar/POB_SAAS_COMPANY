// Test-only browser-global shims. MUST be imported before any app module so
// module-scope browser usage is safe. No imports of its own.
class LocalStorageMock {
  #m = new Map();
  getItem(k) { return this.#m.has(k) ? this.#m.get(k) : null; }
  setItem(k, v) { this.#m.set(k, String(v)); }
  removeItem(k) { this.#m.delete(k); }
  clear() { this.#m.clear(); }
}

const noop = () => {};
const win = {
  addEventListener: noop, removeEventListener: noop,
  dispatchEvent: () => true, localStorage: new LocalStorageMock(),
  innerWidth: 1280, location: { href: 'http://localhost/' },
  fetch: async () => ({ ok: true, headers: { get: () => 'application/json' }, json: async () => ({}) }),
};
globalThis.window = win;
globalThis.localStorage = win.localStorage;
try { Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node' }, configurable: true }); } catch { /* noop */ }
globalThis.document = {
  createElement: () => ({ style: {}, classList: { add: noop }, click: noop, remove: noop, appendChild: noop }),
  body: {}, addEventListener: noop,
};
globalThis.Event = class Event { constructor(type) { this.type = type; } };
