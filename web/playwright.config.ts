import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: process.env.FOLIO_UI_ORIGIN || "http://127.0.0.1:5101",
    headless: true,
    trace: "retain-on-failure",
  },
  reporter: [
    ["list"],
    [
      "json",
      {
        outputFile:
          process.env.FOLIO_BROWSER_REPORT_PATH ||
          "../evidence/documents/flow/browser-results.json",
      },
    ],
  ],
});
