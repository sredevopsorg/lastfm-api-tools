import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API serves this bundle in production; during development Vite proxies
// /api to the FastAPI process so the SPA can run on its own hot-reload server.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
