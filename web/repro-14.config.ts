import { defineConfig } from "@playwright/test";

const evidence = process.env.FOLIO_LIVE_EVIDENCE;
const report = process.env.FOLIO_BROWSER_REPORT_PATH;
if (!evidence || !report) {
  throw new Error("FOLIO_LIVE_EVIDENCE and FOLIO_BROWSER_REPORT_PATH must point to private run artifacts");
}

export default defineConfig({
  testDir: "./live-tests",
  testMatch: "live-journeys.spec.ts",
  grep: /J5 all fourteen screens render/,
  workers: 1,
  timeout: 240000,
  retries: 0,
  use: {
    baseURL: process.env.FOLIO_UI_ORIGIN || "http://127.0.0.1:15101",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  outputDir: process.env.FOLIO_BROWSER_OUTPUT_DIR || evidence + "/pw-output",
  reporter: [["list"], ["json", { outputFile: report }]],
});
