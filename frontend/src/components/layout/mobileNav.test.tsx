import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { AppLayout } from "./AppLayout";
import { NAV_ITEMS } from "./nav";

/** The bar renders at every width; `md:hidden` is what hides it, and jsdom has no CSS. */
const bar = () => screen.getByTestId("mobile-nav");
/** Likewise the header's phone-only cluster: the sidebar is `hidden … md:flex`. */
const header = () => screen.getByRole("banner");

const auth = vi.hoisted(() => ({
  user: null as { id: number; username: string; is_admin: boolean } | null,
  logout: vi.fn(),
}));

vi.mock("../../api/hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/hooks")>()),
  useCurrentUser: () => ({
    user: auth.user,
    canWrite: auth.user !== null,
    isAdmin: auth.user?.is_admin === true,
    isLoading: false,
  }),
  useLogout: () => ({ mutate: auth.logout }),
}));

function signIn(username = "dispatch", is_admin = true) {
  auth.user = { id: 1, username, is_admin };
}

beforeEach(() => {
  auth.user = null;
  auth.logout.mockReset();
});

/**
 * The phone's bottom bar used to be `navItems.slice(0, 5)`, which meant Sites, Statistics,
 * Alerts, Admin, Activity and Help were reachable on a phone only by typing a URL — which
 * on a phone means they were not reachable at all.
 */
describe("the mobile navigation bar", () => {
  it("puts every page an anonymous visitor may see either on the bar or behind More", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");

    const visible = NAV_ITEMS.filter((item) => !item.requiresAuth && !item.requiresAdmin);
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));

    const reachable = new Set<string>();
    for (const link of screen.getAllByRole("link")) {
      const href = link.getAttribute("href");
      if (href) reachable.add(href);
    }
    for (const item of visible) {
      expect(reachable.has(item.to), item.to).toBe(true);
    }
  });

  it("hides the pages a signed-out visitor has no account for", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));
    // Activity is admin-only in the nav; the page itself stays reachable by URL.
    expect(screen.queryByRole("link", { name: "Activity" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  });

  it("marks More as current while one of the pages inside it is open", async () => {
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/help");
    // Otherwise the bar would claim nothing is selected on most of the site's pages.
    const more = within(bar()).getByRole("button", { name: "More pages" });
    expect(more.className).toContain("text-accent-ink");
  });

  it("leaves More unmarked while one of the four tabs is the current page", () => {
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    const more = within(bar()).getByRole("button", { name: "More pages" });
    expect(more.className).not.toContain("text-accent-ink");
  });

  it("keeps the bar to four tabs plus More", () => {
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    expect(within(bar()).getAllByRole("link")).toHaveLength(4);
    expect(within(bar()).getByRole("button", { name: "More pages" })).toBeInTheDocument();
  });
});

/**
 * The account button lives in the sidebar, which is `hidden … md:flex`. On a phone that
 * left no way at all to sign in — `/login` had to be typed — and so no way to reach the
 * admin pages, which are only listed once there is a session.
 */
describe("signing in from a phone", () => {
  it("offers the account button in the header a phone can see", () => {
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    expect(within(header()).getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("names the account in the header once there is a session", () => {
    signIn("dispatch");
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    expect(within(header()).getByRole("button", { name: "Sign out of dispatch" })).toBeInTheDocument();
  });

  it("offers Sign in at the foot of the More sheet", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));

    const sheet = screen.getByRole("dialog");
    expect(within(sheet).getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(within(sheet).queryByRole("button", { name: /Sign out/ })).not.toBeInTheDocument();
  });

  it("lists the admin pages and a sign-out row once an admin is signed in", async () => {
    signIn("dispatch");
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));

    const sheet = screen.getByRole("dialog");
    expect(within(sheet).getByRole("link", { name: "Admin" })).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Activity" })).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Feed stats" })).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Sign out dispatch" })).toBeInTheDocument();
  });

  it("signs out from the More sheet", async () => {
    signIn("dispatch");
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));
    await user.click(screen.getByRole("button", { name: "Sign out dispatch" }));
    expect(auth.logout).toHaveBeenCalled();
  });
});

/**
 * Four pages are the operator's own working state rather than the archive: the unit and
 * personnel rosters (names, ranks, badge numbers that were never on the air), the site
 * list, and the alert rules. A guest is not shown them anywhere — and `App` guards the
 * routes, so this is a listing rule, not the only lock.
 */
describe("the pages a guest is not shown", () => {
  const OPERATOR_ONLY = ["/units", "/personnel", "/sites", "/alerts"];

  it("keeps the roster, the sites and the alerts out of a signed-out nav", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));

    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    for (const path of OPERATOR_ONLY) {
      expect(hrefs, path).not.toContain(path);
    }
    // What a guest *is* offered stays offered.
    for (const path of ["/calls", "/live", "/search", "/talkgroups", "/radios", "/stats", "/help"]) {
      expect(hrefs, path).toContain(path);
    }
  });

  it("lists all four again once somebody signs in", async () => {
    signIn("dispatch");
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));

    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    for (const path of OPERATOR_ONLY) {
      expect(hrefs, path).toContain(path);
    }
  });
});

/**
 * `/embed/transcripts` is nginx's page, not a route of this app: a router link would land
 * on the SPA's not-found page instead of loading it.
 */
describe("a nav item outside the app", () => {
  it("is a plain link that opens in a new tab, in the sidebar and behind More", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppLayout>{null}</AppLayout>, "/calls");
    await user.click(within(bar()).getByRole("button", { name: "More pages" }));

    const links = screen
      .getAllByRole("link")
      .filter((link) => link.getAttribute("href") === "/embed/transcripts");
    expect(links.length).toBeGreaterThanOrEqual(2);
    for (const link of links) {
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")).toContain("noopener");
    }
  });
});
