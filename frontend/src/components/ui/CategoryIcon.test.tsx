import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TALKGROUP_CATEGORIES } from "../../api/types";
import { renderWithProviders } from "../../test/render";
import { CategoryIcon } from "./CategoryIcon";

describe("CategoryIcon", () => {
  it("draws a glyph for every category the app knows", () => {
    for (const category of TALKGROUP_CATEGORIES) {
      const { container, unmount } = render(<CategoryIcon category={category} interactive={false} />);
      expect(container.querySelector("svg")).not.toBeNull();
      unmount();
    }
  });

  it("names the category in words, not only in color", () => {
    renderWithProviders(<CategoryIcon category="ems" interactive={false} />);
    expect(screen.getByText("Category: EMS")).toBeInTheDocument();
  });

  it("adds the RadioReference service tag when the row carries one", () => {
    renderWithProviders(<CategoryIcon category="fire" serviceTag="Fire-Tac" interactive={false} />);
    expect(screen.getByText("Category: Fire — Fire-Tac")).toBeInTheDocument();
  });

  it("falls back for a category the list does not name, and still says what it was", () => {
    // The column is free text on the wire, so this is an ordinary case, not an error.
    renderWithProviders(<CategoryIcon category="weather" interactive={false} />);
    expect(screen.getByText("Category: Weather")).toBeInTheDocument();
  });

  it("says so when there is no category at all", () => {
    renderWithProviders(<CategoryIcon category={null} interactive={false} />);
    expect(screen.getByText("Category: Uncategorized")).toBeInTheDocument();
  });
});
