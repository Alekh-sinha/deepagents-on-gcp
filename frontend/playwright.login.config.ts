import { defineConfig } from "@playwright/test";

// The sign-in page against a second dev stack that uses the Google sign-in route (with a stub in place of Google).
export default defineConfig({
  testDir: "./e2e-login",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:5174", viewport: { width: 1280, height: 820 } },
  webServer: [
    { command: "cd .. && .venv/bin/python scripts/dev_stack.py --auth google-fake --port 8082 --engine-port 8090 --no-mcp", url: "http://127.0.0.1:8082/healthz", reuseExistingServer: true, timeout: 90_000 },
    { command: "PRIVAI_BACKEND=http://127.0.0.1:8082 npm run dev -- --host 127.0.0.1 --port 5174", url: "http://localhost:5174", reuseExistingServer: true, timeout: 60_000 },
  ],
});
