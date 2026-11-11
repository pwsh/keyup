/**
 * Typed fetch wrapper for the Keyup API.
 *
 * Two modes. With `VITE_MOCK=1` (or `VITE_MOCK=true`) every request is served
 * from `./mock`, dynamically imported so the fixtures never reach the production
 * bundle, through exactly the same function signatures the real client offers.
 *
 * In dev the mock also switches itself on when the API turns out to be
 * unreachable. Two things count as unreachable: `fetch` rejecting outright, and
 * the Vite dev proxy's 503 + `X-Api-Unreachable` (see `vite.config.ts`), which is
 * what a refused connection to the API port looks like from the browser. Either
 * latches mock mode on for the rest of the session and retries the request
 * against the fixtures, so `npm run dev` shows a working app with no backend
 * running. Every other HTTP status — 404, 500, 401 — is a *working* API saying
 * no, and never triggers it.
 */

export type QueryPrimitive = string | number | boolean | null | undefined;
export type QueryValue = QueryPrimitive | readonly (string | number)[];
export type QueryParams = Record<string, QueryValue>;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly url: string;

  constructor(message: string, status: number, url: string, detail?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
    this.detail = detail ?? message;
  }
}

/**
 * The sentence to show a person when a request fails.
 *
 * `Error.message` on an `ApiError` is the status line — "409 Conflict" — and the half that
 * is worth reading is `detail`, which is what the API wrote: *why* the merge was refused,
 * *what* is still referencing the site, *which* recorder is still reporting. Every toast
 * that reports a failed mutation wants that one, and several were showing the status line.
 */
export function errorDetail(error: unknown): string | undefined {
  if (error instanceof ApiError || error instanceof Error) return describeError(error).message || undefined;
  return undefined;
}

/** A failure in the reader's words, with what the server literally said kept for a second look. */
export interface ErrorText {
  /** The sentence to show: what happened and, where there is one, what to do. */
  message: string;
  /** The status and the server's own text — for a "details" fold or a bug report; null when there is none. */
  detail: string | null;
}

/**
 * The server's `detail`, when it is a sentence written for a person.
 *
 * A 409 or 422 from this API usually is — "callsign 'E1' already exists" — and is the one
 * part of the reply that says what to do differently. A FastAPI validation list (JSON), a
 * bare status text echoed back, or a Python exception string from a 500 is not.
 */
function readableDetail(error: ApiError): string | null {
  const text = error.detail.trim();
  if (!text || error.message.endsWith(text) || /^[[{]/.test(text)) return null;
  if (error.status >= 500 || error.status === 0) return null;
  return text;
}

const STATUS_MESSAGES: Record<number, string> = {
  401: "Sign in to see or do this.",
  403: "Your account does not have permission for this.",
  404: "This could not be found. It may have been deleted, or the link is out of date.",
  408: "The server took too long to answer. Try again in a moment.",
  410: "This is no longer available; it has been removed from the archive.",
  413: "That is too large for the server to accept.",
  429: "Too many requests at once. Wait a moment and try again.",
  502: "Keyup is not responding right now. Try again in a moment.",
  503: "Keyup is not responding right now. Try again in a moment.",
  504: "Keyup took too long to answer. Try again in a moment.",
};

/**
 * Turn any thrown value into something a person can read.
 *
 * The status line and the server's raw text ("500 — boom") are not a message; they are
 * evidence. This keeps them as `detail` and leads with a sentence: the API's own reason
 * when it wrote one for people (most 4xx), otherwise a plain description of the status.
 */
export function describeError(error: unknown): ErrorText {
  if (error instanceof ApiError) {
    const raw = error.detail.trim();
    const technical =
      error.status === 0
        ? raw || null
        : `Error ${error.status}${raw && raw !== error.message && !error.message.endsWith(raw) ? `: ${raw}` : ""}`;
    if (error.status === 0) {
      return {
        message: "Keyup could not be reached. Check the connection and try again.",
        detail: technical,
      };
    }
    const readable = readableDetail(error);
    if (readable && error.status !== 401 && error.status !== 403)
      return { message: readable, detail: technical };
    const known = STATUS_MESSAGES[error.status];
    if (known) return { message: known, detail: technical };
    if (error.status >= 500) {
      return {
        message:
          "Something went wrong on the server. Try again; if it keeps happening, tell an administrator.",
        detail: technical,
      };
    }
    return { message: "The server did not accept this request.", detail: technical };
  }
  if (error instanceof Error && error.message.trim()) return { message: error.message, detail: null };
  return { message: "Something went wrong.", detail: null };
}

/** 401: a mutation arrived without a session. The UI offers the sign-in page. */
export function isAuthError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 401;
}

/** 403: signed in, but without the scope this action needs. */
export function isForbiddenError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 403;
}

/**
 * The app's query retry policy: one retry, except for a `410 Gone`. A 410 is the API saying
 * the thing is permanently not there (a purged recording, a vacuumed export bundle), so a
 * second request can only get the same answer.
 */
export function retryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status === 410) return false;
  return failureCount < 1;
}

/**
 * Builds a query string. Arrays are repeated (`?talkgroup=1101&talkgroup=1102`)
 * because that is what FastAPI's repeatable query params expect; `null`,
 * `undefined` and `""` are dropped so cleared filters leave the URL entirely.
 * Keys are sorted so the same filter always produces the same string — which is
 * what makes a query key, and the browser cache, stable.
 */
export function buildQuery(params: QueryParams | undefined): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const key of Object.keys(params).sort()) {
    const value = params[key];
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item === null || item === undefined || item === "") continue;
        search.append(key, String(item));
      }
      continue;
    }
    if (value === "") continue;
    search.append(key, String(value as QueryPrimitive));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

/** Widen a typed filter object (`CallFilterParams` and friends) to the generic bag. */
export function toQueryParams(params: object): QueryParams {
  return params as QueryParams;
}

/**
 * Fixed at build time. `import.meta.env.VITE_MOCK` is substituted as a literal, so
 * `MOCK_BUILD` is a constant and `loadMock` below folds to `null` in a production
 * build — which is what keeps the whole fixture corpus out of the bundle, not just
 * off the execution path.
 */
const MOCK_BUILD = import.meta.env.VITE_MOCK === "1" || import.meta.env.VITE_MOCK === "true";

/**
 * The fixtures, or nothing.
 *
 * `null` in a production build without `VITE_MOCK=1`: the dynamic import sits in a
 * branch the bundler can prove is dead, so it is dropped along with every chunk it
 * reaches. A release therefore cannot serve sample data even if something set the
 * latch, because there is no sample data in it.
 */
const loadMock =
  MOCK_BUILD || import.meta.env.DEV ? (): Promise<typeof import("./mock")> => import("./mock") : null;

/** The same, for the live feed's replay of the fixture corpus (`live.ts`). */
export const loadMockLive =
  MOCK_BUILD || import.meta.env.DEV
    ? (): Promise<typeof import("./mock/live")> => import("./mock/live")
    : null;

/** The same, for the spectrum feed's synthetic FFT frames (`live.ts`). */
export const loadMockSpectrum =
  MOCK_BUILD || import.meta.env.DEV
    ? (): Promise<typeof import("./mock/spectrum")> => import("./mock/spectrum")
    : null;

const envMock = (): boolean => MOCK_BUILD;

/** Latched on by the dev fallback; never set in a production build. */
let fallbackMock = false;
const mockListeners = new Set<() => void>();

export function isMockMode(): boolean {
  return envMock() || fallbackMock;
}

/** Re-render the connection badge when the fallback latches. */
export function subscribeMockMode(listener: () => void): () => void {
  mockListeners.add(listener);
  return () => {
    mockListeners.delete(listener);
  };
}

function latchMockMode(): void {
  if (fallbackMock) return;
  fallbackMock = true;
  for (const listener of mockListeners) listener();
}

/** Tests reset the latch between cases. */
export function resetMockFallback(): void {
  fallbackMock = false;
}

/** Absolute-in-origin URL for a resource — audio, exports, playlists. */
export function apiUrl(path: string, params?: QueryParams): string {
  return `/api${path}${buildQuery(params)}`;
}

/** `ws(s)://…/api/live` for the live feed, with the filter already applied. */
export function liveSocketUrl(params?: QueryParams): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/live${buildQuery(params)}`;
}

export interface ApiResponse<T> {
  data: T;
  headers: Headers;
}

async function readError(res: Response, url: string): Promise<ApiError> {
  let detail = res.statusText;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
    else if (body.detail) detail = JSON.stringify(body.detail);
  } catch {
    /* non-JSON error body: keep the status text */
  }
  return new ApiError(`${res.status} ${res.statusText}`, res.status, url, detail);
}

/**
 * Sent on every request. The API requires it on anything that changes data: a
 * custom header cannot be set by a cross-site form or image, so together with the
 * same-origin `Origin` the browser adds it makes CSRF against the session cookie
 * impractical. Harmless on reads.
 */
const REQUESTED_WITH = { "X-Requested-With": "keyup" } as const;

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

async function mockRequest<T>(
  method: HttpMethod,
  path: string,
  params?: QueryParams,
  body?: unknown,
): Promise<ApiResponse<T>> {
  if (loadMock === null) {
    // Unreachable: `isMockMode()` is false in such a build and the dev fallback is
    // compiled out. Kept as an error rather than a silent empty response, because a
    // request arriving here would mean the mode flags had come apart.
    throw new ApiError("This build has no sample data", 500, path);
  }
  const { handleMock } = await loadMock();
  return handleMock<T>(method, path, params, body);
}

async function request<T>(
  method: HttpMethod,
  path: string,
  params?: QueryParams,
  body?: unknown,
  signal?: AbortSignal,
): Promise<ApiResponse<T>> {
  if (isMockMode()) return mockRequest<T>(method, path, params, body);

  const url = apiUrl(path, params);
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      signal,
      // The session cookie is same-origin (nginx proxies /api), so this is all the
      // credential handling the app needs — no tokens are stored in JS.
      credentials: "same-origin",
      headers:
        body === undefined
          ? { Accept: "application/json", ...REQUESTED_WITH }
          : { Accept: "application/json", "Content-Type": "application/json", ...REQUESTED_WITH },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    // An aborted request is the caller changing its mind, not a dead API.
    if (signal?.aborted) throw cause;
    if (import.meta.env.DEV) {
      latchMockMode();
      return mockRequest<T>(method, path, params, body);
    }
    throw new ApiError(
      "Network error — is the API running?",
      0,
      url,
      cause instanceof Error ? cause.message : String(cause),
    );
  }
  if (import.meta.env.DEV && res.status === 503 && res.headers.get("X-Api-Unreachable")) {
    latchMockMode();
    return mockRequest<T>(method, path, params, body);
  }
  if (!res.ok) throw await readError(res, url);
  if (res.status === 204) return { data: undefined as T, headers: res.headers };
  return { data: (await res.json()) as T, headers: res.headers };
}

export async function apiGet<T>(path: string, params?: QueryParams, signal?: AbortSignal): Promise<T> {
  return (await request<T>("GET", path, params, undefined, signal)).data;
}

/** For the list endpoints, whose total arrives in the `X-Total-Count` header. */
export async function apiGetRaw<T>(
  path: string,
  params?: QueryParams,
  signal?: AbortSignal,
): Promise<ApiResponse<T>> {
  return request<T>("GET", path, params, undefined, signal);
}

export async function apiPost<T>(path: string, body?: unknown, params?: QueryParams): Promise<T> {
  return (await request<T>("POST", path, params, body ?? {})).data;
}

/** A response that is a file rather than a document — the bytes, and the headers with them. */
export interface BlobResponse {
  blob: Blob;
  headers: Headers;
}

/**
 * A POST whose *answer* is a file: today the audio-profile preview, which returns the
 * rendered MP3 itself rather than a URL to fetch it from.
 *
 * It cannot go through `request` because that parses every body as JSON, and it cannot be
 * a plain `fetch` because then it would be the one call in the app that forgets the CSRF
 * header, the same-origin credentials or `ApiError`'s detail sentence. So it is the same
 * request `request` makes, read as bytes instead — including the mock branch, because the
 * fixtures have to answer this route too or the demo's editor plays nothing.
 *
 * The headers come back with the blob because the interesting half of this response is in
 * them: `X-Keyup-Filter` is the chain that produced the audio, which is what makes the
 * preview a reading rather than a black box.
 */
export async function apiPostBlob(path: string, body: unknown): Promise<BlobResponse> {
  if (isMockMode()) {
    if (loadMock === null) throw new ApiError("This build has no sample data", 500, path);
    const { handleMockBlob } = await loadMock();
    return handleMockBlob(path, body);
  }
  const url = apiUrl(path);
  let res: Response;
  try {
    res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "audio/mpeg, application/json",
        "Content-Type": "application/json",
        ...REQUESTED_WITH,
      },
      body: JSON.stringify(body ?? {}),
    });
  } catch (cause) {
    throw new ApiError(
      "Network error — is the API running?",
      0,
      url,
      cause instanceof Error ? cause.message : String(cause),
    );
  }
  // The failures here — 404, 422, 503 — all carry a JSON `detail`, so the error path is
  // the ordinary one even though the success path is not.
  if (!res.ok) throw await readError(res, url);
  return { blob: await res.blob(), headers: res.headers };
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return (await request<T>("PATCH", path, undefined, body)).data;
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  return (await request<T>("PUT", path, undefined, body)).data;
}

export async function apiDelete<T>(path: string, params?: QueryParams): Promise<T> {
  return (await request<T>("DELETE", path, params)).data;
}
