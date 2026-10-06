import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end configuration.
 *
 * Deliberately tiny: one browser, one worker, no retries. These tests drive a real
 * write path, so a flaky pass is worse than a slow one -- a retry could hide an
 * intermittent double-write. `forbidOnly` keeps a stray `.only` from silently reducing
 * the suite to one test in CI.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
