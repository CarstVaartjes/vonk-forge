import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["contract-consumer.spec.ts", "observation-transfer.spec.ts"],
  workers: 1,
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  use: { baseURL: "http://127.0.0.1:4174", trace: "retain-on-failure" },
  webServer: {
    command: "npm exec vite -- --host 127.0.0.1 --port 4174",
    port: 4174,
    reuseExistingServer: false,
  },
});
