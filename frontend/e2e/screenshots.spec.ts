import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { ROUTES, settle } from "./helpers";

/**
 * A full-page screenshot of every route in both themes at both widths, into
 * `e2e/screenshots/`. They are git-ignored — a screenshot of a live archive is
 * not a fixture — but `e2e/screenshots/README.md` lists what should be there, so
 * a missing one is obvious.
 *
 *     npm run e2e:shots
 *
 * The theme comes from the context's `colorScheme`, which is what `index.html`
 * reads before the app boots, so this captures the real default rather than a
 * toggle the page happened to be left on.
 */
const OUT = path.join(process.cwd(), "e2e", "screenshots");

const LAYOUTS = [
  { name: "desktop", viewport: { width: 1440, height: 900 } },
  // 390 × 844: an iPhone 14, and the width the filter rail becomes a sheet at.
  { name: "mobile", viewport: { width: 390, height: 844 } },
] as const;

const THEMES = ["light", "dark"] as const;

/** Routes plus the two detail pages, which only exist for a real id. */
async function shotList(baseURL: string) {
  const calls = await fetch(`${baseURL}/api/calls?transcript_status=done&page_size=1&sort=-duration_s`);
  const call = ((await calls.json()) as { items: { id: number }[] }).items[0];
  const talkgroups = await fetch(`${baseURL}/api/talkgroups?sort=-call_count&page_size=1`);
  const talkgroup = ((await talkgroups.json()) as { items: { id: number }[] }).items[0];
  const radios = await fetch(`${baseURL}/api/radios?sort=-call_count&page_size=1`);
  const radio = ((await radios.json()) as { items: { id: number }[] }).items[0];

  return [
    ...ROUTES.map((route) => ({ name: route.name, path: route.path })),
    { name: "login", path: "/login" },
    { name: "call-detail", path: `/calls/${call?.id}` },
    { name: "talkgroup-detail", path: `/talkgroups/${talkgroup?.id}` },
    { name: "radio-detail", path: `/radios/${radio?.id}` },
  ];
}

test.describe("screenshots", () => {
  test("captures every page, light and dark, desktop and mobile", async ({ browser, baseURL }) => {
    // Thirteen pages × two themes × two widths, each with its own page load.
    test.setTimeout(15 * 60_000);
    await mkdir(OUT, { recursive: true });
    const pages = await shotList(baseURL as string);
    const taken: string[] = [];

    for (const layout of LAYOUTS) {
      for (const theme of THEMES) {
        const context = await browser.newContext({
          viewport: layout.viewport,
          colorScheme: theme,
          // Deterministic images: no spinner caught mid-turn, no chart mid-tween.
          reducedMotion: "reduce",
        });
        const page = await context.newPage();
        for (const target of pages) {
          await page.goto(target.path);
          await settle(page, 1500);
          const file = `${target.name}-${theme}-${layout.name}.png`;
          await page.screenshot({ path: path.join(OUT, file), fullPage: true });
          taken.push(file);
        }
        await context.close();
      }
    }

    expect(taken).toHaveLength(pages.length * LAYOUTS.length * THEMES.length);
    await writeFile(
      path.join(OUT, "README.md"),
      [
        "# Screenshots",
        "",
        "Written by `npm run e2e:shots` (`e2e/screenshots.spec.ts`) from a dev server",
        "running against a real archive, and git-ignored: they are pictures of live",
        "radio traffic, not fixtures. This file lists what a complete run produces.",
        "",
        `Captured ${new Date().toISOString().slice(0, 10)} — ${taken.length} images,`,
        `${pages.length} pages × ${THEMES.length} themes × ${LAYOUTS.length} widths`,
        `(desktop 1440×900, mobile ${LAYOUTS[1].viewport.width}×${LAYOUTS[1].viewport.height}).`,
        "",
        ...taken.sort().map((file) => `- \`${file}\``),
        "",
      ].join("\n"),
      "utf8",
    );
  });

  test("the mobile filter sheet opens and filters", async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    await page.goto("/calls?preset=7d");
    await settle(page);

    // Below `lg` the rail is a sheet behind this button, and the table is cards.
    await page.getByRole("button", { name: /^Filters/ }).click();
    const sheet = page.getByRole("dialog", { name: "Filters" });
    await expect(sheet).toBeVisible();
    await sheet.getByRole("switch", { name: "Starred only" }).click();
    await expect(page).toHaveURL(/starred=true/);
    await context.close();
  });
});
