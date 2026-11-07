import { defineConfig } from "@playwright/test";
if (!process.env.FOLIO_EXPORT_UI_EVIDENCE)
  throw new Error("Unique evidence directory required");
export default defineConfig({
  testDir: "./export-tests",
  workers: 1,
  timeout: 20000,
  outputDir: process.env.FOLIO_EXPORT_UI_EVIDENCE + "/browser",
  reporter: [
    ["list"],
    [
      "json",
      { outputFile: process.env.FOLIO_EXPORT_UI_EVIDENCE + "/results.json" },
    ],
  ],
  use: {
    baseURL: "http://127.0.0.1:15161",
    headless: true,
    channel: "msedge",
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 15161 --strictPort",
    url: "http://127.0.0.1:15161",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
