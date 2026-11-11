/**
 * The 2026-10-29 UI review, batch 1 (page behaviour): one case per fix that a test can
 * hold in place. Numbers are the review's.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequireAdmin, RequireAuth } from "../App";
import * as client from "../api/client";
import { apiPost } from "../api/client";
import { useLiveFeed } from "../api/live";
import { CALLS, TALKGROUPS } from "../api/mock/dataset";
import { AppLayout } from "../components/layout/AppLayout";
import { renderWithProviders } from "../test/render";
import { CallDetail } from "./CallDetail";
import { Calls } from "./Calls";
import { Live } from "./Live";
import { Login } from "./Login";
import { TalkgroupDetail } from "./TalkgroupDetail";

/**
 * `useMediaQuery` caches each query's `MediaQueryList`, whose jsdom stub answers once,
 * so a width set in a test would never be seen. This answers against the width now.
 */
vi.mock("../lib/useMediaQuery", () => ({
  useMediaQuery: (query: string) => {
    const min = /\(min-width:\s*(\d+)px\)/.exec(query);
    return min ? window.innerWidth >= Number(min[1]) : false;
  },
}));

const signIn = (username = "alex") => apiPost("/auth/login", { username, password: "hunter2" });

afterEach(() => {
  vi.restoreAllMocks();
  window.innerWidth = 1440;
});

/** Every path `apiGet` was asked for, with its params. */
function spyGets() {
  return vi.spyOn(client, "apiGet");
}
const askedPaths = (spy: ReturnType<typeof spyGets>) => spy.mock.calls.map(([path]) => path);

describe("#13 operator pages are guarded", () => {
  const guarded = (route: string, admin: boolean) =>
    renderWithProviders(
      <Routes>
        <Route
          path="/activity"
          element={
            admin ? (
              <RequireAdmin>
                <p>the activity log</p>
              </RequireAdmin>
            ) : (
              <RequireAuth>
                <p>the admin page</p>
              </RequireAuth>
            )
          }
        />
        <Route path="/login" element={<p>the sign-in form</p>} />
      </Routes>,
      route,
    );

  it("sends a guest to sign in", async () => {
    guarded("/activity", true);
    expect(await screen.findByText("the sign-in form")).toBeInTheDocument();
    expect(screen.queryByText("the activity log")).not.toBeInTheDocument();
  });

  it("tells a signed-in account without the admin scope that it is for administrators", async () => {
    await signIn("dispatch");
    guarded("/activity", true);
    expect(await screen.findByText("Administrators only")).toBeInTheDocument();
    expect(screen.queryByText("the activity log")).not.toBeInTheDocument();
  });

  it("shows an administrator the page", async () => {
    await signIn("alex");
    guarded("/activity", true);
    expect(await screen.findByText("the activity log")).toBeInTheDocument();
  });

  it("lets any signed-in account through a RequireAuth page", async () => {
    await signIn("dispatch");
    guarded("/activity", false);
    expect(await screen.findByText("the admin page")).toBeInTheDocument();
  });
});

describe("#5 the Calls breakdown is only asked for while it is shown", () => {
  it("does not fetch the facets below 1600 px, where the column does not exist", async () => {
    const get = spyGets();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    await screen.findByRole("list", { name: "Calls" });
    expect(askedPaths(get)).toContain("/calls");
    expect(askedPaths(get)).not.toContain("/calls/facets");
  });

  it("fetches them at 1600 px and up with the column open", async () => {
    window.innerWidth = 1700;
    const get = spyGets();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    await waitFor(() => expect(askedPaths(get)).toContain("/calls/facets"));
  });

  it("does not fetch them at 1600 px when the reader closed the column", async () => {
    window.innerWidth = 1700;
    localStorage.setItem("keyup-facets", "closed");
    const get = spyGets();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    await screen.findByRole("list", { name: "Calls" });
    expect(askedPaths(get)).not.toContain("/calls/facets");
    localStorage.removeItem("keyup-facets");
  });
});

describe("#28 the Calls count", () => {
  it("never says 0 calls while the first page is loading", async () => {
    renderWithProviders(<Calls />, "/calls?preset=7d");
    expect(screen.getByText("Loading calls…")).toBeInTheDocument();
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/^\s*calls$/)).toBeInTheDocument());
    expect(screen.queryByText("Loading calls…")).not.toBeInTheDocument();
  });

  it("shows no Live pill over a list that failed to load", async () => {
    vi.spyOn(client, "apiGet").mockImplementation(async (path) => {
      if (path === "/calls") throw new client.ApiError("500 boom", 500, path, "boom");
      return [] as never;
    });
    renderWithProviders(<Calls />, "/calls?preset=7d");
    expect(await screen.findByText("Calls unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: /Live updates/ })).not.toBeInTheDocument();
  });
});

describe("#12 guests are not shown edit controls", () => {
  const call = CALLS.find((entry) => entry.audio_state === "present") ?? CALLS[0];

  it("hides Star, Re-transcribe, Delete, Correct and the notes from a guest on a call", async () => {
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Star/ })).not.toBeInTheDocument();
    // Re-transcribe and Delete live in the call's Actions menu (batch 2, #19): open it.
    await userEvent.click(screen.getByRole("button", { name: "More actions for this call" }));
    expect(await screen.findByRole("button", { name: /Copy link/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Re-transcribe/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Correct/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Notes about this call")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Add a tag")).not.toBeInTheDocument();
  });

  it("shows them to someone signed in", async () => {
    await signIn();
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    expect(await screen.findByRole("button", { name: /Star/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Notes about this call")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "More actions for this call" }));
    expect(await screen.findByRole("button", { name: /Re-transcribe/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Delete/ })).toBeInTheDocument();
  });

  it("hides the talkgroup's Edit from a guest", async () => {
    const talkgroup = TALKGROUPS[0];
    renderWithProviders(<TalkgroupDetail />, `/talkgroups/${talkgroup?.id}`, "/talkgroups/:id");
    await screen.findByRole("heading", { name: talkgroup?.alias as string });
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument();
  });

  it("hides the Calls page's Feed button from a guest and shows it signed in", async () => {
    const guest = renderWithProviders(<Calls />, "/calls?preset=7d");
    await screen.findByRole("list", { name: "Calls" });
    expect(screen.queryByRole("button", { name: "Feed" })).not.toBeInTheDocument();
    guest.unmount();

    await signIn();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    expect(await screen.findByRole("button", { name: "Feed" })).toBeInTheDocument();
  });
});

describe("#10/#11 titles and headings", () => {
  it("titles a call by its channel and time, with the channel as the page's h1", async () => {
    const call = CALLS[0];
    renderWithProviders(<CallDetail />, `/calls/${call?.id}`, "/calls/:id");
    const heading = await screen.findByRole("heading", { level: 1 });
    const channel = heading.textContent ?? "";
    expect(channel).not.toBe("");
    await waitFor(() => expect(document.title.startsWith(`${channel} · `)).toBe(true));
    expect(document.title.endsWith(" · Keyup")).toBe(true);
  });

  it("gives sign-in one h1, a title, and the helper sentence once", async () => {
    renderWithProviders(<Login />, "/login");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(document.title).toBe("Sign in · Keyup");
    expect(screen.getAllByText(/signing in is only needed to change things/i)).toHaveLength(1);
  });
});

describe("#9/#32 the Live page", () => {
  it("fills the list from the archive on arrival rather than waiting for the stream", async () => {
    const get = spyGets();
    renderWithProviders(<Live />, "/live");
    await waitFor(() =>
      expect(
        get.mock.calls.some(
          ([path, params]) => path === "/calls" && (params as Record<string, unknown>)?.page_size === 50,
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(screen.queryByText("Listening…")).not.toBeInTheDocument());
  });

  it("seeds Who's active from the last ten minutes", async () => {
    const get = spyGets();
    const before = Date.now();
    renderWithProviders(<Live />, "/live");
    await waitFor(() => {
      const seed = get.mock.calls.find(
        ([path, params]) => path === "/calls" && (params as Record<string, unknown>)?.page_size === 500,
      );
      expect(seed).toBeDefined();
      const from = Date.parse(String((seed?.[1] as Record<string, unknown> | undefined)?.date_from));
      expect(before - from).toBeGreaterThanOrEqual(10 * 60_000 - 1000);
      expect(before - from).toBeLessThan(11 * 60_000);
    });
  });

  it("says Follow new calls, shows no second status pill, and labels the channel chips", async () => {
    renderWithProviders(<Live />, "/live");
    expect(await screen.findByRole("switch", { name: /Follow new calls/ })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /^Live$/ })).not.toBeInTheDocument();
    expect(screen.queryByText("Offline")).not.toBeInTheDocument();
    expect(await screen.findByRole("group", { name: "Busiest channels:" })).toBeInTheDocument();
  });
});

describe("#3 the header pill reports the real stream", () => {
  function Streaming() {
    useLiveFeed({}, true);
    return <p>streaming page</p>;
  }

  it("shows no pill — and never Offline — where nothing streams", async () => {
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/talkgroups");
    const banner = screen.getByRole("banner");
    await waitFor(() => expect(within(banner).queryByText("Offline")).not.toBeInTheDocument());
    expect(within(banner).queryByText("Simulated")).not.toBeInTheDocument();
  });

  it("shows the stream's own state while a page streams", async () => {
    renderWithProviders(
      <AppLayout>
        <Streaming />
      </AppLayout>,
      "/calls",
    );
    const banner = screen.getByRole("banner");
    // The test build runs on the mock feed, whose state is "Simulated".
    expect(await within(banner).findByText("Simulated")).toBeInTheDocument();
    expect(within(banner).queryByText("Offline")).not.toBeInTheDocument();
  });
});
