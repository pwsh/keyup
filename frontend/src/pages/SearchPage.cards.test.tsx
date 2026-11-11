import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { usePlayerStore } from "../state/player";
import { renderWithProviders } from "../test/render";
import { SearchPage } from "./SearchPage";

afterEach(() => usePlayerStore.getState().clear());

/** Review #41: a result card plays, links its channel and time, and shows no bare score. */
describe("Search result cards", () => {
  it("has a named play button, a talkgroup link and a call link", async () => {
    renderWithProviders(<SearchPage />, "/search?q=fire");
    const play = (await screen.findAllByRole("button", { name: /^Play the call on .+, / }))[0] as HTMLElement;
    const card = play.parentElement?.closest("li") as HTMLElement;
    const links = within(card).getAllByRole("link");
    expect(links.some((link) => /^\/talkgroups\/\d+$/.test(link.getAttribute("href") ?? ""))).toBe(true);
    expect(links.some((link) => /^\/calls\/\d+$/.test(link.getAttribute("href") ?? ""))).toBe(true);
    expect(within(card).queryByText(/^\d\.\d\d$/)).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Play" })).not.toBeInTheDocument();
  });

  it("plays from the button and from a click on the card body", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SearchPage />, "/search?q=fire");
    const buttons = await screen.findAllByRole("button", { name: /^Play the call on / });
    await user.click(buttons[0] as HTMLElement);
    const first = usePlayerStore.getState().current?.id;
    expect(first).toBeDefined();
    const second = (buttons[1] as HTMLElement).closest("li") as HTMLElement;
    await user.click(within(second).getByText((_, el) => el?.tagName === "P"));
    expect(usePlayerStore.getState().current?.id).not.toBe(first);
  });
});
