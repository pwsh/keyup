import { act, render, screen, waitFor } from "@testing-library/react";
import { Link, MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePlayerStore } from "../state/player";

const currentUser = vi.hoisted(() => ({ user: null as { id: number } | null, isLoading: false }));

vi.mock("../api/hooks", async (original) => ({
  ...(await original<typeof import("../api/hooks")>()),
  useCurrentUser: () => currentUser,
}));

const GTM = "GTM-ABC1234";

function setHost(hostname: string) {
  vi.stubGlobal("location", { ...window.location, hostname });
}

async function mount(route: string) {
  const { usePublicSite } = await import("./publicSite");
  function Harness() {
    usePublicSite();
    return <Link to="/radios/77?q=Jane+Doe">go</Link>;
  }
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Harness />
    </MemoryRouter>,
  );
}

function pushed(): Record<string, unknown>[] {
  return (window.dataLayer ?? []) as Record<string, unknown>[];
}

/** Google's queue only honours real `arguments` objects; an array is silently ignored. */
function isCommand(entry: unknown): boolean {
  return Object.prototype.toString.call(entry) === "[object Arguments]";
}

function commands(): unknown[][] {
  return pushed()
    .filter(isCommand)
    .map((entry) => Array.from(entry as unknown as ArrayLike<unknown>));
}

function events(name: string): unknown[][] {
  return commands().filter((command) => command[0] === "event" && command[1] === name);
}

function pageViews() {
  return events("page_view").map((command) => command[2] as Record<string, unknown>);
}

function pageSettings() {
  return commands()
    .filter((command) => command[0] === "set")
    .map((command) => command[1] as Record<string, unknown>);
}

function gtmScripts() {
  return document.head.querySelectorAll('script[src*="googletagmanager.com/gtm.js"]');
}

beforeEach(() => {
  currentUser.user = null;
  currentUser.isLoading = false;
  window.dataLayer = undefined;
  vi.stubEnv("VITE_PUBLIC_HOST", "archive.example.org");
  setHost("archive.example.org");
  vi.stubEnv("VITE_GTM_CONTAINER_ID", GTM);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  for (const script of gtmScripts()) script.remove();
  document.head.querySelector('link[rel="canonical"]')?.remove();
});

describe("analytics", () => {
  it("loads the container once for a guest on the public host", async () => {
    await mount("/calls");
    await waitFor(() => expect(gtmScripts()).toHaveLength(1));
    expect((gtmScripts()[0] as HTMLScriptElement).src).toContain(`id=${GTM}`);
    expect(pushed().some((entry) => entry.event === "gtm.js")).toBe(true);
  });

  it("reports a call page by its template, never its id", async () => {
    await mount("/calls/6419663");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    expect(pageViews()[0]?.page_path).toBe("/calls/:id");
    expect(pageViews()[0]?.page_location).toBe("https://archive.example.org/calls/:id");
  });

  it("never sends a query string or the tab title", async () => {
    await mount("/search?q=Jane+Doe");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    expect(JSON.stringify(pageViews())).not.toContain("Jane");
    expect(pageViews()[0]?.page_title).toBe("/search");
  });

  it("tells Google the clean page before the container can read the real one", async () => {
    await mount("/calls/6419663?q=Jane+Doe");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    const firstSetting = pushed().findIndex(isCommand);
    const containerStart = pushed().findIndex((entry) => entry.event === "gtm.js");
    expect(firstSetting).toBeGreaterThanOrEqual(0);
    expect(firstSetting).toBeLessThan(containerStart);
    expect(pageSettings()[0]).toMatchObject({
      page_location: "https://archive.example.org/calls/:id",
      page_title: "/calls/:id",
      send_page_view: false,
    });
  });

  it("moves the clean page along on a navigation", async () => {
    await mount("/calls");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    act(() => screen.getByText("go").click());
    await waitFor(() => expect(pageViews()).toHaveLength(2));
    expect(pageSettings().at(-1)?.page_location).toBe("https://archive.example.org/radios/:id");
    expect(JSON.stringify(commands())).not.toContain("Jane");
  });

  it("drops a referrer from this site, which can hold a search", async () => {
    vi.spyOn(document, "referrer", "get").mockReturnValue("https://archive.example.org/search?q=Jane+Doe");
    await mount("/calls");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    expect(pageSettings()[0]?.page_referrer).toBe("");
  });

  it("keeps a referrer from another site", async () => {
    vi.spyOn(document, "referrer", "get").mockReturnValue("https://www.example.net/");
    await mount("/calls");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    expect(pageSettings()[0]?.page_referrer).toBe("https://www.example.net/");
  });

  it("reports a navigation", async () => {
    await mount("/calls");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    act(() => screen.getByText("go").click());
    await waitFor(() => expect(pageViews()).toHaveLength(2));
    expect(pageViews()[1]?.page_path).toBe("/radios/:id");
  });

  it("stays off for a signed-in reader", async () => {
    currentUser.user = { id: 1 };
    await mount("/calls");
    expect(gtmScripts()).toHaveLength(0);
    expect(pageViews()).toHaveLength(0);
  });

  it("waits until it knows who is reading", async () => {
    currentUser.isLoading = true;
    await mount("/calls");
    expect(gtmScripts()).toHaveLength(0);
  });

  it("stays off on any other host", async () => {
    setHost("other.example.org");
    await mount("/calls");
    expect(gtmScripts()).toHaveLength(0);
  });

  it("stays off everywhere when no public host is configured", async () => {
    vi.stubEnv("VITE_PUBLIC_HOST", "");
    setHost("localhost");
    await mount("/calls");
    expect(gtmScripts()).toHaveLength(0);
    expect(document.head.querySelector('link[rel="canonical"]')).toBeNull();
  });

  it("stays off without a valid container id", async () => {
    vi.stubEnv("VITE_GTM_CONTAINER_ID", 'GTM-1"><script>');
    await mount("/calls");
    expect(gtmScripts()).toHaveLength(0);
  });

  it("counts a guest playing a call, without saying which", async () => {
    await mount("/calls");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    act(() => usePlayerStore.setState({ current: { id: 42 } as never }));
    const plays = events("call_play");
    expect(plays).toHaveLength(1);
    expect(JSON.stringify(plays)).not.toContain("42");
    act(() => usePlayerStore.setState({ current: null }));
  });

  it("counts a guest starting the live feed", async () => {
    await mount("/live");
    await waitFor(() => expect(pageViews()).toHaveLength(1));
    act(() => usePlayerStore.setState({ live: true }));
    expect(events("live_start")).toHaveLength(1);
    act(() => usePlayerStore.setState({ live: false }));
  });
});

describe("canonical link", () => {
  it("names the page on the public origin, without its query", async () => {
    await mount("/talkgroups?agency=X");
    await waitFor(() =>
      expect(document.head.querySelector('link[rel="canonical"]')?.getAttribute("href")).toBe(
        "https://archive.example.org/talkgroups",
      ),
    );
  });
});
