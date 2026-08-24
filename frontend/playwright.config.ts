import { defineConfig } from "@playwright/test";

/**
 * E2E smoke against the LIVE compose stack (make up-m3 && make seed) — the
 * same two-window story tools/demo/place-order.sh proves over curl, proved
 * here through the actual UI. Not mocked, deliberately: the suite's value
 * is that a real saga, real kitchen, and real courier timers sit behind
 * every click, so timeouts are sized for the world (SETTLED ≈ 55s after
 * "Food is ready").
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 150_000,
  expect: { timeout: 15_000 },
  // One worker: both specs drive the SAME seeded kitchen; parallel runs
  // would interleave their feed clicks.
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: true, // the dev server is usually already up
    timeout: 30_000,
  },
});
