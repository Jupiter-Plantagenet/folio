import { defineConfig } from "@playwright/test";
// Fixture-backed browser checks for per-source original availability. Every
// /api/ request is answered by authored stubs or aborted; no retained backend.
if (!process.env.FOLIO_ORIGINAL_UI_EVIDENCE)
  throw new Error("Unique evidence directory required");
export default defineConfig({
  testDir: "./original-tests",
  workers: 1,
  timeout: 20000,
  outputDir: process.env.FOLIO_ORIGINAL_UI_EVIDENCE + "/browser",
  reporter: [
    ["list"],
    [
      "json",
      { outputFile: process.env.FOLIO_ORIGINAL_UI_EVIDENCE + "/results.json" },
    ],
  ],
  use: {
    baseURL: "http://127.0.0.1:15171",
    headless: true,
    channel: "msedge",
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 15171 --strictPort",
    url: "http://127.0.0.1:15171",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
