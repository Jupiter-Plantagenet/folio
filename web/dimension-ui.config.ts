import { defineConfig } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const configDirectory = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  testDir: "./dimension-tests",
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:5174",
    headless: true,
    channel: "chrome",
  },
  reporter: [["list"]],
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5174 --strictPort",
    cwd: configDirectory,
    url: "http://127.0.0.1:5174/",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
