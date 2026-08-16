import { defineConfig } from "@playwright/test";

const PORT = Number(process.env.PORT || 8501);
const BASE = process.env.BASE_URL || `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: /^(?!.*\/\._).*\.spec\.(ts|js)$/,
  timeout: 180_000,
  expect: { timeout: 60_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: BASE,
    browserName: "chromium",
    trace: "on-first-retry",
    screenshot: "off",
    video: "off",
  },
  webServer: {
    command: `cd .. && PYTHONPATH=. .venv/bin/streamlit run e2e/fixture_app.py --server.port=${PORT} --server.address=127.0.0.1 --server.headless=true --browser.gatherUsageStats=false`,
    url: BASE,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    {
      name: "phone",
      use: { browserName: "chromium", viewport: { width: 390, height: 844 } },
    },
    {
      name: "ipad",
      use: { browserName: "chromium", viewport: { width: 768, height: 1024 } },
    },
    {
      name: "desktop",
      use: { browserName: "chromium", viewport: { width: 1280, height: 720 } },
    },
  ],
});
