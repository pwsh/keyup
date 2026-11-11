import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as client from "../../api/client";
import { apiPost } from "../../api/client";
import { ADMIN_AGENCIES } from "../../api/mock/dataset";
import { renderWithProviders } from "../../test/render";
import { AgencyDialog, codeProblem } from "./AgencyDialog";

async function signInAsAdmin(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

/**
 * Adding an agency the county's dispatch system has no code for.
 *
 * The code is the primary key everything else points at and cannot be changed
 * afterwards, so the validation happening *before* the round trip is the point of
 * the form — a 422 discovered on Save leaves the reader with no list to check
 * against, which is the same failure the picker was built to end.
 */
describe("codeProblem", () => {
  it("insists on a code", () => {
    expect(codeProblem("", [])).toMatch(/required/);
  });

  it("accepts the shape the API accepts and rejects the rest", () => {
    expect(codeProblem("NORTH_SO", [])).toBeNull();
    expect(codeProblem("north_so", [])).toBeNull(); // folded before it is checked
    expect(codeProblem("XX0010100", [])).toBeNull();
    expect(codeProblem("X", [])).toMatch(/2 to 16/);
    expect(codeProblem("way-too-long-a-code", [])).toMatch(/2 to 16/);
    expect(codeProblem("has space", [])).toMatch(/2 to 16/);
  });

  it("catches a duplicate before the request", () => {
    expect(codeProblem("ecc", ["ECC"])).toBe("ECC is already an agency.");
  });
});

describe("AgencyDialog", () => {
  it("will not submit until the code and name are usable", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<AgencyDialog open onOpenChange={vi.fn()} existingCodes={["ECC"]} />);

    const add = screen.getByRole("button", { name: "Add agency" });
    expect(add).toBeDisabled();

    await user.type(screen.getByLabelText("Code"), "ECC");
    await user.type(screen.getByLabelText("Name"), "Something");
    expect(screen.getByText("ECC is already an agency.")).toBeInTheDocument();
    expect(add).toBeDisabled();
  });

  it("creates a local agency and hands the code back to whoever opened it", async () => {
    await signInAsAdmin();
    const post = vi.spyOn(client, "apiPost");
    const onCreated = vi.fn();
    const onOpenChange = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <AgencyDialog open onOpenChange={onOpenChange} onCreated={onCreated} existingCodes={[]} />,
    );

    // Typed in lower case: the box folds it, because that is what gets stored.
    await user.type(screen.getByLabelText("Code"), "north_so");
    await user.type(screen.getByLabelText("Name"), "North County Sheriff");
    await user.click(screen.getByRole("button", { name: "Add agency" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("NORTH_SO"));
    expect(post).toHaveBeenCalledWith(
      "/admin/agencies",
      expect.objectContaining({ code: "NORTH_SO", name: "North County Sheriff" }),
    );
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("fixes the code when editing, because everything points at it", async () => {
    await signInAsAdmin();
    const existing = ADMIN_AGENCIES[0];
    renderWithProviders(<AgencyDialog open onOpenChange={vi.fn()} agency={existing ?? null} />);

    expect(screen.getByLabelText("Code")).toBeDisabled();
    expect(screen.getByLabelText("Code")).toHaveValue(existing?.code);
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});
