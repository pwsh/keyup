import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests. These run against the **real** API — `VITE_MOCK=0` and the
 * dev server proxying `/api` to a running Keyup backend — because the whole
 * point of them is to catch the places where the fixtures and the server disagree.
 *
 *     npm run dev            # in one terminal, or let the webServer below do it
 *     npm run e2e
 *
 * `VITE_API_TARGET` points the proxy somewhere other than `http://127.0.0.1:8096`.
 * `E2E_BASE_URL` runs the tests against an already-serving app (a container, a
 * staging deployment) instead of starting one.
 *
 * `E2E_USERNAME` / `E2E_PASSWORD` enable the mutation suite; without them those
 * tests skip, because they write to whatever database the API is pointed at.
 */
const PORT = Number(process.env.E2E_PORT ?? 5174);
const BASE_URL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // A cold Vite dev server compiles the route chunk on first visit, and the calls
  // page asks for 500 rows of real data.
  timeout: 90_000,
  expect: { timeout: 20_000 },
  // The mutation suite signs in and edits shared rows; running it beside the
  // read-only suites would make both flaky.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  outputDir: "e2e/.results",
  use: {
    baseURL: BASE_URL,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      // 1600 is the width at which the calls page shows all three columns — the
      // filter rail, the results and the facet breakdown — so the tests exercise
      // the widest layout. The screenshots take their own, narrower viewport.
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1600, height: 900 },
        launchOptions: {
          // Headless Chrome refuses to start audio without a gesture it recognizes,
          // which makes "press space, expect it to play" flaky. The tests still have
          // to click or type to get there; this only removes the browser's own veto.
          args: ["--autoplay-policy=no-user-gesture-required"],
        },
      },
    },
  ],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `npm run dev -- --port ${PORT} --strictPort`,
        // Probing through the proxy means the server is only "ready" once the API
        // behind it answers too — which is what these tests actually need.
        url: `http://127.0.0.1:${PORT}/api/status`,
        reuseExistingServer: true,
        timeout: 120_000,
        stdout: "ignore",
        stderr: "pipe",
        env: {
          VITE_MOCK: "0",
          VITE_API_TARGET: process.env.VITE_API_TARGET ?? "http://127.0.0.1:8096",
        },
      },
});
