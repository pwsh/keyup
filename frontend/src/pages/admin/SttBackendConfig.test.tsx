import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../../api/client";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { BackendsTab } from "./SttBackendsTab";

const signInAsAdmin = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

const row = async (name: string): Promise<HTMLElement> => {
  const cell = await screen.findByText(name);
  const tableRow = cell.closest("tr");
  if (!tableRow) throw new Error(`no row for ${name}`);
  return tableRow;
};

/**
 * Defining a backend from the page, rather than from a `.env` on the host.
 *
 * The two things worth asserting in a browser test are the ones a reader of the code cannot
 * check: that the form follows the *kind* (a local model has no base URL, a remote server has
 * no compute type), and that the key is never pre-filled — the server has never sent it, and a
 * box that looks full would invite an operator to trust it.
 */
describe("the backend dialog", () => {
  it("is closed to a reader and open to an admin", async () => {
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    expect(await screen.findByRole("button", { name: /new backend/i })).toBeDisabled();

    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await waitFor(() => expect(screen.getAllByRole("button", { name: /new backend/i }).at(-1)).toBeEnabled());
  });

  it("shows the fields the chosen kind actually has", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(await screen.findByRole("button", { name: /new backend/i }));

    const dialog = await screen.findByRole("dialog");
    // A remote server: a URL and a key, no compute type.
    expect(within(dialog).getByLabelText(/^base url$/i)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/^compute type$/i)).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("combobox", { name: /backend kind/i }));
    await user.click(await screen.findByRole("option", { name: /faster-whisper/i }));

    // An in-process model: a device and a compute type, and no URL to get wrong.
    expect(within(dialog).queryByLabelText(/^base url$/i)).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^compute type$/i)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^device$/i)).toBeInTheDocument();
  });

  it("refuses to save until the kind's requirements are met", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(await screen.findByRole("button", { name: /new backend/i }));
    const dialog = await screen.findByRole("dialog");

    const save = within(dialog).getByRole("button", { name: /add backend/i });
    expect(save).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/^name$/i), "gpu-box");
    expect(save).toBeDisabled(); // still no model
    await user.type(within(dialog).getByLabelText(/^model$/i), "large-v3");
    await user.type(within(dialog).getByLabelText(/^base url$/i), "http://gpu-box:8000");
    expect(save).toBeEnabled();
  });

  it("will not let a name that is already taken be saved", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(await screen.findByRole("button", { name: /new backend/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/^name$/i), "whisper");
    await user.type(within(dialog).getByLabelText(/^model$/i), "large-v3");
    await user.type(within(dialog).getByLabelText(/^base url$/i), "http://x:8000");
    expect(within(dialog).getByText(/already a backend called whisper/i)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /add backend/i })).toBeDisabled();
  });

  it("adds a backend and shows it in the table", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(await screen.findByRole("button", { name: /new backend/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/^name$/i), "gpu-box");
    await user.type(within(dialog).getByLabelText(/^model$/i), "large-v3");
    await user.type(within(dialog).getByLabelText(/^base url$/i), "http://gpu-box:8000");
    await user.click(within(dialog).getByRole("button", { name: /add backend/i }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await row("gpu-box")).toBeInTheDocument();
    expect(useToastStore.getState().toasts.at(-1)?.title).toBe("gpu-box added");
  });

  it("never pre-fills the stored key, and says one is there", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");

    // Give the fixture backend a key by saving one, then reopen it.
    await user.click(await screen.findByRole("button", { name: /new backend/i }));
    let dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^name$/i), "keyed");
    await user.type(within(dialog).getByLabelText(/^model$/i), "large-v3");
    await user.type(within(dialog).getByLabelText(/^base url$/i), "http://keyed:8000");
    await user.type(within(dialog).getByLabelText(/^api key$/i), "sk-secret-value");
    await user.click(within(dialog).getByRole("button", { name: /add backend/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(within(await row("keyed")).getByRole("button", { name: /edit keyed/i }));
    dialog = await screen.findByRole("dialog");
    const key = within(dialog).getByLabelText(/^api key$/i);
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "unchanged");
    expect(within(dialog).getByText(/a key is stored\. leave blank to keep it/i)).toBeInTheDocument();
    // …and it is not sitting anywhere else on the page either.
    expect(document.body.textContent).not.toContain("sk-secret-value");
  });

  it("cannot rename an existing backend", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(within(await row("whisper-npu")).getByRole("button", { name: /edit/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText(/^name$/i)).toBeDisabled();
  });
});

describe("the probe", () => {
  it("reports health and the real-time factor it measured", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(within(await row("whisper")).getByRole("button", { name: /probe/i }));

    const result = await screen.findByRole("status");
    expect(within(result).getByText(/answered/)).toBeInTheDocument();
    expect(within(result).getByText(/8.42× real time/)).toBeInTheDocument();
  });

  it("says so plainly when the backend does not answer", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(within(await row("whisper-npu")).getByRole("button", { name: /probe/i }));

    const result = await screen.findByRole("status");
    expect(within(result).getByText(/did not answer/)).toBeInTheDocument();
    expect(within(result).getByText(/no clip was timed/i)).toBeInTheDocument();
  });
});

describe("deleting a backend", () => {
  it("explains that history is kept, and keeps the row when it is", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    await user.click(within(await row("whisper")).getByRole("button", { name: /delete/i }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/the row is kept and disabled/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /^remove$/i }));

    await waitFor(() =>
      expect(useToastStore.getState().toasts.at(-1)?.title).toBe("whisper disabled and kept"),
    );
    // Still on the page, because its transcripts still name it.
    expect(await row("whisper")).toBeInTheDocument();
  });
});

describe("the configuration version", () => {
  it("says whether the worker has picked the last save up", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    expect(await screen.findByText(/config v4, applied/)).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: /new backend/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^name$/i), "gpu-box");
    await user.type(within(dialog).getByLabelText(/^model$/i), "large-v3");
    await user.type(within(dialog).getByLabelText(/^base url$/i), "http://gpu-box:8000");
    await user.click(within(dialog).getByRole("button", { name: /add backend/i }));

    // The save moved the database on; the worker is still on the old generation, and the
    // page says so rather than leaving the operator watching a counter that has not moved.
    expect(await screen.findByText(/the worker is on v4/)).toBeInTheDocument();
  });
});
