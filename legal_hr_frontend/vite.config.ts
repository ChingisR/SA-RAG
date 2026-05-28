import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Forward all /api/* requests to the FastAPI backend on the remote GPU server.
      // The backend strips /api prefix via nginx rewrite in production,
      // but here we target port 8000 directly (no prefix strip needed).
      '/api': {
        target: 'http://10.242.102.2:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
        timeout: 120000,          // 2 min — LLM streaming can be slow
      },
    },
  },
})

