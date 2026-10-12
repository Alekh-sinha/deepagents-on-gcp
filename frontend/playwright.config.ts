import { defineConfig } from "@playwright/test";

// The tests drive the real UI against the dev stack (the real engine and web backend with a scripted demo model; see scripts/dev_stack.py).
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: 0,
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:5173", viewport: { width: 1280, height: 820 }, screenshot: "off", trace: "off" },
  webServer: [
    { command: "cd .. && uv run python scripts/dev_stack.py", url: "http://127.0.0.1:8081/healthz", reuseExistingServer: true, timeout: 90_000 },
    { command: "npm run dev -- --host 127.0.0.1", url: "http://localhost:5173", reuseExistingServer: true, timeout: 60_000 },
  ],
});
