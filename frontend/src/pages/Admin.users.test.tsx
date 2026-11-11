import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import * as client from "../api/client";
import { apiPost } from "../api/client";
import { USERS } from "../api/mock/dataset";
import { renderWithProviders } from "../test/render";
import { Admin } from "./Admin";

async function signInAsAdmin(): Promise<void> {
  await apiPost("/auth/login", { username: "alex", password: "hunter2" });
}

/**
 * Creating and rescuing an account.
 *
 * `POST /admin/users` refuses a body with no password, so a form that never asked
 * for one produced nothing but 422s — and the mock accepted it, so the suite
 * agreed with the broken form. Both halves are pinned here.
 */
describe("the Users tab", () => {
  it("will not offer to create an account with no password", async () => {
    await signInAsAdmin();
    const user = userEvent.setup();
    renderWithProviders(<Admin />, "/admin?tab=users");

    await user.click(await screen.findByRole("button", { name: /New account/ }));
    await user.type(screen.getByRole("textbox", { name: "Username" }), "newcomer");
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("sends the password with the new account", async () => {
    await signInAsAdmin();
    const post = vi.spyOn(client, "apiPost");
    const user = userEvent.setup();
    renderWithProviders(<Admin />, "/admin?tab=users");

    await user.click(await screen.findByRole("button", { name: /New account/ }));
    await user.type(screen.getByRole("textbox", { name: "Username" }), "newcomer");
    await user.type(screen.getByLabelText("Password"), "correct horse");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      const call = post.mock.calls.find(([path]) => path === "/admin/users");
      expect(call?.[1]).toMatchObject({ username: "newcomer", password: "correct horse" });
    });
    post.mockRestore();
  });

  it("can set a password on an account that already exists", async () => {
    // `UserPatch.password` is the only way an operator rescues an account whose
    // password is lost, and nothing in the UI reached it.
    await signInAsAdmin();
    const patch = vi.spyOn(client, "apiPatch");
    const user = userEvent.setup();
    renderWithProviders(<Admin />, "/admin?tab=users");

    const target = USERS[0];
    await user.click(await screen.findByRole("button", { name: `Set a password for ${target?.username}` }));
    await user.type(screen.getByLabelText("New password"), "a new one");
    await user.click(screen.getByRole("button", { name: "Set password" }));

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith(`/admin/users/${target?.id}`, { password: "a new one" }),
    );
    patch.mockRestore();
  });
});
