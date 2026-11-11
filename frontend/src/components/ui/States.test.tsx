import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { renderWithProviders } from "../../test/render";
import { ErrorState } from "./States";

describe("ErrorState", () => {
  it("says what happened in words and keeps the raw server text behind Details", () => {
    renderWithProviders(
      <ErrorState error={new ApiError("500 Internal Server Error", 500, "/api/calls", "boom")} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/Something went wrong on the server/);
    expect(screen.queryByText(/500 — boom/)).not.toBeInTheDocument();
    expect(screen.getByText("Details")).toBeInTheDocument();
    expect(screen.getByText("Error 500: boom")).not.toBeVisible();
  });
});
