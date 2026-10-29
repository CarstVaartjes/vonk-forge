import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
// runtime.generated.js is ~30 MB of compiled validators with no JSX. Running it
// through the React plugin made a cold transform need 3-4 GB of heap, which sat
// at Node's default limit and killed the admin web suite with exit 134.
export default defineConfig({
  plugins: [react({ exclude: [/runtime\.generated\.js$/] })],
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: process.env.VONK_LIVE_ORIGIN ?? "http://localhost" } },
    setupFiles: "./src/test-setup.ts",
    globals: true,
    exclude: ["e2e/**", "node_modules/**"],
  },
});
