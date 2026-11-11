/**
 * The last axe findings after the 2026-10-29 UI review: headers with no name, a scroll box
 * a keyboard could not reach. One case per fix a test can hold in place.
 */
import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as client from "../api/client";
import { apiPost } from "../api/client";
import { EXPORT_STATUS, HTTPS_STATUS } from "../api/mock/dataset";
import type { ExportLedgerRow } from "../api/types";
import { StatTile } from "../components/charts/StatTile";
import { TalkgroupFields } from "../components/help/TalkgroupFields";
import { ConnectRecorderCard } from "../components/recorders/ConnectRecorderCard";
import { RecorderTab } from "../components/recorders/RecorderTab";
import { renderWithProviders } from "../test/render";
import { Alerts } from "./Alerts";
import { AgenciesTab } from "./admin/AgenciesTab";
import { ExportsTab } from "./admin/ExportsTab";
import { HttpsTab } from "./admin/HttpsTab";
import { BackendsTab } from "./admin/SttBackendsTab";
import { Live } from "./Live";

const signInAsAdmin = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

afterEach(() => {
  vi.restoreAllMocks();
});

/** Every column header in the document, by its accessible name. */
const headerNames = () =>
  screen.getAllByRole("columnheader").map((header) => header.textContent?.trim() ?? "");

describe("action columns have a name", () => {
  it("Agencies", async () => {
    await signInAsAdmin();
    renderWithProviders(<AgenciesTab canWrite />, "/admin?tab=agencies");
    expect(await screen.findByRole("columnheader", { name: "Actions" })).toBeInTheDocument();
    expect(headerNames()).not.toContain("");
  });

  it("Backends", async () => {
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    expect(await screen.findByRole("columnheader", { name: "Actions" })).toBeInTheDocument();
    expect(headerNames()).not.toContain("");
  });

  it("Recorder", async () => {
    await signInAsAdmin();
    renderWithProviders(<RecorderTab />, "/admin?tab=recorder");
    expect((await screen.findAllByRole("columnheader", { name: "Actions" })).length).toBeGreaterThan(0);
    expect(headerNames()).not.toContain("");
  });

  it("Exports", async () => {
    await signInAsAdmin();
    const real = client.apiGet;
    const row = {
      id: 7,
      call_id: 101,
      talkgroup: 1101,
      talkgroup_alias: "EC FIRE 1",
      service: "broadcastify",
      status: "failed",
      attempts: 5,
      updated_at: "2026-10-31T12:00:00Z",
      last_error: "timed out",
      skip_reason: null,
      response: null,
    } as unknown as ExportLedgerRow;
    vi.spyOn(client, "apiGet").mockImplementation((async (path: string, ...rest: unknown[]) =>
      path === "/admin/exports"
        ? { ...EXPORT_STATUS, recent: [row] }
        : (real as (...args: unknown[]) => Promise<unknown>)(path, ...rest)) as typeof client.apiGet);
    renderWithProviders(<ExportsTab />, "/admin?tab=exports");
    expect(await screen.findByRole("columnheader", { name: "Actions" })).toBeInTheDocument();
    expect(headerNames()).not.toContain("");
  });
});

describe("scroll boxes a keyboard can reach", () => {
  it("Alerts scrolls in a named region that takes focus", async () => {
    await signInAsAdmin();
    renderWithProviders(<Alerts />, "/alerts");
    expect(await screen.findByRole("region", { name: "Alert rules" })).toHaveAttribute("tabindex", "0");
  });

  it("the Help page's service tag table scrolls in a named region that takes focus", async () => {
    renderWithProviders(<TalkgroupFields />, "/help");
    expect(await screen.findByRole("region", { name: "Service tags" })).toHaveAttribute("tabindex", "0");
  });

  it("the recorder configuration snippet takes focus", () => {
    renderWithProviders(<ConnectRecorderCard canAdmin />, "/admin?tab=recorder");
    expect(screen.getByRole("region", { name: "Recorder configuration" })).toHaveAttribute("tabindex", "0");
  });

  it("Live's Who's active list takes focus", async () => {
    renderWithProviders(<Live />, "/live");
    await waitFor(() =>
      expect(screen.getByRole("list", { name: "Who's active" })).toHaveAttribute("tabindex", "0"),
    );
  });
});

describe("a stat tile", () => {
  it("is a term and its value, not a bold paragraph over a small one", () => {
    renderWithProviders(
      <StatTile
        label="Calls today"
        value="13,965"
        help="stats.tile.callsToday"
        footnote="2 in the archive"
      />,
    );
    expect(screen.getByRole("term")).toHaveTextContent("Calls today");
    expect(screen.getAllByRole("definition").map((item) => item.textContent)).toEqual([
      "13,965",
      "2 in the archive",
    ]);
  });
});

describe("the HTTPS certificate table", () => {
  it("scrolls in a named region a keyboard can reach", async () => {
    await signInAsAdmin();
    const real = client.apiGet;
    vi.spyOn(client, "apiGet").mockImplementation((async (path: string, ...rest: unknown[]) =>
      path === "/admin/https/status"
        ? {
            ...HTTPS_STATUS,
            certificates: [
              {
                domain: "radio.example.org",
                ok: true,
                trusted: true,
                issuer: "Let's Encrypt",
                not_after: "2027-01-01T00:00:00Z",
                days_left: 61,
                error: null,
              },
            ],
          }
        : (real as (...args: unknown[]) => Promise<unknown>)(path, ...rest)) as typeof client.apiGet);
    renderWithProviders(<HttpsTab />, "/admin?tab=https");
    const region = await screen.findByRole("region", { name: "Certificates" });
    expect(region).toHaveAttribute("tabindex", "0");
    await waitFor(() => expect(within(region).getByRole("table")).toBeInTheDocument());
  });
});
