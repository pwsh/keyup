import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { useSignInStore } from "./state/auth";
import { renderWithProviders } from "./test/render";

const auth = vi.hoisted(() => ({
  user: null as { id: number; username: string; is_admin: boolean } | null,
  loading: false,
}));

vi.mock("./api/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/hooks")>()),
  useCurrentUser: () => ({
    user: auth.user,
    canWrite: auth.user !== null,
    isAdmin: auth.user?.is_admin === true,
    isLoading: auth.loading,
  }),
}));

beforeEach(() => {
  auth.user = null;
  auth.loading = false;
});

/**
 * The roster, the site list and the alert rules are hidden from a guest's navigation —
 * but a nav that omits a link is not a lock: a bookmark, a shared URL or a typed path
 * all arrive at the route directly. These are about that second half.
 */
describe("the guarded routes", () => {
  for (const path of ["/units", "/personnel", "/sites", "/alerts", "/units/1", "/personnel/1"]) {
    it(`sends a guest from ${path} to the sign-in form`, async () => {
      renderWithProviders(<App />, path);
      expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
      // …and remembers where they were going, so signing in lands there rather than
      // dumping them back on the calls.
      expect(useSignInStore.getState().redirectTo).toBe(path);
    });
  }

  it("lets a signed-in operator through", async () => {
    auth.user = { id: 1, username: "dispatch", is_admin: true };
    renderWithProviders(<App />, "/units");
    expect(await screen.findByRole("heading", { name: "Units" })).toBeInTheDocument();
  });

  it("decides nothing while the session is still being resolved", () => {
    // `GET /auth/me` is in flight on the first paint. Treating "not yet known" as
    // "anonymous" would bounce the operator out of their own page on every reload.
    auth.loading = true;
    renderWithProviders(<App />, "/units");
    expect(screen.queryByRole("heading", { name: "Sign in" })).not.toBeInTheDocument();
    expect(useSignInStore.getState().redirectTo).toBeNull();
  });

  it("leaves the public pages public", async () => {
    renderWithProviders(<App />, "/talkgroups");
    expect(await screen.findByRole("heading", { name: "Talkgroups" })).toBeInTheDocument();
  });
});
