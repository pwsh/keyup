import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Sites } from "../../pages/Sites";
import { renderWithProviders } from "../../test/render";
import { SiteProfilePanel } from "./SiteProfilePanel";

const section = async (name: RegExp): Promise<HTMLElement> => {
  const heading = await screen.findByRole("button", { name });
  const block = heading.parentElement;
  if (!block) throw new Error(`no section for ${name}`);
  return block;
};

describe("the site profile panel", () => {
  it("renders every section of what the site announces", async () => {
    renderWithProviders(<SiteProfilePanel siteId={1} />);

    // Identity: the numbers the control channel keys, NAC in both bases.
    const identity = await section(/^Identity/);
    expect(within(identity).getByText("781824")).toBeInTheDocument();
    expect(within(identity).getByText("52 (0x34)")).toBeInTheDocument();
    expect(within(identity).getByText(/site 9001/)).toBeInTheDocument();

    // Services: available as chips, supported-but-not-offered beside them.
    const services = await section(/^Services/);
    expect(within(services).getByText("Group Voice")).toBeInTheDocument();
    expect(within(services).getByText("Encryption")).toBeInTheDocument();
    expect(within(services).getByText("0x2F1000")).toBeInTheDocument();

    // Control: the secondary channel whose frequency cannot be computed says so.
    const control = await section(/^Control channels/);
    expect(within(control).getByText("851.6500 MHz")).toBeInTheDocument();
    expect(within(control).getByText("frequency not computable")).toBeInTheDocument();

    // Neighbours: one resolved to a Keyup site, one named only by RadioReference (and
    // marked RR, because Keyup knows nothing else about that tower), one nameless.
    const adjacent = await section(/^Adjacent sites/);
    expect(within(adjacent).getByText("North")).toBeInTheDocument();
    expect(within(adjacent).getByText("West Co Simulcast")).toBeInTheDocument();
    expect(within(adjacent).getByText("RR")).toBeInTheDocument();
    expect(within(adjacent).getByText("unnamed")).toBeInTheDocument();

    expect(within(await section(/^Band plan/)).getByText("851.0063 MHz")).toBeInTheDocument();
    expect(within(await section(/^Traffic channels/)).getByText("851.3000 MHz")).toBeInTheDocument();
    expect(within(await section(/^Data channel/)).getByText("858.9875 MHz")).toBeInTheDocument();
    // The single-decode CRC noise is counted apart from "nobody has named this".
    const messages = await section(/^Message kinds/);
    expect(within(messages).getByText(/2 named, 1 still unnamed/)).toBeInTheDocument();
    expect(within(messages).getByText(/8 more were decoded once or twice/)).toBeInTheDocument();
  });

  it("lists what the site has never told us", async () => {
    renderWithProviders(<SiteProfilePanel siteId={1} />);

    const unknown = await section(/^Unknown/);
    const lines = within(unknown).getAllByRole("listitem");
    expect(lines).toHaveLength(4);
    expect(lines.map((line) => line.textContent).join(" ")).toContain(
      "match no site Keyup or RadioReference knows",
    );
  });

  it("collapses and reopens a section", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SiteProfilePanel siteId={1} />);

    const heading = await screen.findByRole("button", { name: /^Band plan/ });
    expect(heading).toHaveAttribute("aria-expanded", "true");
    await user.click(heading);
    expect(heading).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("851.0063 MHz")).not.toBeInTheDocument();
    await user.click(heading);
    expect(await screen.findByText("851.0063 MHz")).toBeInTheDocument();
  });

  it("says nobody has decoded a service message rather than showing an empty list", async () => {
    // Site 3 has never been observed at all, so the mock has no profile for it and the
    // panel shows the error state instead of pretending the site is empty.
    renderWithProviders(<SiteProfilePanel siteId={3} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load/);
  });
});

describe("the sites table", () => {
  it("shows the services of each site, and null is not an empty list", async () => {
    renderWithProviders(<Sites />);

    const central = (await screen.findByText("Central")).closest("tr");
    expect(central).not.toBeNull();
    expect(within(central as HTMLElement).getByText("Composite")).toBeInTheDocument();
    // Five available, three chips and a "+2".
    expect(within(central as HTMLElement).getByText("+2")).toBeInTheDocument();

    // The site nobody has decoded a bitmap for says so, rather than showing nothing.
    expect(screen.getAllByText("none decoded yet").length).toBeGreaterThan(0);
  });

  it("expands a row into its profile and folds it back", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Sites />);

    const toggle = await screen.findByRole("button", {
      name: /Show everything known about .*Central/,
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-expanded", "true"));
    expect(await screen.findByRole("button", { name: /^Unknown/ })).toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /^Unknown/ })).not.toBeInTheDocument();
  });
});
