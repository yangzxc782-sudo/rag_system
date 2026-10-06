import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.QA_E2E_PORT ?? 3305);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error("Invalid local E2E port");
export default defineConfig({
  testDir: "./tests",
  outputDir: process.env.QA_E2E_OUTPUT_DIR ?? "../backend/.phase13-m5.tmp/playwright-results",
  timeout: 30_000, expect: { timeout: 7000 }, fullyParallel: true, workers: 2, retries: 0,
  reporter: [["list"], ["junit", { outputFile: process.env.QA_E2E_REPORT ?? `../backend/.phase13-m5.tmp/frontend-${process.env.QA_E2E_MANIFEST ? "real" : "mock"}.xml` }]],
  use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "mock-api", testMatch: /(?:^|[\\/])(?:chat|contracts|casting-design)\.spec\.ts$/ },
    ...(process.env.QA_E2E_MANIFEST ? [{ name: "isolated-postgresql", testMatch: /real-chat\.spec\.ts/ }] : []),
    ...(process.env.CASTING_E2E_MANIFEST ? [{ name: "isolated-casting", testMatch: /real-casting\.spec\.ts/ }] : []),
  ],
  webServer: { command: `node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port ${port}`, url: `http://127.0.0.1:${port}/rag`,
    reuseExistingServer: false, timeout: 60_000 },
});
