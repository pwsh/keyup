import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { usePlayerStore } from "../state/player";
import { renderWithProviders } from "../test/render";
import { Calls } from "./Calls";

describe("the Calls page", () => {
  it("loads the mock corpus and says how many calls matched", async () => {
    renderWithProviders(<Calls />, "/calls");
    await waitFor(() => expect(screen.getByText(/^\s*calls$/)).toBeInTheDocument());
    const list = await screen.findByRole("list", { name: "Calls" });
    expect(within(list).getAllByRole("listitem").length).toBeGreaterThan(0);
  });

  it("shows the filter rail with the text query and the date presets", async () => {
    renderWithProviders(<Calls />, "/calls");
    expect(await screen.findByLabelText("Search transcripts, notes and aliases")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Last 24h/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Last 7 days/ })).toBeInTheDocument();
  });

  it("starts from the filters in the URL", async () => {
    renderWithProviders(<Calls />, "/calls?q=engine&preset=7d");
    const search = (await screen.findByLabelText(
      "Search transcripts, notes and aliases",
    )) as HTMLInputElement;
    expect(search.value).toBe("engine");
    expect(screen.getByRole("button", { name: /Last 7 days/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("narrows the results when a filter is applied", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    const count = () => Number(/(\d+)/.exec(screen.getByText(/^\s*calls$/).textContent ?? "")?.[1] ?? 0);
    await waitFor(() => expect(count()).toBeGreaterThan(0));
    const before = count();
    await user.click(screen.getByRole("switch", { name: /Starred only/ }));
    await waitFor(() => expect(count()).toBeLessThan(before));
  });

  it("loads a selected call into the player", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Calls />, "/calls?preset=7d");
    const list = await screen.findByRole("list", { name: "Calls" });
    const [firstRow] = within(list).getAllByRole("button");
    await user.click(firstRow as HTMLElement);
    expect(usePlayerStore.getState().current).not.toBeNull();
    expect(usePlayerStore.getState().queue.length).toBeGreaterThan(0);
  });

  it("offers no star while nobody is signed in", async () => {
    renderWithProviders(<Calls />, "/calls?preset=7d");
    const list = await screen.findByRole("list", { name: "Calls" });
    // The rows are there, with their other actions; the edit-only star is not.
    expect(within(list).getAllByRole("button", { name: /^Play the call on/ }).length).toBeGreaterThan(0);
    expect(within(list).queryAllByRole("button", { name: /star this call|remove the star/i })).toHaveLength(
      0,
    );
  });

  it("offers the exports", async () => {
    renderWithProviders(<Calls />, "/calls");
    expect(await screen.findByRole("button", { name: /Export/ })).toBeInTheDocument();
  });

  describe("the new toolbar", () => {
    afterEach(() => {
      // These preferences live in localStorage; clear them so one case cannot leak
      // its columns or transcript-row choice into the next.
      localStorage.clear();
    });

    it("hides the metadata-only calls by default, and can widen to the whole archive", async () => {
      const user = userEvent.setup();
      renderWithProviders(<Calls />, "/calls?preset=7d");
      const toggle = await screen.findByRole("switch", { name: /Audio only/ });
      expect(toggle).toBeChecked();
      const count = () => Number(/(\d+)/.exec(screen.getByText(/^\s*calls$/).textContent ?? "")?.[1] ?? 0);
      await waitFor(() => expect(count()).toBeGreaterThan(0));
      const audioOnly = count();
      await user.click(toggle);
      await waitFor(() => expect(toggle).not.toBeChecked());
      // Turning it off can only add the rows it was holding back, never remove any.
      await waitFor(() => expect(count()).toBeGreaterThanOrEqual(audioOnly));
    });

    it("offers a quality filter, a column picker and a transcript-row toggle", async () => {
      renderWithProviders(<Calls />, "/calls?preset=7d");
      await screen.findByRole("list", { name: "Calls" });
      expect(screen.getByRole("combobox", { name: /transcript quality/i })).toBeInTheDocument();
      // Both live in the one View menu.
      await userEvent.setup().click(screen.getByRole("button", { name: /View/ }));
      expect(await screen.findByRole("checkbox", { name: "Frequency" })).toBeInTheDocument();
      expect(screen.getByRole("switch", { name: /Transcript row/ })).toBeInTheDocument();
    });

    it("shows a column once it is picked", async () => {
      const user = userEvent.setup();
      renderWithProviders(<Calls />, "/calls?preset=7d");
      await screen.findByRole("list", { name: "Calls" });
      await user.click(screen.getByRole("button", { name: /View/ }));
      const frequency = await screen.findByRole("checkbox", { name: "Frequency" });
      expect(frequency).not.toBeChecked();
      await user.click(frequency);
      expect(frequency).toBeChecked();
      // The "Freq" header only exists once the column is on.
      expect(await screen.findByText("Freq")).toBeInTheDocument();
    });

    it("moves the transcript between its own row and a column when asked", async () => {
      const user = userEvent.setup();
      renderWithProviders(<Calls />, "/calls?preset=7d");
      await screen.findByRole("list", { name: "Calls" });
      // The test viewport is 1440 px, where the transcript starts on its own row: the
      // "Transcript" column header is absent, leaving only the rail's section title.
      await user.click(screen.getByRole("button", { name: /View/ }));
      const toggle = await screen.findByRole("switch", { name: /Transcript row/ });
      expect(toggle).toBeChecked();
      // Counted with the menu open, whose column picker has a "Transcript" box too.
      const before = screen.getAllByText("Transcript").length;
      await user.click(toggle);
      await waitFor(() => expect(screen.getAllByText("Transcript")).toHaveLength(before + 1));
    });
  });
});
