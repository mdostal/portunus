import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// Every spawn of `portunus` from the UI server hits the stub in
// e2e/fixtures/bin, which appends its argv here (see e2e/api-guard.spec.ts).
export const STUB_LOG = path.join(__dirname, ".e2e-portunus-stub.log");

// The write credential the test server requires (lib/write-auth.ts). A fixed,
// test-only value; specs send it in X-Portunus-Write-Token.
export const WRITE_TOKEN_FILE = path.join(__dirname, "e2e", "fixtures", "write-token");
export const WRITE_TOKEN = fs.readFileSync(WRITE_TOKEN_FILE, "utf8").trim();

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:3100",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run build && npm run start -- --port 3100",
    url: "http://127.0.0.1:3100",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
    env: {
      PATH: `${path.join(__dirname, "e2e", "fixtures", "bin")}${path.delimiter}${process.env.PATH}`,
      PORTUNUS_STUB_LOG: STUB_LOG,
      PORTUNUS_WRITE_TOKEN_FILE: WRITE_TOKEN_FILE,
    },
  },
});
