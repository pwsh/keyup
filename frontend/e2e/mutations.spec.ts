import { expect, test } from "@playwright/test";
import { api, apiPatch, expectClean, settle, watchForProblems } from "./helpers";

/**
 * The write half of the app, against the real API.
 *
 * These sign in and then edit real rows, so they only run when credentials are
 * supplied — `E2E_USERNAME` / `E2E_PASSWORD`, for an account made with
 * `keyup user create <name> --admin --password …`. Everything each test
 * creates, it deletes again; the two that edit an existing row (a call's notes,
 * a talkgroup's alias) put the previous value back, and fail if that did not work.
 *
 * The one thing a run leaves behind is a revoked API token: the API revokes rather
 * than deletes, because a token that once existed is an audit record. That is the
 * API behaving correctly, not the test failing to tidy up.
 */
const USERNAME = process.env.E2E_USERNAME;
const PASSWORD = process.env.E2E_PASSWORD;

test.describe("signed in", () => {
  test.skip(
    !USERNAME || !PASSWORD,
    "set E2E_USERNAME and E2E_PASSWORD to run the mutation suite against the real API",
  );

  test.beforeEach(async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Username", { exact: true }).fill(USERNAME as string);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD as string);
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    // The sidebar swaps the sign-in button for a sign-out one naming the account.
    await expect(page.getByRole("button", { name: `Sign out of ${USERNAME}` })).toBeVisible();
  });

  test("signing in turns the read-only controls back on", async ({ page }) => {
    const problems = watchForProblems(page);
    await page.goto("/calls?preset=7d");
    await settle(page);
    await expect(
      page.getByRole("button", { name: /^Star this call$|^Remove the star$/ }).first(),
    ).toBeEnabled();
    await page.goto("/admin");
    await settle(page);
    await expect(page.getByRole("button", { name: /rescan/i }).first()).toBeEnabled();
    expectClean(problems, "signed in");
  });

  test("notes, tags and the star survive a reload", async ({ page }) => {
    const problems = watchForProblems(page);
    const list = await api<{ items: { id: number; notes: string; tags: string[]; starred: boolean }[] }>(
      page,
      "/calls?page_size=1&sort=-started_at",
    );
    const call = list.items[0] as { id: number; notes: string; tags: string[]; starred: boolean };
    const note = `e2e note ${Date.now()}`;
    const tag = `e2e-${Date.now()}`;

    await page.goto(`/calls/${call.id}`);
    await settle(page);

    await page.getByLabel("Notes about this call", { exact: true }).fill(note);
    await page.getByRole("button", { name: "Save notes" }).click();
    await expect(page.getByText("Saved").first()).toBeVisible();

    await page.getByLabel("Add a tag", { exact: true }).fill(tag);
    await page.getByLabel("Add a tag", { exact: true }).press("Enter");

    const star = page.getByRole("button", { name: /^Star$/ });
    const wasStarred = (await star.getAttribute("aria-pressed")) === "true";
    await star.click();

    await page.reload();
    await settle(page);
    await expect(page.getByLabel("Notes about this call", { exact: true })).toHaveValue(note);
    await expect(page.getByText(tag, { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /^Star$/ })).toHaveAttribute(
      "aria-pressed",
      String(!wasStarred),
    );

    // The API really has it, not just the cache.
    const after = await api<{ notes: string; tags: string[]; starred: boolean }>(page, `/calls/${call.id}`);
    expect(after.notes).toBe(note);
    expect(after.tags).toContain(tag);
    expect(after.starred).toBe(!wasStarred);

    // Put the row back the way it was found — and fail if that did not work, so a
    // run cannot quietly leave its litter in a real archive.
    await apiPatch(page, `/calls/${call.id}`, {
      notes: call.notes,
      tags: call.tags,
      starred: call.starred,
    });
    const restored = await api<{ notes: string; tags: string[]; starred: boolean }>(
      page,
      `/calls/${call.id}`,
    );
    expect(restored).toMatchObject({ notes: call.notes, tags: call.tags, starred: call.starred });
    expectClean(problems, "call patch");
  });

  test("a talkgroup's alias can be edited and comes back changed", async ({ page }) => {
    const problems = watchForProblems(page);
    const list = await api<{ items: { id: number; alias: string | null; tgid: number }[] }>(
      page,
      "/talkgroups?sort=-call_count&page_size=1&include_hidden=false",
    );
    const talkgroup = list.items[0] as { id: number; alias: string | null; tgid: number };
    const original = talkgroup.alias ?? "";
    const renamed = `${original} E2E`;

    await page.goto("/talkgroups");
    await settle(page);
    await page.getByRole("button", { name: `Edit ${original || `TG ${talkgroup.tgid}`}` }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Alias", { exact: true }).fill(renamed);
    await dialog.getByRole("button", { name: "Save" }).click();

    // The alias is an inline cell now, so what carries the name on the row is the
    // cell's button; the link beside it opens the channel.
    await expect(page.getByRole("link", { name: `Open ${renamed}` })).toBeVisible();
    expect((await api<{ alias: string }>(page, `/talkgroups/${talkgroup.id}`)).alias).toBe(renamed);

    // Rename it back through the same dialog, so the undo is tested too.
    await page.getByRole("button", { name: `Edit ${renamed}` }).click();
    await page.getByRole("dialog").getByLabel("Alias", { exact: true }).fill(original);
    await page.getByRole("dialog").getByRole("button", { name: "Save" }).click();
    await expect
      .poll(async () => (await api<{ alias: string }>(page, `/talkgroups/${talkgroup.id}`)).alias)
      .toBe(original);
    expectClean(problems, "talkgroup patch");
  });

  test("a talkgroup can be favorited and unfavorited", async ({ page }) => {
    const problems = watchForProblems(page);
    const list = await api<{ items: { id: number; alias: string | null; tgid: number }[] }>(
      page,
      "/talkgroups?sort=-call_count&page_size=1&include_hidden=false",
    );
    const talkgroup = list.items[0] as { id: number; alias: string | null; tgid: number };
    const label = talkgroup.alias ?? `TG ${talkgroup.tgid}`;

    await page.goto("/talkgroups");
    await settle(page);
    const add = page.getByRole("button", { name: `Add ${label} to favorites` });
    await add.click();
    await expect(page.getByRole("button", { name: `Remove ${label} from favorites` })).toBeVisible();
    expect((await api<{ talkgroup_id: number }[]>(page, "/favorites")).map((f) => f.talkgroup_id)).toContain(
      talkgroup.id,
    );

    await page.getByRole("button", { name: `Remove ${label} from favorites` }).click();
    await expect(page.getByRole("button", { name: `Add ${label} to favorites` })).toBeVisible();
    expectClean(problems, "favorites");
  });

  test("a search can be saved and deleted again", async ({ page }) => {
    const problems = watchForProblems(page);
    const name = `e2e search ${Date.now()}`;
    await page.goto("/search?q=fire&preset=7d");
    await settle(page);

    await page.getByRole("button", { name: "Save this search" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name for this saved search").fill(name);
    await dialog.getByRole("button", { name: "Save" }).click();

    await expect
      .poll(async () => (await api<{ name: string }[]>(page, "/saved-searches")).map((s) => s.name))
      .toContain(name);

    // With no query, the search page lists the saved ones instead of results.
    await page.goto("/search");
    await settle(page);
    await expect(page.getByRole("link", { name })).toBeVisible();
    await page.getByRole("button", { name: `Delete the saved search ${name}` }).click();
    await expect
      .poll(async () => (await api<{ name: string }[]>(page, "/saved-searches")).map((s) => s.name))
      .not.toContain(name);
    expectClean(problems, "saved searches");
  });

  test("an API token is created once, shown once, and revoked", async ({ page }) => {
    const problems = watchForProblems(page);
    const label = `e2e token ${Date.now()}`;
    await page.goto("/admin");
    await settle(page);
    await page.getByRole("tab", { name: "Tokens" }).click();
    await page.getByRole("button", { name: "New token" }).click();

    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Label", { exact: true }).fill(label);
    await dialog.getByRole("button", { name: "Create" }).click();

    // The plaintext token appears exactly once, on create.
    await expect(dialog.getByText(/^kup_/)).toBeVisible();
    await dialog
      .getByRole("button", { name: /done|close/i })
      .first()
      .click();

    const row = page.getByRole("listitem").filter({ hasText: label });
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: `Revoke ${label}` }).click();
    await expect
      .poll(async () => {
        const tokens = await api<{ label: string; revoked_at: string | null }[]>(page, "/admin/tokens");
        return tokens.find((token) => token.label === label)?.revoked_at;
      })
      .not.toBeNull();
    expectClean(problems, "tokens");
  });

  test("an alert rule can be created and deleted", async ({ page }) => {
    const problems = watchForProblems(page);
    const name = `e2e alert ${Date.now()}`;
    await page.goto("/alerts");
    await settle(page);

    await page.getByRole("button", { name: "New rule" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name", { exact: true }).fill(name);
    await dialog.getByLabel("Pattern", { exact: true }).fill("structure fire");
    await dialog.getByRole("button", { name: "Save" }).click();

    await expect(page.getByText(name)).toBeVisible();
    const created = (await api<{ id: number; name: string }[]>(page, "/alerts")).find(
      (rule) => rule.name === name,
    );
    expect(created, "the alert rule was not persisted").toBeTruthy();

    await page.getByRole("button", { name: `Delete ${name}` }).click();
    await expect
      .poll(async () => (await api<{ name: string }[]>(page, "/alerts")).map((rule) => rule.name))
      .not.toContain(name);
    expectClean(problems, "alerts");
  });

  test("signing out puts the app back into read-only", async ({ page }) => {
    await page.goto("/calls");
    await settle(page);
    await page.getByRole("button", { name: `Sign out of ${USERNAME}` }).click();
    await expect(page.getByRole("button", { name: "Sign in" }).first()).toBeVisible();
  });
});
