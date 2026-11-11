import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CALLS } from "../../api/mock/dataset";
import { toCall } from "../../api/mock/filter";
import type { Call } from "../../api/types";
import { contrastRatio } from "../../lib/color";
import { CHART_INK } from "../../lib/colors";
import { noTranscriptReason } from "../../lib/transcriptReason";
import { setTheme } from "../../state/theme";
import { renderWithProviders } from "../../test/render";
import { CallCards, CallsTable } from "./CallsTable";

const base = toCall(CALLS[0] as (typeof CALLS)[number]);

function call(overrides: Partial<Call>): Call {
  return {
    ...base,
    transcript_text: null,
    transcript_quality: null,
    transcript_status: "done",
    encrypted: false,
    duplicate_of: null,
    emergency: false,
    ...overrides,
  };
}

function renderTable(calls: Call[], selectedId: number | null = null) {
  renderWithProviders(
    <div style={{ height: 800 }}>
      <CallsTable
        calls={calls}
        selectedId={selectedId}
        onSelect={vi.fn()}
        sort="-started_at"
        onSortChange={vi.fn()}
        canWrite
        onToggleStar={vi.fn()}
        transcriptRow
      />
    </div>,
  );
}

afterEach(() => {
  setTheme("light");
  try {
    localStorage.removeItem("keyup-theme");
  } catch {
    /* no storage */
  }
});

describe("why a call has no transcript", () => {
  it("names the reason from the call's own fields", () => {
    expect(noTranscriptReason(call({ encrypted: true, transcript_status: "skipped" }))).toBe("encrypted");
    expect(noTranscriptReason(call({ transcript_status: "empty", transcript_quality: "noise" }))).toBe(
      "no speech",
    );
    expect(noTranscriptReason(call({ transcript_status: "done" }))).toBe("no speech");
    expect(noTranscriptReason(call({ transcript_status: "skipped" }))).toBe("not transcribed");
    expect(noTranscriptReason(call({ transcript_status: "skipped", duplicate_of: 7 }))).toBe("duplicate");
    expect(noTranscriptReason(call({ transcript_status: "queued" }))).toBe("queued");
    expect(noTranscriptReason(call({ transcript_status: "running" }))).toBe("transcribing");
    expect(noTranscriptReason(call({ transcript_status: "failed" }))).toBe("transcription failed");
  });

  it("shows a quiet italic reason and a neutral glyph instead of the raw status", () => {
    renderTable([call({ id: 1, encrypted: true, transcript_status: "skipped" })]);
    const list = screen.getByRole("list", { name: "Calls" });
    expect(within(list).queryByText("skipped")).not.toBeInTheDocument();
    expect(within(list).queryByText(/No transcript \(/)).not.toBeInTheDocument();
    for (const reason of within(list).getAllByText("encrypted", { selector: "span.italic" })) {
      expect(reason).toHaveClass("italic", "text-muted");
    }
    const glyph = screen.getByText("No transcript: encrypted").parentElement as HTMLElement;
    expect(glyph).toHaveAttribute("data-reason", "encrypted");
    expect(glyph).toHaveClass("text-muted");
    expect(glyph.className).not.toMatch(/critical/);
  });

  it("keeps the red mark for a transcript the archive judged to be noise", () => {
    renderTable([call({ id: 2, transcript_status: "empty", transcript_quality: "noise" })]);
    expect(screen.getAllByText("no speech", { selector: "span.italic" }).length).toBeGreaterThan(0);
    const mark = screen.getByText("Transcript quality: noise").parentElement as HTMLElement;
    expect(mark).toHaveClass("text-critical-ink");
  });

  it("does the same on the phone cards", () => {
    renderWithProviders(
      <CallCards
        calls={[call({ id: 3, transcript_status: "skipped" })]}
        selectedId={null}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("not transcribed", { selector: "span.italic" })).toBeInTheDocument();
    expect(screen.queryByText(/skipped/)).not.toBeInTheDocument();
  });
});

describe("row colours", () => {
  // A talkgroup's raw colour is no longer drawn on the row (it was a dot, clamped here);
  // the key-up edge carries the category's hue instead, clamped the same way.
  it("ignore the talkgroup's raw colour and clear 3:1 on the dark surface", () => {
    setTheme("dark");
    renderTable([call({ id: 4, color: "#0000ff", category: "law" })]);
    const shown = document.querySelector(".key-edge")?.getAttribute("data-color") ?? "";
    expect(shown).not.toBe("#0000ff");
    expect(contrastRatio(shown, CHART_INK.dark.surface)).toBeGreaterThanOrEqual(3);
  });

  it("clear 3:1 on the light surface, even for the amber public-works hue", () => {
    setTheme("light");
    renderTable([call({ id: 5, color: "#f5e663", category: "pw" })]);
    const shown = document.querySelector(".key-edge")?.getAttribute("data-color") ?? "";
    expect(contrastRatio(shown, CHART_INK.light.surface)).toBeGreaterThanOrEqual(3);
  });
});

describe("selected rows", () => {
  it("mark themselves so their muted text is re-pointed to --ink-2", () => {
    renderTable([call({ id: 6 }), call({ id: 7 })], 6);
    const selected = document.querySelectorAll('[data-selected="true"]');
    expect(selected).toHaveLength(1);
    expect(selected[0]).toHaveClass("bg-accent-soft");
  });

  it("on the cards too", () => {
    renderWithProviders(<CallCards calls={[call({ id: 8 })]} selectedId={8} onSelect={vi.fn()} />);
    expect(document.querySelector('[data-selected="true"]')).toHaveClass("bg-accent-soft");
  });
});

describe("24 px targets", () => {
  it("the card's open-detail link is 24 px square", () => {
    renderWithProviders(<CallCards calls={[call({ id: 9 })]} selectedId={null} onSelect={vi.fn()} />);
    const link = screen.getByRole("link", { name: /Open the detail page/ });
    expect(link).toHaveClass("size-6", "inline-flex");
    expect(link.className).not.toMatch(/\bp-1\b/);
  });

  it("the quality mark carries a 24 px hit area", () => {
    renderTable([call({ id: 10, transcript_text: "engine 3", transcript_quality: "good" })]);
    const mark = screen.getByText("Transcript quality: good").parentElement as HTMLElement;
    expect(mark).toHaveClass("relative", "before:absolute", "before:size-6");
  });
});

describe("the key-up edge", () => {
  it("is in the channel's category colour when the talkgroup has none of its own", () => {
    setTheme("light");
    renderTable([call({ id: 11, color: null, category: "fire" })]);
    const edge = document.querySelector<HTMLElement>(".key-edge");
    const shown = edge?.getAttribute("data-color") ?? "";
    expect(edge?.style.getPropertyValue("--edge")).toBe(shown);
    expect(contrastRatio(shown, CHART_INK.light.surface)).toBeGreaterThanOrEqual(3);
  });

  it("marks only the playing row and the fresh arrivals", () => {
    renderWithProviders(
      <div style={{ height: 800 }}>
        <CallsTable
          calls={[call({ id: 21 }), call({ id: 22 }), call({ id: 23 })]}
          selectedId={22}
          playingId={22}
          freshIds={new Set([21])}
          onSelect={vi.fn()}
          sort="-started_at"
          onSortChange={vi.fn()}
          canWrite
          onToggleStar={vi.fn()}
        />
      </div>,
    );
    const edges = [...document.querySelectorAll<HTMLElement>(".key-edge")];
    expect(edges).toHaveLength(3);
    expect(edges.map((edge) => edge.dataset.playing ?? "-")).toEqual(["-", "true", "-"]);
    expect(edges.map((edge) => edge.dataset.fresh ?? "-")).toEqual(["true", "-", "-"]);
  });

  it("on the phone cards too", () => {
    renderWithProviders(
      <CallCards
        calls={[call({ id: 31 }), call({ id: 32 })]}
        selectedId={32}
        playingId={32}
        freshIds={new Set([31])}
        onSelect={vi.fn()}
      />,
    );
    const edges = [...document.querySelectorAll<HTMLElement>(".key-edge")];
    expect(edges.map((edge) => edge.dataset.playing ?? "-")).toEqual(["-", "true"]);
    expect(edges.map((edge) => edge.dataset.fresh ?? "-")).toEqual(["true", "-"]);
  });

  it("is critical red, not the channel colour, on an emergency", () => {
    renderTable([call({ id: 41, emergency: true })]);
    expect(document.querySelector(".key-edge")).toHaveAttribute("data-emergency", "true");
  });
});
