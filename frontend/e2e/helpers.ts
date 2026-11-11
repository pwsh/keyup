import { expect, type Locator, type Page, type Request, type Response } from "@playwright/test";

/**
 * Every route the navigation offers, and what proves it rendered. `requiresAuth` marks
 * the pages App.tsx wraps in `RequireAuth`: a guest is sent to the sign-in page instead.
 */
export const ROUTES: readonly { path: string; name: string; heading: string; requiresAuth?: boolean }[] = [
  { path: "/calls", name: "calls", heading: "Calls" },
  { path: "/search?q=fire", name: "search", heading: "Search" },
  { path: "/live", name: "live", heading: "Live" },
  { path: "/talkgroups", name: "talkgroups", heading: "Talkgroups" },
  { path: "/radios", name: "radios", heading: "Radios and units" },
  { path: "/sites", name: "sites", heading: "Sites", requiresAuth: true },
  { path: "/stats", name: "stats", heading: "Statistics" },
  { path: "/alerts", name: "alerts", heading: "Alerts", requiresAuth: true },
  { path: "/admin", name: "admin", heading: "Admin" },
  { path: "/help", name: "help", heading: "Help" },
];

/** Whether this browser context is signed out — what the app calls a guest. */
export async function isGuest(page: Page): Promise<boolean> {
  const response = await page.request.get("/api/auth/me");
  if (response.status() === 401) return true;
  expect(response.ok(), `GET /api/auth/me → ${response.status()}`).toBe(true);
  const me = (await response.json()) as { user: unknown };
  return me.user == null;
}

export interface Problems {
  /** Console errors and uncaught exceptions, in order. */
  console: string[];
  /** `status method path` for every API response of 400 or worse. */
  requests: string[];
}

/**
 * Records the two things a page should never produce: a console error and a
 * failed API request.
 *
 * Signed out, the app deliberately *asks* for things it cannot have — the star
 * button, a rescan — and the API deliberately answers 401. Those are the app
 * working, so `allow401` lets a test say so rather than papering over the rest.
 */
export function watchForProblems(page: Page, options: { allow401?: boolean } = {}): Problems {
  const problems: Problems = { console: [], requests: [] };

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    // Chrome logs a console error for every media/network 4xx even when the page
    // handled it; the request watcher below is the authority on those.
    if (/Failed to load resource/.test(text)) return;
    problems.console.push(text);
  });
  page.on("pageerror", (error) => problems.console.push(`uncaught: ${error.message}`));
  page.on("requestfailed", (request: Request) => {
    // A navigation the test itself canceled is not a failure.
    if (request.failure()?.errorText === "net::ERR_ABORTED") return;
    problems.requests.push(`failed ${request.method()} ${short(request.url())}`);
  });
  page.on("response", (response: Response) => {
    if (!response.url().includes("/api/")) return;
    const status = response.status();
    if (status < 400) return;
    if (options.allow401 && status === 401) return;
    problems.requests.push(`${status} ${response.request().method()} ${short(response.url())}`);
  });

  return problems;
}

const short = (url: string): string => url.replace(/^https?:\/\/[^/]+/, "");

/** Asserts a page produced neither a console error nor a failed request. */
export function expectClean(problems: Problems, where: string): void {
  expect(problems.console, `console errors on ${where}`).toEqual([]);
  expect(problems.requests, `failed API requests on ${where}`).toEqual([]);
}

/**
 * Waits until the page has stopped fetching. `networkidle` alone is not enough:
 * TanStack Query fires a second round of requests once the first ones resolve, and
 * ECharts paints a frame after that.
 */
export async function settle(page: Page, extraMs = 1200): Promise<void> {
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(extraMs);
}

/**
 * Writes to the API directly, for the "put it back the way you found it" half of a
 * mutation test. The `Origin` header is not optional: the API rejects a
 * cookie-authenticated mutation without one, and Playwright's request context — a
 * program, not a browser — does not add it.
 */
export async function apiPatch(page: Page, path: string, data: unknown): Promise<void> {
  const response = await page.request.patch(`/api${path}`, {
    headers: { "X-Requested-With": "keyup", Origin: new URL(page.url()).origin },
    data,
  });
  expect(response.ok(), `PATCH /api${path} → ${response.status()}: could not restore the row`).toBe(true);
}

/** Reads the API directly, for the fixtures a test needs to know before it starts. */
export async function api<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(`/api${path}`);
  expect(response.ok(), `GET /api${path} → ${response.status()}`).toBe(true);
  return (await response.json()) as T;
}

/**
 * Runs `action` and returns the JSON of the first successful `GET` it caused whose path
 * and query satisfy `matches` — the answer the *page* got, rather than a second question
 * asked a moment later that a busy archive may already answer differently.
 */
export async function pageResponse<T>(
  page: Page,
  matches: (url: URL) => boolean,
  action: () => Promise<unknown>,
): Promise<T> {
  // The body is read the moment the response lands, not after `action` settles: by the
  // time a `goto` has fired `load` Chromium may already have dropped it.
  const body = page
    .waitForResponse(
      (candidate) =>
        candidate.request().method() === "GET" && candidate.ok() && matches(new URL(candidate.url())),
    )
    .then((response) => response.json() as Promise<T>);
  body.catch(() => undefined); // if `action` throws, that is the failure worth reporting
  await action();
  return await body;
}

/** The rows of the calls table, whichever layout the viewport is showing. */
export function callRows(page: Page): Locator {
  return page.getByRole("button", { name: /^\w{3} \d+, \d\d:\d\d:\d\d,/ });
}

export interface CallRowShape {
  id: number;
  duration_s: number;
  talkgroup_id: number | null;
  talkgroup_alias: string | null;
  tgid: number | null;
  transcript_text: string | null;
  transcript_status: string;
  conversation_id: number | null;
  audio_state: string;
}
