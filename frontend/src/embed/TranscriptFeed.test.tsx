import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RecentAnswer, TranscriptRow } from "./live";
import { defaultEmbedOptions, embedSearch, embedUrl, parseEmbedParams } from "./params";
import { TranscriptFeed } from "./TranscriptFeed";

/** A WebSocket that does nothing until the test says so. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  emit(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }
}

function row(
  id: number,
  clock: string,
  text: string | null,
  extra: Partial<TranscriptRow> = {},
): TranscriptRow {
  return {
    id,
    started_at: `2026-09-22T${clock}Z`,
    started_local: `2026-09-22T${clock}`,
    duration_s: 7.4,
    tgid: 1101,
    talkgroup_alias: "EC EVENT 6",
    radio_label: "E1",
    transcript_text: text,
    transcript_status: text ? "done" : "queued",
    transcript_quality: text ? "good" : null,
    ...extra,
  };
}

const ANSWER: RecentAnswer = {
  minutes: 15,
  since: "2026-09-22T18:20:00Z",
  preset: "emergency",
  filter: { category: ["law", "fire", "ems", "multi", "interop"] },
  items: [
    row(102, "18:34:10", "Engine 1 on scene, nothing showing."),
    row(101, "18:30:05", "Medic 4 responding.", { tgid: 1001, talkgroup_alias: "EC V-LAW 31+" }),
  ],
  presets: [
    { slug: "all", name: "All calls" },
    { slug: "emergency", name: "Emergency dispatch" },
    { slug: "fire", name: "Fire" },
  ],
};

let fetchMock: ReturnType<typeof vi.fn>;

function answerWith(body: unknown, status = 200) {
  fetchMock.mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => body,
  });
}

beforeEach(() => {
  FakeSocket.instances = [];
  fetchMock = vi.fn();
  answerWith(ANSWER);
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const socket = async () => {
  await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0));
  return FakeSocket.instances.at(-1) as FakeSocket;
};

/** The default view, loaded and tailing. */
const openedFeed = async () => {
  render(<TranscriptFeed initial={defaultEmbedOptions()} />);
  await screen.findByText("Medic 4 responding.");
  const ws = await socket();
  act(() => ws.open());
  return ws;
};

const listed = () =>
  within(screen.getByRole("list", { name: /Transcribed calls/ }))
    .queryAllByRole("listitem")
    .map((item) => item.getAttribute("data-id"))
    // The day dividers and the "Load older" item are list items too; only calls have an id.
    .filter((id) => id !== null);

describe("the embeddable live transcript view", () => {
  it("loads the last fifteen minutes of the Emergency dispatch feed by default", async () => {
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);

    expect(await screen.findByText("Engine 1 on scene, nothing showing.")).toBeInTheDocument();
    expect(listed()).toEqual(["102", "101"]);
    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain("/api/transcripts/recent?");
    expect(url).toContain("preset=emergency");
    expect(url).toContain("minutes=15");
    expect(url).toContain("presets=true");

    const first = screen.getAllByRole("listitem")[0] as HTMLElement;
    expect(within(first).getByText("18:34:10")).toBeInTheDocument();
    expect(within(first).getByText("EC EVENT 6")).toHaveAttribute("title", "Talkgroup 1101");
    expect(within(first).getByText("7s")).toBeInTheDocument();
    expect(screen.getByText(/Emergency dispatch · last 15 min/)).toBeInTheDocument();
  });

  it("is one main landmark whose list holds only list items", async () => {
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText("Medic 4 responding.");
    const main = screen.getByRole("main");
    expect(within(main).getByRole("heading", { level: 1, name: "Live transcripts" })).toBeInTheDocument();
    const list = within(main).getByRole("list", { name: /Transcribed calls/ });
    for (const child of Array.from(list.children)) {
      expect(child.tagName).toBe("LI");
      expect(child).not.toHaveAttribute("role");
    }
  });

  it("links every row to the full call with an absolute URL that opens outside the frame", async () => {
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText("Medic 4 responding.");
    const links = screen.getAllByRole("link", { name: /Open the full call/ });
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      `${window.location.origin}/calls/102`,
      `${window.location.origin}/calls/101`,
    ]);
    for (const link of links) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener");
    }
  });

  it("tails the live feed with the filter the server resolved, and inserts a transcript that lands", async () => {
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText("Medic 4 responding.");
    const ws = await socket();
    expect(ws.url).toBe(`ws://${window.location.host}/api/live`);
    act(() => ws.open());
    expect(JSON.parse(ws.sent[0] as string)).toEqual(ANSWER.filter);
    expect(screen.getByRole("status")).toHaveTextContent("Live");

    // A call that has only just been recorded is not news yet...
    act(() => ws.emit({ type: "call.new", call: row(103, "18:35:00", null) }));
    expect(listed()).toEqual(["102", "101"]);
    // ...its transcript is, and it goes in at the top.
    act(() => ws.emit({ type: "call.transcribed", call: row(103, "18:35:00", "Command, second alarm.") }));
    expect(listed()).toEqual(["103", "102", "101"]);
    expect(screen.getByText("Command, second alarm.")).toBeInTheDocument();
    // A transcript for an earlier call slots in by its start time, not its arrival.
    act(() => ws.emit({ type: "call.transcribed", call: row(104, "18:32:00", "Copy.") }));
    expect(listed()).toEqual(["103", "102", "104", "101"]);
    // A rejected transcript never shows.
    act(() =>
      ws.emit({
        type: "call.transcribed",
        call: row(105, "18:36:00", null, { transcript_status: "empty", transcript_quality: "noise" }),
      }),
    );
    expect(listed()).toEqual(["103", "102", "104", "101"]);
  });

  it("ignores a transcript landing for a call older than the window it opened with", async () => {
    // The backlog transcribes months-old calls continuously; each arrives as call.transcribed.
    const ws = await openedFeed();
    const old = row(90, "11:02:00", "An old backlog call.", { started_at: "2024-10-21T21:04:46.763250Z" });
    act(() => ws.emit({ type: "call.transcribed", call: old }));
    act(() => ws.emit({ type: "call.transcribed", call: row(95, "18:19:59", "Just before the window.") }));
    expect(screen.queryByText("An old backlog call.")).toBeNull();
    expect(screen.queryByText("Just before the window.")).toBeNull();
    // An edit to a row already shown still lands, whatever its age.
    act(() => ws.emit({ type: "call.updated", call: row(101, "18:30:05", "Medic 4, correction.") }));
    expect(screen.getByText("Medic 4, correction.")).toBeInTheDocument();
  });

  it("orders by instant, not by how the timestamp is spelt", async () => {
    const ws = await openedFeed();
    act(() =>
      ws.emit({
        type: "call.transcribed",
        call: row(103, "18:34:11", "One second after 102.", { started_at: "2026-09-22T18:34:11+00:00" }),
      }),
    );
    expect(listed()).toEqual(["103", "102", "101"]);
  });

  it("updates an edited call in place and ignores edits to calls it never showed", async () => {
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText("Medic 4 responding.");
    const ws = await socket();
    act(() => ws.open());
    act(() => ws.emit({ type: "call.updated", call: row(101, "18:30:05", "Medic 4 responding, code 3.") }));
    expect(screen.getByText("Medic 4 responding, code 3.")).toBeInTheDocument();
    expect(screen.queryByText("Medic 4 responding.")).not.toBeInTheDocument();
    act(() => ws.emit({ type: "call.updated", call: row(99, "18:31:00", "Somebody else's edit.") }));
    expect(listed()).toEqual(["102", "101"]);
  });

  it("drops a row reconciliation demoted to a duplicate", async () => {
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText("Medic 4 responding.");
    const ws = await socket();
    act(() => ws.open());
    act(() => ws.emit({ type: "call.superseded", id: 102, by: 101 }));
    expect(listed()).toEqual(["101"]);
    expect(screen.queryByText("Engine 1 on scene, nothing showing.")).not.toBeInTheDocument();
  });

  it("shows the filter bar by default and hides it with controls=0", async () => {
    const { unmount } = render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText("Medic 4 responding.");
    expect(screen.getByRole("combobox", { name: "Feed" })).toHaveValue("emergency");
    expect(screen.getByRole("textbox", { name: /Add a talkgroup/ })).toBeInTheDocument();
    unmount();

    fetchMock.mockClear();
    render(<TranscriptFeed initial={parseEmbedParams("?controls=0")} />);
    await screen.findByText("Medic 4 responding.");
    expect(screen.queryByRole("combobox", { name: "Feed" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: /Filter the transcripts/ })).not.toBeInTheDocument();
    expect(String(fetchMock.mock.calls[0]?.[0])).not.toContain("presets=true");
    // The status dot stays: an embed still says whether it is live.
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("refilters from the bar: a number is a talkgroup, a word is a category", async () => {
    const user = userEvent.setup();
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText("Medic 4 responding.");
    await user.type(screen.getByRole("textbox", { name: /Add a talkgroup/ }), "1101{Enter}");
    await waitFor(() => expect(String(fetchMock.mock.calls.at(-1)?.[0])).toContain("talkgroup=1101"));
    expect(screen.getByText("TG 1101")).toBeInTheDocument();
    expect(window.location.search).toContain("talkgroup=1101");
    await user.click(screen.getByRole("button", { name: "Remove TG 1101" }));
    await waitFor(() => expect(String(fetchMock.mock.calls.at(-1)?.[0])).not.toContain("talkgroup="));
  });

  it("tells a guest when the archive is closed to them", async () => {
    answerWith({ detail: "Sign in to read this archive" }, 401);
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    expect(await screen.findByText(/This archive is closed to guests/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in to Keyup" })).toHaveAttribute(
      "href",
      `${window.location.origin}/login`,
    );
    expect(FakeSocket.instances).toHaveLength(0);
  });
});

describe("scrolling past the first screen", () => {
  /** First screen from ANSWER with a cursor; a cursor request answers with `older`. */
  function withOlder(older: TranscriptRow[], nextCursor: string | null) {
    fetchMock.mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      statusText: "",
      json: async () =>
        url.includes("cursor=")
          ? { ...ANSWER, items: older, next_cursor: nextCursor, presets: null }
          : { ...ANSWER, next_cursor: "Y3Vyc29y" },
    }));
  }

  it("reads the next page of older calls from the end of the list, with the cursor", async () => {
    withOlder([row(88, "18:02:00", "Twenty minutes ago."), row(87, "17:55:00", "Older still.")], "bmV4dA");
    await openedFeed();
    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    await waitFor(() => expect(listed()).toEqual(["102", "101", "88", "87"]));
    const cursorCall = fetchMock.mock.calls.find(([url]) => String(url).includes("cursor="));
    expect(String(cursorCall?.[0])).toContain("cursor=Y3Vyc29y");
    // Older pages are not news: no pill, no highlight.
    expect(screen.queryByRole("button", { name: /new/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Load older calls" })).toBeInTheDocument();
  });

  it("says when the archive has run out", async () => {
    withOlder([row(88, "18:02:00", "The last one.")], null);
    await openedFeed();
    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    await screen.findByText("No older calls");
    expect(screen.queryByRole("button", { name: "Load older calls" })).toBeNull();
  });

  it("marks where the day changes going down", async () => {
    const yesterday = row(80, "23:59:00", "Late last night.", {
      started_at: "2026-09-21T23:59:00Z",
      started_local: "2026-09-21T23:59:00",
    });
    withOlder([yesterday], null);
    await openedFeed();
    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    await screen.findByText("Late last night.");
    const divider = document.querySelector(".kt-day");
    expect(divider?.textContent).toMatch(/21/);
    expect(listed()).toEqual(["102", "101", "80"]);
  });

  it("reads into the past from an empty first screen", async () => {
    fetchMock.mockImplementation(async (url: string) => ({
      ok: true,
      status: 200,
      statusText: "",
      json: async () =>
        url.includes("cursor=")
          ? { ...ANSWER, items: [row(70, "17:10:00", "An hour ago.")], next_cursor: null, presets: null }
          : { ...ANSWER, items: [], next_cursor: "c2luY2U" },
    }));
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    await screen.findByText(/No transcribed calls in the last 15 minutes/);
    await userEvent.click(screen.getByRole("button", { name: "Load older calls" }));
    await screen.findByText("An hour ago.");
    expect(screen.getByText("No older calls")).toBeInTheDocument();
  });

  it("offers no end row before the first screen answers", () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    render(<TranscriptFeed initial={defaultEmbedOptions()} />);
    expect(screen.queryByRole("button", { name: "Load older calls" })).toBeNull();
    expect(screen.queryByText("No older calls")).toBeNull();
  });
});

describe("the embed URL", () => {
  it("defaults to the Emergency dispatch feed, fifteen minutes, controls on", () => {
    expect(defaultEmbedOptions()).toMatchObject({
      preset: "emergency",
      minutes: 15,
      controls: true,
      theme: "auto",
      compact: false,
      limit: 100,
      showPending: false,
    });
  });

  it("lets explicit filters replace the default feed, and bounds the numbers", () => {
    const options = parseEmbedParams("?talkgroup=1101,1102&category=fire&minutes=999&limit=0&theme=dark");
    expect(options.preset).toBeNull();
    expect(options.filters).toEqual({ talkgroup: ["1101", "1102"], category: ["fire"] });
    expect(options.minutes).toBe(240);
    expect(options.limit).toBe(100);
    expect(options.theme).toBe("dark");
  });

  it("writes only what differs from the defaults, and reads back the same options", () => {
    expect(embedSearch(defaultEmbedOptions())).toBe("");
    const custom = parseEmbedParams("?preset=fire&minutes=30&controls=0&compact=1&theme=light");
    const search = embedSearch(custom);
    expect(search).toBe("?preset=fire&minutes=30&controls=0&theme=light&compact=1");
    expect(parseEmbedParams(search)).toEqual(custom);
    expect(embedUrl("https://radio.example.org/", custom)).toBe(
      `https://radio.example.org/embed/transcripts${search}`,
    );
    // "No feed, no filters" survives the round trip as `all`, not as the default feed.
    expect(embedSearch({ ...defaultEmbedOptions(), preset: null })).toBe("?preset=all");
  });
});
