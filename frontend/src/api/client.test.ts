import { describe, expect, it } from "vitest";
import {
  ApiError,
  apiUrl,
  buildQuery,
  describeError,
  errorDetail,
  isAuthError,
  isForbiddenError,
  isMockMode,
  liveSocketUrl,
  retryQuery,
} from "./client";

describe("buildQuery", () => {
  it("is empty when there is nothing to send", () => {
    expect(buildQuery(undefined)).toBe("");
    expect(buildQuery({})).toBe("");
  });

  it("repeats an array param, the way FastAPI reads them", () => {
    expect(buildQuery({ talkgroup: [1101, 1102] })).toBe("?talkgroup=1101&talkgroup=1102");
  });

  it("drops what a cleared filter leaves behind", () => {
    expect(buildQuery({ q: "", tag: null, starred: undefined, page: 1 })).toBe("?page=1");
    expect(buildQuery({ tag: ["", "review"] })).toBe("?tag=review");
  });

  it("sorts its keys, so the same filter is always the same string", () => {
    expect(buildQuery({ z: 1, a: 2 })).toBe(buildQuery({ a: 2, z: 1 }));
  });

  it("encodes values that need it", () => {
    expect(buildQuery({ q: '"structure fire"' })).toBe("?q=%22structure+fire%22");
    expect(buildQuery({ time_from: "07:00" })).toBe("?time_from=07%3A00");
  });

  it("sends booleans as the words the API expects", () => {
    expect(buildQuery({ starred: true, include_duplicates: false })).toBe(
      "?include_duplicates=false&starred=true",
    );
  });
});

describe("URL builders", () => {
  it("prefixes every path with /api", () => {
    expect(apiUrl("/calls/12/audio")).toBe("/api/calls/12/audio");
    expect(apiUrl("/calls", { page: 2 })).toBe("/api/calls?page=2");
  });

  it("builds the live socket URL from the page's own origin", () => {
    expect(liveSocketUrl()).toBe(`ws://${window.location.host}/api/live`);
    expect(liveSocketUrl({ talkgroup: [1] })).toBe(`ws://${window.location.host}/api/live?talkgroup=1`);
  });
});

describe("error classification", () => {
  it("recognizes the two statuses the UI reacts to", () => {
    expect(isAuthError(new ApiError("no", 401, "/x"))).toBe(true);
    expect(isForbiddenError(new ApiError("no", 403, "/x"))).toBe(true);
    expect(isAuthError(new ApiError("no", 500, "/x"))).toBe(false);
    expect(isAuthError(new Error("boom"))).toBe(false);
  });

  it("keeps the API's own detail message", () => {
    const error = new ApiError("404 Not Found", 404, "/api/calls/9", "No such call");
    expect(error.detail).toBe("No such call");
    expect(error.status).toBe(404);
  });
});

describe("mock mode", () => {
  it("is on for the test suite", () => {
    expect(isMockMode()).toBe(true);
  });
});

describe("retryQuery", () => {
  it("retries a failed query once", () => {
    const error = new ApiError("500 Internal Server Error", 500, "/api/stats");
    expect(retryQuery(0, error)).toBe(true);
    expect(retryQuery(1, error)).toBe(false);
    expect(retryQuery(0, new TypeError("Failed to fetch"))).toBe(true);
  });

  it("never retries a 410: the thing is gone for good", () => {
    expect(retryQuery(0, new ApiError("410 Gone", 410, "/api/calls/1/waveform"))).toBe(false);
  });
});

/** Review 2026-10-29: "500 — boom" is evidence, not a message. */
describe("describeError", () => {
  it("leads a 500 with a sentence and keeps the server's text as the detail", () => {
    const text = describeError(new ApiError("500 Internal Server Error", 500, "/api/calls", "boom"));
    expect(text.message).toMatch(/^Something went wrong on the server/);
    expect(text.message).not.toContain("boom");
    expect(text.detail).toBe("Error 500: boom");
  });

  it("passes on a 4xx reason the API wrote for people", () => {
    const text = describeError(new ApiError("409 Conflict", 409, "/x", "callsign 'E1' already exists"));
    expect(text.message).toBe("callsign 'E1' already exists");
    expect(text.detail).toBe("Error 409: callsign 'E1' already exists");
  });

  it("does not show a validation list or an echoed status text as the message", () => {
    const list = describeError(new ApiError("422 Unprocessable", 422, "/x", '[{"loc":["body"]}]'));
    expect(list.message).toBe("The server did not accept this request.");
    const echoed = describeError(new ApiError("404 Not Found", 404, "/x", "Not Found"));
    expect(echoed.message).toMatch(/could not be found/);
    expect(echoed.detail).toBe("Error 404");
  });

  it("explains an unreachable server and the auth statuses in plain words", () => {
    expect(describeError(new ApiError("Network error", 0, "/x", "Failed to fetch")).message).toMatch(
      /could not be reached/,
    );
    expect(describeError(new ApiError("401", 401, "/x", "Not authenticated")).message).toBe(
      "Sign in to see or do this.",
    );
    expect(describeError(new ApiError("503", 503, "/x", "upstream")).message).toMatch(/not responding/);
  });

  it("keeps a plain Error's own message, and has words for anything else", () => {
    expect(describeError(new Error("Pick a file first")).message).toBe("Pick a file first");
    expect(describeError("nope").message).toBe("Something went wrong.");
  });

  it("is what a failed-mutation toast shows as its body", () => {
    expect(errorDetail(new ApiError("500", 500, "/x", "Traceback ..."))).not.toContain("Traceback");
  });
});
