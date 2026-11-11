import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../api/client";
import { renderWithProviders } from "../test/render";
import { Feeds } from "./Feeds";

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

/** The card for one built-in feed, found by its name. */
const cardFor = async (name: string) => (await screen.findByText(name)).closest("li") as HTMLElement;

describe("the Feeds page", () => {
  it("describes every built-in feed: what it contains, how busy it is and how it publishes", async () => {
    renderWithProviders(<Feeds />);

    const fire = await cardFor("Keyup · Fire");
    expect(
      within(fire).getByText("Fire dispatch, fireground and department talk channels."),
    ).toBeInTheDocument();
    expect(within(fire).getByText("4 talkgroups")).toBeInTheDocument();
    expect(within(fire).getByText("96 calls in the last 7 days")).toBeInTheDocument();
    expect(within(fire).getByText(/Busiest: EC EVENT 6 \(62\), EC FIRE DISPATCH \(34\)/)).toBeInTheDocument();
    expect(within(fire).getByText(/Newest first, up to 50 items/)).toBeInTheDocument();
    expect(within(fire).getByText(/transcript is final and its audio is processed/)).toBeInTheDocument();
    expect(
      within(fire).getByText(/Audio is included while the archive is open to guests/),
    ).toBeInTheDocument();

    // ...and the three ways to take the feed away with you.
    expect(within(fire).getByRole("button", { name: "Copy the RSS URL for Keyup · Fire" })).toBeEnabled();
    expect(within(fire).getByRole("button", { name: "Copy the Atom URL for Keyup · Fire" })).toBeEnabled();
    const preview = within(fire).getByRole("link", { name: /Preview the Keyup · Fire feed/ });
    expect(preview).toHaveAttribute("href", "http://localhost/api/feeds/preset/fire.rss");
    expect(preview).toHaveAttribute("target", "_blank");
  });

  it("says plainly that `all` is the whole archive", async () => {
    renderWithProviders(<Feeds />);
    const everything = await cardFor("Keyup · All calls");
    expect(within(everything).getByText(/Every call in the archive — all 12 talkgroups/)).toBeInTheDocument();
  });

  it("copies a built-in URL that carries no token", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Feeds />);
    const everything = await cardFor("Keyup · All calls");
    await user.click(within(everything).getByRole("button", { name: /Copy the RSS URL/ }));
    await waitFor(async () =>
      expect(await navigator.clipboard.readText()).toBe("http://localhost/api/feeds/preset/all.rss"),
    );
  });

  it("tells a guest that publishing one needs an account, and offers no table", async () => {
    renderWithProviders(<Feeds />);

    expect(await screen.findByText(/Saving a search as a feed needs an account/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });

  it("gives a signed-in reader the management table, with each feed's filters as its description", async () => {
    await signIn();
    await apiPost("/feeds", { name: "Working fires", query: { q: "structure fire" } });
    renderWithProviders(<Feeds />);

    const row = (await screen.findByText("Working fires")).closest("li") as HTMLElement;
    expect(within(row).getByText(/matching "structure fire"/)).toBeInTheDocument();
    expect(within(row).getByText(/Newest first, up to 50 items/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /Rotate the token/ })).toBeEnabled();
    expect(screen.queryByText(/Saving a search as a feed needs an account/)).not.toBeInTheDocument();
  });

  it("builds an embed snippet for the live transcripts, Emergency dispatch by default", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Feeds />);

    const snippet = (await screen.findByLabelText("Snippet")) as HTMLTextAreaElement;
    expect(snippet.value).toBe(
      '<iframe src="http://localhost/embed/transcripts" title="Live radio transcripts" width="100%" height="480" style="border:0" loading="lazy"></iframe>',
    );
    expect(screen.getByRole("link", { name: "Preview the embed in a new tab" })).toHaveAttribute(
      "href",
      "http://localhost/embed/transcripts",
    );

    await user.type(screen.getByLabelText(/Talkgroups/), "1101");
    await user.click(screen.getByRole("switch", { name: "Filter bar" }));
    expect(snippet.value).toContain(
      'src="http://localhost/embed/transcripts?preset=emergency&amp;talkgroup=1101&amp;controls=0"',
    );

    await user.click(screen.getByRole("button", { name: "Copy the embed snippet" }));
    await waitFor(async () => expect(await navigator.clipboard.readText()).toBe(snippet.value));
  });

  it("explains the rules both kinds of feed follow, in the documentation's words", async () => {
    renderWithProviders(<Feeds />);
    expect(await screen.findByText("How feeds work")).toBeInTheDocument();
    expect(screen.getByText(/the URL is the password/i)).toBeInTheDocument();
    expect(screen.getByText(/Built-in feeds hold no secret/)).toBeInTheDocument();
    expect(screen.getByText(/cached for 60 seconds/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "The help page" })).toHaveAttribute("href", "/help");
  });
});
