import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

// https://vitejs.dev/config/
export default defineConfig(() => {
  // Keep normal development pointed at :8000. Browser E2E supplies an
  // isolated backend target so it cannot attach to or stop a developer's
  // already-running FastAPI process on the same machine.
  const apiTarget = process.env.GISMIND_VITE_API_TARGET ?? 'http://localhost:8000';
  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          // SSE streaming: do not buffer
          // (no ws rewrite needed; /api/chat is HTTP streaming)
        },
      },
    },
    build: {
      target: 'es2020',
      sourcemap: false,
      rollupOptions: {
        output: {
          manualChunks: {
            react: ['react', 'react-dom'],
            markdown: ['react-markdown', 'remark-gfm'],
            amap: ['@amap/amap-jsapi-loader'],
            echarts: ['echarts'],
          },
        },
      },
    },
  };
});
