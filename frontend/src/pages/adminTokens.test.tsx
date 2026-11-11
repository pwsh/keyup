import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiGet, apiPost } from "../api/client";
import type { ApiTokenInfo } from "../api/types";
import { renderWithProviders } from "../test/render";
import { Admin } from "./Admin";

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

/**
 * The Tokens tab.
 *
 * `expires_at` was hard-coded to null on the way out even though the API had always
 * accepted one, and a token's row said nothing about what was holding it — so "can I
 * revoke this?" was a question nobody could answer from this page.
 */
describe("the Tokens tab", () => {
  it("says which recorder holds a token, so a revoke is not a guess", async () => {
    renderWithProviders(<Admin />, "/admin?tab=tokens");
    const row = (await screen.findByText("sdrtrunk recorder")).closest("li");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText(/used by attic-trunkd/)).toBeInTheDocument();

    // A token nothing has ever polled with says nothing rather than "used by".
    const unused = (await screen.findByText("CAD integration")).closest("li");
    expect(within(unused as HTMLElement).queryByText(/used by/)).not.toBeInTheDocument();
  });

  it("sends the expiry the operator typed rather than a hard-coded null", async () => {
    const user = userEvent.setup();
    await signIn();
    renderWithProviders(<Admin />, "/admin?tab=tokens");

    await user.click(await screen.findByRole("button", { name: /New token/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Label"), "weekend loan");
    await user.type(within(dialog).getByLabelText("Expires"), "2026-09-30T00:00");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(async () => {
      const tokens = await apiGet<ApiTokenInfo[]>("/admin/tokens");
      const minted = tokens.find((token) => token.label === "weekend loan");
      expect(minted?.expires_at).not.toBeNull();
      // A `datetime-local` value has no zone; it is converted from the operator's own
      // clock on the way out rather than handed to the API to read as UTC.
      expect(new Date(minted?.expires_at ?? "").getTime()).toBe(new Date("2026-09-30T00:00").getTime());
    });
  });

  it("offers Delete on a revoked row only, and Revoke on the live ones", async () => {
    await signIn();
    renderWithProviders(<Admin />, "/admin?tab=tokens");

    const revoked = (await screen.findByText("old laptop")).closest("li") as HTMLElement;
    expect(within(revoked).getByRole("button", { name: "Delete old laptop" })).toBeInTheDocument();
    expect(within(revoked).queryByRole("button", { name: /^Revoke/ })).not.toBeInTheDocument();

    // A token still in service can only be revoked; deleting it would be a 409 anyway.
    const live = (await screen.findByText("sdrtrunk recorder")).closest("li") as HTMLElement;
    expect(within(live).getByRole("button", { name: "Revoke sdrtrunk recorder" })).toBeInTheDocument();
    expect(within(live).queryByRole("button", { name: /^Delete/ })).not.toBeInTheDocument();
  });

  it("deletes a revoked token only after the confirmation is answered", async () => {
    const user = userEvent.setup();
    await signIn();
    renderWithProviders(<Admin />, "/admin?tab=tokens");

    await user.click(await screen.findByRole("button", { name: "Delete old laptop" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("Permanently delete token old laptop? This cannot be undone."),
    ).toBeInTheDocument();

    // Backing out leaves the row alone.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("old laptop")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete old laptop" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete token" }));

    await waitFor(() => expect(screen.queryByText("old laptop")).not.toBeInTheDocument());
    const tokens = await apiGet<ApiTokenInfo[]>("/admin/tokens");
    expect(tokens.some((token) => token.label === "old laptop")).toBe(false);
  });

  it("mints a token with no expiry when the field is left alone", async () => {
    const user = userEvent.setup();
    await signIn();
    renderWithProviders(<Admin />, "/admin?tab=tokens");

    await user.click(await screen.findByRole("button", { name: /New token/ }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Label"), "permanent");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(async () => {
      const tokens = await apiGet<ApiTokenInfo[]>("/admin/tokens");
      expect(tokens.find((token) => token.label === "permanent")?.expires_at).toBeNull();
    });
  });
});
