/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The browser only ever talks to its own origin; Vite forwards /api to the web backend (`uv run pinboard web`, default port 8081).
const BACKEND = process.env.PRIVAI_BACKEND ?? "http://127.0.0.1:8081";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: BACKEND, changeOrigin: false }, "/healthz": BACKEND },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
