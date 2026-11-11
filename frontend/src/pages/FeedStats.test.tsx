import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../api/client";
import { useToastStore } from "../state/toasts";
import { renderWithProviders } from "../test/render";
import { FeedStats } from "./FeedStats";

async function signInAsAdmin(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

/**
 * The Feed stats page against the mock API.
 *
 * The first test is the one that matters most: this page shows the operator's own
 * Broadcastify account data, and a reader who is not an admin must get the refusal
 * rather than a page full of empty tiles and a console of 403s.
 */
describe("Feed stats", () => {
  it("refuses a caller who is not an admin", async () => {
    await apiPost("/auth/logout");
    renderWithProviders(<FeedStats />, "/feed-stats");
    expect(await screen.findByText("Admin only")).toBeInTheDocument();
  });

  it("shows the status, both heatmaps, the hot hours and the archives", async () => {
    await signInAsAdmin();
    renderWithProviders(<FeedStats />, "/feed-stats");

    expect(await screen.findByRole("heading", { name: "Feed stats" })).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("table", { name: "Average listeners by weekday and hour" }),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("table", { name: "Average calls by weekday and hour" })).toBeInTheDocument();
    expect(screen.getByText("Hottest hours")).toBeInTheDocument();
    // One download link per archived block the sync has stored.
    expect((await screen.findAllByRole("link", { name: /Download/ })).length).toBeGreaterThan(1);
  });

  it("offers the credential fields, and never shows the password back", async () => {
    await signInAsAdmin();
    renderWithProviders(<FeedStats />, "/feed-stats");

    const password = await screen.findByLabelText(/Broadcastify password/);
    // `secret` kind: the API returns "" however it is set, so the field must be
    // empty here — a page that displayed a stored credential would be the leak.
    expect(password).toHaveValue("");
    expect(screen.getByLabelText(/Feed id/)).toBeInTheDocument();
  });

  it("draws the imported daily history and says where both histories start", async () => {
    await signInAsAdmin();
    renderWithProviders(<FeedStats />, "/feed-stats");

    expect(
      await screen.findByRole("heading", { name: "Daily peak listeners and uptime" }),
    ).toBeInTheDocument();

    // The coverage line carries three facts that are easy to conflate, so all three are
    // asserted: where the hourly history starts, how much of it was not polled here, and
    // that the daily history starts somewhere else entirely.
    const coverage = await screen.findByText(/hourly history from/);
    expect(coverage).toHaveTextContent(/h imported/);
    expect(coverage).toHaveTextContent(/from the public page/);
    expect(coverage).toHaveTextContent(/daily history from/);
  });

  it("imports a dashboard export and reports what it stored", async () => {
    await signInAsAdmin();
    renderWithProviders(<FeedStats />, "/feed-stats");

    // The control is a Button over a hidden input, so the button is what a person sees…
    expect(await screen.findByRole("button", { name: "Import dashboard export" })).toBeEnabled();
    // …and the input is what the file goes to.
    const input = screen.getByLabelText("Dashboard CSV export") as HTMLInputElement;
    expect(input.accept).toContain(".csv");

    const csv = new File(
      ["Day,Uptime %,Max Listeners\n2026-09-03 00:00:00,99.72,31\n2026-09-02 00:00:00,100.00,24\n"],
      "owner_uptime.csv",
      { type: "text/csv" },
    );
    await userEvent.upload(input, csv);

    // The toast is the whole answer: how many rows landed, and how far back they reach.
    // Asserted through the store rather than the DOM, because the `<Toaster>` lives in the
    // app shell and these tests render the page alone.
    await waitFor(() =>
      expect(useToastStore.getState().toasts.at(-1)).toMatchObject({
        tone: "success",
        title: expect.stringContaining("2 of 2 days stored"),
      }),
    );
    // …and the input is cleared, so re-choosing the same file after a fresh export fires
    // `change` again rather than doing nothing.
    expect(input.value).toBe("");
  });

  it("says so, rather than storing anything, when the file is not an export", async () => {
    await signInAsAdmin();
    renderWithProviders(<FeedStats />, "/feed-stats");

    const input = (await screen.findByLabelText("Dashboard CSV export")) as HTMLInputElement;
    await userEvent.upload(input, new File(["a,b\n1,2\n"], "notes.csv", { type: "text/csv" }));

    await waitFor(() => {
      const last = useToastStore.getState().toasts.at(-1);
      expect(last?.tone).toBe("error");
      expect(last?.title).toContain("Could not import that file");
      // The API's own sentence is what says *why*, and it is not thrown away.
      expect(last?.body).toContain("dashboard export");
    });
  });
});
