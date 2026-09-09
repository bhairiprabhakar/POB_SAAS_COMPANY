import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The SPA is served by the FastAPI backend (saas/main.py) from web/dist,
// so the base path is the app root and the dev proxy forwards /api to :8000.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/healthz': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
});
