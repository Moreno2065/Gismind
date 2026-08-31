import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const backendRoot = path.resolve(__dirname, '../backend');
const backendPython = process.env.GISMIND_BACKEND_PYTHON
  ?? path.resolve(backendRoot, '.venv/Scripts/python.exe');
const viteEntry = path.resolve(__dirname, 'node_modules/vite/bin/vite.js');

// Use ports that differ from the developer defaults. This prevents Chromium
// verification from silently exercising a pre-existing local server.
const e2ePort = process.env.GISMIND_E2E_PORT ?? '18000';
const vitePort = process.env.GISMIND_E2E_VITE_PORT ?? '15173';
const redisUrl = process.env.GISMIND_TEST_REDIS_URL ?? 'redis://localhost:6379/15';
const externalServers = process.env.GISMIND_E2E_EXTERNAL_SERVERS === '1';

/**
 * Real browser wiring: Chromium → Vite (:5173) → proxy /api → FastAPI e2e server.
 * No page.route / MSW / request interception — only passive waitForRequest.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 30_000 },
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${vitePort}`,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: externalServers ? undefined : [
    {
      // Quote-safe on Windows (path may contain spaces).
      command: JSON.stringify(backendPython) + ' scripts/e2e_awaiting_server.py',
      cwd: backendRoot,
      url: `http://127.0.0.1:${e2ePort}/api/health`,
      // Always start the deterministic e2e server; never reuse a random :8000.
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        ...process.env,
        GISMIND_TEST_REDIS_URL: redisUrl,
        GISMIND_E2E_HOST: '127.0.0.1',
        GISMIND_E2E_PORT: String(e2ePort),
        GISMIND_E2E_UPLOAD_TTL_S: '5',
        APP_ENV: 'dev',
      },
    },
    {
      // Invoke the checked-in Vite dependency directly.  On Windows an `npx`
      // wrapper can outlive (or lose) its child process between Playwright runs.
      command: `${JSON.stringify(process.execPath)} ${JSON.stringify(viteEntry)} --host 127.0.0.1 --port ${vitePort} --strictPort`,
      cwd: __dirname,
      url: `http://127.0.0.1:${vitePort}`,
      // A previous short-lived E2E server is not valid production-wiring evidence.
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        ...process.env,
        GISMIND_VITE_API_TARGET: `http://127.0.0.1:${e2ePort}`,
      },
    },
  ],
});
