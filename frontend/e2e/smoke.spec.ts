import { expect, test } from "@playwright/test";
import { expectClean, isGuest, ROUTES, settle, watchForProblems } from "./helpers";

/**
 * Every page, against the real archive: it renders, it says something specific to
 * this deployment's data, and it does it without a console error or a failed
 * request. Signed out, so mutations are expected to be refused (`allow401`).
 */
test.describe("every page loads against the real API", () => {
  for (const route of ROUTES) {
    test(`${route.name} renders`, async ({ page }) => {
      const problems = watchForProblems(page, { allow401: true });
      if (route.requiresAuth && (await isGuest(page))) {
        // The site list and the alert rules are the operator's working state: a guest is
        // sent to sign in, and the navigation does not offer the page at all.
        await page.goto(route.path);
        await expect(page).toHaveURL(/\/login(\?|$)/);
        await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
        await expect(page.getByRole("heading", { name: route.heading, level: 1 })).toHaveCount(0);
        await expect(
          page.getByRole("navigation").getByRole("link", { name: route.heading, exact: true }),
        ).toHaveCount(0);
        await settle(page);
        expectClean(problems, route.path);
        return;
      }
      await page.goto(route.path);
      await expect(page.getByRole("heading", { name: route.heading, level: 1 }).first()).toBeVisible();
      await settle(page);
      expectClean(problems, route.path);
    });
  }

  test("an unknown path renders the not-found state, not a blank page", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/no-such-page");
    await expect(page.getByText("Page not found")).toBeVisible();
    expectClean(problems, "/no-such-page");
  });

  for (const layout of [
    { name: "desktop", viewport: { width: 1600, height: 900 } },
    { name: "mobile", viewport: { width: 390, height: 844 } },
  ] as const) {
    test(`no page gives the window its own scrollbar on ${layout.name}`, async ({ page }) => {
      // The shell is a fixed-height frame whose panels scroll internally. A second,
      // outer scrollbar means something escaped a scroll region — an absolutely
      // positioned `sr-only` caption is the classic way — and the reader gets a
      // page that scrolls into nothing.
      await page.setViewportSize(layout.viewport);
      for (const route of ROUTES) {
        await page.goto(route.path);
        await settle(page, 800);
        const scrolled = await page.evaluate(() => {
          window.scrollTo(0, 90_000);
          const y = window.scrollY;
          window.scrollTo(0, 0);
          return y;
        });
        expect(scrolled, `${route.path} scrolls the window by ${scrolled}px`).toBe(0);
      }
    });
  }

  test("the header reports the archive's real ingest lag and status", async ({ page }) => {
    await page.goto("/calls");
    await settle(page);
    // The lag badge is a number plus a unit — never the em dash that means "no data".
    const banner = page.getByRole("banner");
    await expect(banner.getByText(/^\d+(\.\d+)?[smh]$/)).toBeVisible();
  });
});

test.describe("the shell", () => {
  test("navigates between pages without a reload", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/calls");
    await settle(page);
    for (const [label, heading] of [
      ["Talkgroups", "Talkgroups"],
      ["Radios", "Radios and units"],
      ["Statistics", "Statistics"],
      ["Help", "Help"],
    ] as const) {
      await page.getByRole("navigation").getByRole("link", { name: label, exact: true }).first().click();
      await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    }
    expectClean(problems, "navigation");
  });

  test("offers sign-in while signed out, and the sign-in page renders", async ({ page }) => {
    const problems = watchForProblems(page, { allow401: true });
    await page.goto("/calls");
    await page
      .getByRole("button", { name: /sign in/i })
      .first()
      .click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expect(page.getByLabel("Username")).toBeVisible();
    await settle(page);
    expectClean(problems, "/login");
  });
});
