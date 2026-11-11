import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiPost } from "../../api/client";
import { STT_BACKEND_STATS, STT_QUEUE } from "../../api/mock/dataset";
import type { SttBackendStat } from "../../api/types";
import { useToastStore } from "../../state/toasts";
import { renderWithProviders } from "../../test/render";
import { BackendsTab, healthState, QueueStrip } from "./SttBackendsTab";

const signInAsAdmin = () => apiPost("/auth/login", { username: "alex", password: "hunter2" });

const row = async (name: string): Promise<HTMLElement> => {
  const cell = await screen.findByText(name);
  const tableRow = cell.closest("tr");
  if (!tableRow) throw new Error(`no row for ${name}`);
  return tableRow;
};

const BASE = STT_BACKEND_STATS[0] as SttBackendStat;

const stat = (overrides: Partial<SttBackendStat>): SttBackendStat => ({ ...BASE, ...overrides });

/**
 * The page exists because the old one reported zeros: the counters live in the worker
 * process, and the API process was building its own router.  So the cases below are all
 * about *whose* numbers are on screen and how honest the page is when nobody is publishing.
 */
describe("BackendsTab", () => {
  it("shows the queue as SQL counted it", async () => {
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    expect(await screen.findByText("queued (live)")).toBeInTheDocument();
    expect(screen.getByText(String(STT_QUEUE.queued_live))).toBeInTheDocument();
    expect(screen.getByText("4,310")).toBeInTheDocument(); // the backlog band
    expect(screen.getByText("20.6")).toBeInTheDocument(); // jobs a minute
    expect(screen.getByText("3.5h")).toBeInTheDocument(); // the ETA at that rate
  });

  it("renders a row per backend with its counters", async () => {
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    const whisper = await row("whisper");
    expect(within(whisper).getByText("2 / 4")).toBeInTheDocument(); // in flight / concurrency
    expect(within(whisper).getByText("4,168")).toBeInTheDocument(); // completed
    expect(within(whisper).getByText("9.14×")).toBeInTheDocument(); // real-time factor
    expect(within(whisper).getByText("21 / 0")).toBeInTheDocument(); // the last minute
  });

  it("calls a backend that keeps erroring degraded, not down", async () => {
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    const npu = await row("whisper-npu");
    expect(within(npu).getByText("degraded")).toBeInTheDocument();
    expect(within(npu).getByText(/NPU inference request timed out/)).toBeInTheDocument();
  });

  it("distinguishes off-by-operator from off-in-the-configuration", async () => {
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    const spare = await row("gpu-spare");
    expect(within(spare).getByText("disabled")).toBeInTheDocument();
    expect(within(spare).getByText(/by operator \(config: on\)/)).toBeInTheDocument();
  });

  it("says the worker is not reporting rather than showing an idle system", () => {
    const stale = stat({ stale: true, updated_at: new Date(Date.now() - 600_000).toISOString() });
    expect(healthState(stale)).toBe("stale");
    // …and a switched-off backend is off, not unhealthy: a red dot nobody can act on is noise.
    expect(healthState(stat({ enabled: false, stale: false }))).toBe("disabled");
    expect(healthState(stat({ stale: false, consecutive_errors: 5 }))).toBe("degraded");
    expect(healthState(stat({ stale: false, healthy: false }))).toBe("down");
  });

  it("warns on the queue strip when nothing is publishing", () => {
    renderWithProviders(<QueueStrip queue={STT_QUEUE} reporting={false} />);
    expect(screen.getByText(/is not reporting/)).toBeInTheDocument();
  });

  it("turns a backend back on and keeps the switch in step", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    const spare = await row("gpu-spare");
    const toggle = within(spare).getByRole("switch");
    expect(toggle).toHaveAttribute("aria-checked", "false");

    await user.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
    await waitFor(() => expect(useToastStore.getState().toasts.at(-1)?.title).toBe("gpu-spare enabled"));
  });

  it("asks before the last enabled backend is switched off", async () => {
    const user = userEvent.setup();
    await signInAsAdmin();
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");

    // Two of the three fixtures are enabled; turn one off with no dialog…
    const npu = await row("whisper-npu");
    await user.click(within(npu).getByRole("switch"));
    await waitFor(() => expect(within(npu).getByRole("switch")).toHaveAttribute("aria-checked", "false"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    // …and the last one asks, because it stops transcription.
    const whisper = await row("whisper");
    await user.click(within(whisper).getByRole("switch"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/only backend still enabled/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /disable anyway/i }));
    await waitFor(() => expect(within(whisper).getByRole("switch")).toHaveAttribute("aria-checked", "false"));
  });

  it("leaves the switches alone for a reader", async () => {
    renderWithProviders(<BackendsTab />, "/admin?tab=backends");
    const whisper = await row("whisper");
    expect(within(whisper).getByRole("switch")).toBeDisabled();
  });
});
