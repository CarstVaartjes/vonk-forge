import {defineConfig, devices} from "@playwright/test";

const previewPort = Number(process.env.PLAYWRIGHT_PORT ?? 4173);

export default defineConfig({
  fullyParallel: true,
  testDir: "./e2e",
  outputDir: "./test-results",
  reporter: [["list"], ["html", {open: "never", outputFolder: "playwright-report"}]],
  workers: 2,
  projects: [{name: "chromium", use: {...devices["Desktop Chrome"]}}],
  use: {
    baseURL: `http://127.0.0.1:${previewPort}`,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `npm run build && npm exec vite preview -- --host 127.0.0.1 --port ${previewPort}`,
    port: previewPort,
    reuseExistingServer: true,
  },
});
