import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../../api/client";
import { renderWithProviders } from "../../test/render";
import { FeedDialog, suggestedName } from "./FeedDialog";
import { FeedsPanel } from "./FeedsPanel";

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

/** The current filters, as the Calls page hands them over. */
const FILTERS = { talkgroup: [1101], sort: "-started_at" } as never;

describe("the Feed dialog", () => {
  it("is disabled while nobody is signed in", () => {
    renderWithProviders(<FeedDialog filters={FILTERS} canWrite={false} />);
    expect(screen.getByRole("button", { name: /Feed/ })).toBeDisabled();
  });

  it("prefills a name from the filters rather than making one up", () => {
    expect(suggestedName({ talkgroup: [1101, 1001] } as never)).toBe("Talkgroups 1101, 1001");
    expect(suggestedName({ q: "structure fire" } as never)).toBe("Search: structure fire");
    expect(suggestedName({ alert: true } as never)).toBe("Pages");
    expect(suggestedName({} as never)).toBe("All calls");
  });

  it("creates the feed from the current filters and shows both URLs behind copy buttons", async () => {
    const user = userEvent.setup();
    await signIn();
    renderWithProviders(<FeedDialog filters={FILTERS} canWrite={true} />);

    await user.click(screen.getByRole("button", { name: /Feed/ }));
    const name = screen.getByRole("textbox") as HTMLInputElement;
    expect(name.value).toBe("Talkgroups 1101");
    await user.clear(name);
    await user.type(name, "Event 6");
    await user.click(screen.getByRole("button", { name: "Create feed" }));

    // The URL is the credential, so it is shown with the warning that says so — and it is
    // text behind a copy button, never a link somebody can middle-click into a chat.
    expect(await screen.findByText(/The URL is the password/)).toBeInTheDocument();
    const rss = await screen.findByTestId("feed-url-rss");
    expect(rss.textContent).toMatch(/\/api\/feeds\/.+\.rss$/);
    expect(screen.getByTestId("feed-url-atom").textContent).toMatch(/\.atom$/);
    expect(screen.queryByRole("link", { name: /feeds/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy the RSS URL" })).toBeInTheDocument();
    expect(screen.getByText(/talkgroups 1101/)).toBeInTheDocument();
  });

  it("carries the audio toggle through to the created feed", async () => {
    const user = userEvent.setup();
    await signIn();
    renderWithProviders(<FeedDialog filters={FILTERS} canWrite={true} />);

    await user.click(screen.getByRole("button", { name: /Feed/ }));
    await user.click(screen.getByRole("switch", { name: /Include audio/ }));
    await user.click(screen.getByRole("button", { name: "Create feed" }));
    await screen.findByText(/The URL is the password/);

    const feeds = await apiGet<{ name: string; include_audio: boolean; query: unknown }[]>("/feeds");
    const created = feeds.find((feed) => feed.name === "Talkgroups 1101");
    expect(created?.include_audio).toBe(true);
    // ...and the feed publishes the filters the page was showing, not an empty set.
    expect(created?.query).toMatchObject({ talkgroup: [1101] });
  });
});

describe("the Search landing card", () => {
  it("points at the Feeds page instead of listing sixteen built-in feeds", async () => {
    renderWithProviders(<FeedsPanel canWrite={false} />);

    const link = await screen.findByRole("link", { name: "see the Feeds page" });
    expect(link).toHaveAttribute("href", "/feeds");
    // The built-in rows themselves live on that page now.
    expect(screen.queryByText("Keyup \u00b7 Fire")).not.toBeInTheDocument();
    expect(screen.queryByText("Built-in feeds")).not.toBeInTheDocument();
  });

  it("keeps the feeds somebody published, under their own label", async () => {
    await signIn();
    await apiPost("/feeds", { name: "Working fires", query: { q: "structure fire" } });
    renderWithProviders(<FeedsPanel canWrite={true} />);

    expect(await screen.findByText("Your feeds")).toBeInTheDocument();
    expect(screen.getByText("Working fires")).toBeInTheDocument();
  });
});

describe("the Feeds table", () => {
  it("lists a feed with its summary and both copy buttons", async () => {
    await signIn();
    await apiPost("/feeds", { name: "Working fires", query: { q: "structure fire" } });
    renderWithProviders(<FeedsPanel canWrite={true} />);

    const row = (await screen.findByText("Working fires")).closest("li") as HTMLElement;
    expect(within(row).getByText(/matching "structure fire"/)).toBeInTheDocument();
    expect(within(row).getByText(/Never fetched/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /Copy the RSS URL/ })).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /Copy the Atom URL/ })).toBeInTheDocument();
  });

  it("rotates a token into a new URL", async () => {
    const user = userEvent.setup();
    await signIn();
    const created = (await apiPost("/feeds", { name: "Rotate me", query: {} })) as {
      rss_url: string;
    };
    renderWithProviders(<FeedsPanel canWrite={true} />);

    const row = (await screen.findByText("Rotate me")).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /Rotate the token/ }));
    // The row keeps its name; what changed is the secret behind the copy button.
    await waitFor(async () => {
      const feeds = await apiGet<{ name: string; rss_url: string }[]>("/feeds");
      const after = feeds.find((feed) => feed.name === "Rotate me");
      expect(after?.rss_url).not.toBe(created.rss_url);
    });
  });

  it("turns a feed off and deletes it", async () => {
    const user = userEvent.setup();
    await signIn();
    await apiPost("/feeds", { name: "Temporary", query: {} });
    renderWithProviders(<FeedsPanel canWrite={true} />);

    const row = (await screen.findByText("Temporary")).closest("li") as HTMLElement;
    const enabled = within(row).getByRole("switch", { name: /On/ });
    expect(enabled).toBeChecked();
    await user.click(enabled);
    await waitFor(() => expect(within(row).getByRole("switch", { name: /On/ })).not.toBeChecked());

    await user.click(within(row).getByRole("button", { name: /Delete the feed/ }));
    await waitFor(() => expect(screen.queryByText("Temporary")).not.toBeInTheDocument());
  });

  it("leaves every control dead while nobody is signed in", async () => {
    await signIn();
    await apiPost("/feeds", { name: "Read only", query: {} });
    renderWithProviders(<FeedsPanel canWrite={false} />);
    const row = (await screen.findByText("Read only")).closest("li") as HTMLElement;
    expect(within(row).getByRole("button", { name: /Rotate the token/ })).toBeDisabled();
    expect(within(row).getByRole("button", { name: /Delete the feed/ })).toBeDisabled();
    // ...but copying the URL is a read, and stays available.
    expect(within(row).getByRole("button", { name: /Copy the RSS URL/ })).toBeEnabled();
  });
});
