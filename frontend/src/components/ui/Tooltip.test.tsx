import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { HELP, isHelpLoaded, loadHelp, resetHelpForTest } from "../../help/help";
import { HELP_TEXT } from "../../help/text";
import { renderWithProviders } from "../../test/render";
import { Button } from "./Button";
import { Tip } from "./Tooltip";

describe("informational tooltip anchors (#21)", () => {
  it("are not tab stops, and carry their explanation as aria-describedby", () => {
    renderWithProviders(
      <Tip label="How confident the reconciler was in this copy">
        <span tabIndex={0} data-testid="score">
          87
        </span>
      </Tip>,
    );
    const score = screen.getByTestId("score");
    expect(score).not.toHaveAttribute("tabindex");
    const describedBy = score.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy as string)).toHaveTextContent(
      "How confident the reconciler was in this copy",
    );
    expect(score).toHaveAccessibleDescription("How confident the reconciler was in this copy");
  });

  it("share one description node for the same sentence", () => {
    renderWithProviders(
      <div>
        {[1, 2, 3].map((n) => (
          <Tip key={n} label="Calls in the last 24 hours">
            <span tabIndex={0} data-testid="cell">
              {n}
            </span>
          </Tip>
        ))}
      </div>,
    );
    const ids = new Set(screen.getAllByTestId("cell").map((cell) => cell.getAttribute("aria-describedby")));
    expect(ids.size).toBe(1);
  });

  it("leave real controls focusable, with the bubble on focus", async () => {
    renderWithProviders(<Button tip="Save the notes">Save</Button>);
    const save = screen.getByRole("button", { name: "Save" });
    fireEvent.focus(save);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Save the notes");
    expect(save).toHaveAccessibleDescription("Save the notes");
    fireEvent.blur(save);
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
  });

  it("render one bubble for the whole app however many anchors there are", async () => {
    renderWithProviders(
      <>
        <Button tip="First">One</Button>
        <Button tip="Second">Two</Button>
      </>,
    );
    fireEvent.focus(screen.getByRole("button", { name: "One" }));
    fireEvent.focus(screen.getByRole("button", { name: "Two" }));
    await waitFor(() => expect(screen.getAllByRole("tooltip")).toHaveLength(1));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Second");
  });
});

describe("lazy help text (#43)", () => {
  afterEach(async () => {
    await loadHelp();
  });

  it("hands out placeholders until the text chunk loads, and the tooltip still shows the sentence", async () => {
    resetHelpForTest();
    expect(isHelpLoaded()).toBe(false);
    const label = HELP["player.play"];
    expect(label).not.toBe(HELP_TEXT["player.play"]);
    renderWithProviders(<Button tip={label}>Play</Button>);
    fireEvent.focus(screen.getByRole("button", { name: "Play" }));
    // Opening a tooltip fetches the chunk; the bubble reads the real sentence, never the marker.
    expect(await screen.findByRole("tooltip")).toHaveTextContent(HELP_TEXT["player.play"]);
    expect(isHelpLoaded()).toBe(true);
    expect(HELP["player.play"]).toBe(HELP_TEXT["player.play"]);
  });

  it("is imported statically only by the help page", () => {
    const sources = import.meta.glob("../../**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const importers = Object.entries(sources)
      .filter(([path]) => !/\.test\.tsx?$/.test(path))
      .filter(([, source]) => /from\s+["'][./]*help\/text["']/.test(source))
      .map(([path]) => path);
    expect(importers).toEqual(["../../pages/Help.tsx"]);
  });
});
