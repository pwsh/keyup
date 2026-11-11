import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../../api/client";
import { SITE_MESSAGE_KINDS } from "../../api/mock/dataset";
import { renderWithProviders } from "../../test/render";
import { MessageKindsPanel } from "./MessageKindsPanel";

const signIn = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

const rowFor = async (key: string): Promise<HTMLElement> => {
  const cell = await screen.findByText(key);
  const row = cell.closest("tr");
  if (!row) throw new Error(`no row for ${key}`);
  return row;
};

describe("the message-kind registry panel", () => {
  it("shows every kind, badges a recent one, and shows the bytes that changed", async () => {
    renderWithProviders(<MessageKindsPanel siteId={1} />);

    // Central's Harris beacon: first heard three days ago, so it is still news, and its
    // old exemplar is on screen beside the new one.
    const beacon = await rowFor("164:34");
    expect(within(beacon).getByText("new")).toBeInTheDocument();
    expect(within(beacon).getByText("a2a4b1c0deadbeef")).toBeInTheDocument();
    expect(within(beacon).getByText("a2a4b1c000000000")).toBeInTheDocument();

    // A kind that has been there for months is not new, and carries the name it was given.
    const known = await rowFor("0:56");
    expect(within(known).queryByText("new")).not.toBeInTheDocument();
    expect(within(known).getByText("Adjacent status broadcast")).toBeInTheDocument();
  });

  it("filters to the kinds nobody has named", async () => {
    const user = userEvent.setup();
    renderWithProviders(<MessageKindsPanel siteId={1} />);

    await screen.findByText("0:56");
    await user.click(screen.getByRole("button", { name: /Unnamed only/ }));
    expect(await screen.findByText("164:34")).toBeInTheDocument();
    expect(screen.queryByText("0:56")).not.toBeInTheDocument();
  });

  it("offers the operator's two fields to nobody who is signed out", async () => {
    renderWithProviders(<MessageKindsPanel siteId={1} />);
    const row = await rowFor("164:34");
    expect(within(row).getByRole("switch", { name: /164:34 is a known message kind/ })).toBeDisabled();
  });

  it("lets an admin name a kind", async () => {
    const user = userEvent.setup();
    await signIn();
    renderWithProviders(<MessageKindsPanel siteId={1} />);

    const row = await rowFor("164:34");
    const toggle = within(row).getByRole("switch", { name: /164:34 is a known message kind/ });
    await waitFor(() => expect(toggle).toBeEnabled());
    await user.click(toggle);

    await waitFor(() =>
      expect(SITE_MESSAGE_KINDS[1]?.find((kind) => kind.key === "164:34")?.known).toBe(true),
    );
  });
});
