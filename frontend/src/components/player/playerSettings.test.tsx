import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Call } from "../../api/types";
import { usePlayerStore } from "../../state/player";
import { renderWithProviders } from "../../test/render";
import { Player } from "./Player";

/** Review 2026-10-29 #23: the phone player's switches live behind a settings popover. */

const CALL = {
  id: 7,
  talkgroup_alias: "EC EVENT 8 WITH A LONGER NAME",
  duration_s: 4,
  started_at: new Date(1_800_000_000_000).toISOString(),
  transcript_status: "done",
  transcript_quality: "good",
} as unknown as Call;

function open() {
  renderWithProviders(<Player />);
  act(() => usePlayerStore.getState().select(CALL, { play: false }));
  return screen.getByRole("region", { name: "Audio player" });
}

describe("the player's settings popover", () => {
  it("keeps the five switches out of the bar until asked for", async () => {
    const user = userEvent.setup();
    const bar = open();
    expect(within(bar).queryByRole("switch")).not.toBeInTheDocument();
    // One seek strip, full width, still a native slider.
    expect(within(bar).getByRole("slider", { name: "Seek within the call" })).toBeInTheDocument();

    const trigger = within(bar).getByRole("button", { name: "Player settings" });
    trigger.focus();
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog");
    for (const name of ["Continuous", "Auto-play new calls", "Normalize", "Processed", "Follow new calls"]) {
      expect(within(dialog).getByRole("switch", { name: new RegExp(name) })).toBeInTheDocument();
    }

    // One Escape closes it and puts focus back on the gear.
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("lets Space flip a switch without pausing, and still toggles playback elsewhere", async () => {
    const user = userEvent.setup();
    const bar = open();
    await user.click(within(bar).getByRole("button", { name: "Player settings" }));
    const continuous = await screen.findByRole("switch", { name: /Continuous/ });
    const before = usePlayerStore.getState().continuous;
    continuous.focus();
    await user.keyboard(" ");
    expect(usePlayerStore.getState().continuous).toBe(!before);
    expect(usePlayerStore.getState().playing).toBe(false);

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    (document.activeElement as HTMLElement | null)?.blur();
    await user.keyboard(" ");
    expect(usePlayerStore.getState().playing).toBe(true);
  });
});
