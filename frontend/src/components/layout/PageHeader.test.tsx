import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { renderWithProviders } from "../../test/render";
import { PageHeader, pageInfoKey } from "./PageHeader";

const DESCRIPTION = "Every call the recorders kept, newest first.";

/** Review #39: the description folds behind an (i) after the first visit. */
describe("PageHeader description", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    localStorage.clear();
    window.innerWidth = 1440;
  });

  it("is shown on the first wide visit, then remembered as folded", () => {
    renderWithProviders(<PageHeader title="Calls" description={DESCRIPTION} />);
    expect(screen.getByRole("button", { name: "About Calls" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("page-description")).not.toHaveClass("sr-only");
    expect(localStorage.getItem(pageInfoKey("Calls"))).toBe("false");
  });

  it("starts folded after that, but stays in the page for screen readers", () => {
    localStorage.setItem(pageInfoKey("Calls"), "false");
    renderWithProviders(<PageHeader title="Calls" description={DESCRIPTION} />);
    const button = screen.getByRole("button", { name: "About Calls" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    const text = screen.getByText(DESCRIPTION);
    expect(text).toHaveClass("sr-only");
    expect(button).toHaveAttribute("aria-controls", text.id);
  });

  it("opens and folds from the keyboard, and remembers the choice", async () => {
    localStorage.setItem(pageInfoKey("Calls"), "false");
    const user = userEvent.setup();
    renderWithProviders(<PageHeader title="Calls" description={DESCRIPTION} />);
    const button = screen.getByRole("button", { name: "About Calls" });
    button.focus();
    await user.keyboard("{Enter}");
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(DESCRIPTION)).not.toHaveClass("sr-only");
    expect(localStorage.getItem(pageInfoKey("Calls"))).toBe("true");
    await user.keyboard(" ");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(localStorage.getItem(pageInfoKey("Calls"))).toBe("false");
  });

  it("starts folded on a phone and saves the first visit for a wide screen", () => {
    window.innerWidth = 375;
    renderWithProviders(<PageHeader title="Calls" description={DESCRIPTION} />);
    expect(screen.getByRole("button", { name: "About Calls" })).toHaveAttribute("aria-expanded", "false");
    expect(localStorage.getItem(pageInfoKey("Calls"))).toBeNull();
  });
});
