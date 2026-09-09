/* Bundle ssr_smoke.mjs + the whole web/src tree for Node so it can import the
 * .jsx pages directly, then run it. */
import { build } from 'esbuild';

await build({
  entryPoints: ['ssr_smoke.mjs'],
  bundle: true,
  platform: 'node',
  format: 'esm',
  outfile: 'ssr_smoke.bundle.mjs',
  jsx: 'automatic',
  sourcemap: false,
  external: ['react', 'react-dom', 'react-dom/server', 'react-router-dom'],
  logLevel: 'error',
});
console.log('bundled');
