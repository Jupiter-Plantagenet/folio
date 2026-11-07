import { defineConfig } from "@playwright/test";
// Live retained-stack journeys (round 4). System Microsoft Edge, headless.
export default defineConfig({
  testDir: "./live-tests",
  workers: 1,
  timeout: 120000,
  retries: 0,
  use: {
    baseURL: process.env.FOLIO_UI_ORIGIN || "http://127.0.0.1:5101",
    channel: "msedge",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  outputDir: "../.cache/round4-scratch/pw-live",
  reporter: [
    ["list"],
    ["json", { outputFile: "../evidence/documents/flow/round4-live/results.json" }],
  ],
});
