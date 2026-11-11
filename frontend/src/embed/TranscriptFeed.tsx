/**
 * `/embed/transcripts` — a live list of transcribed calls, small enough to sit in somebody
 * else's page (docs/EMBED.md).
 *
 * The first screen is the last few minutes of the filter, read from
 * `GET /api/transcripts/recent`; from then on the list is kept live over `WS /api/live`
 * with the filter that answer resolved. A row appears when its transcript lands, changes
 * in place when somebody edits the call, and goes when reconciliation demotes it to a
 * duplicate. A reader who has scrolled down is not moved by rows arriving above: the row
 * they were reading stays where it was and a pill counts what is new. Scrolling to the end
 * reads on into older calls, a page at a time, past the window the first screen covered.
 *
 * No router, no query cache, no app shell and no help registry: this is its own Vite entry
 * and everything it needs is in this folder.
 */
import { type FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  connectLive,
  fetchRecent,
  type LiveFrame,
  type LiveState,
  type PresetChoice,
  RecentError,
  type TranscriptRow,
} from "./live";
import { type EmbedOptions, embedSearch, FILTER_KEYS, type FilterKey, recentQuery } from "./params";

/** A finished transcript that says something — the server's rule for the first screen. */
export function displayable(row: TranscriptRow): boolean {
  return (
    row.transcript_status === "done" &&
    (row.transcript_text ?? "").trim() !== "" &&
    row.transcript_quality !== "noise"
  );
}

/** Just the fields a row prints, from a live frame's full call object. */
function rowOf(call: TranscriptRow): TranscriptRow {
  return {
    id: call.id,
    started_at: call.started_at,
    started_local: call.started_local,
    duration_s: Number(call.duration_s) || 0,
    tgid: call.tgid ?? null,
    talkgroup_alias: call.talkgroup_alias ?? null,
    radio_label: call.radio_label ?? null,
    transcript_text: call.transcript_text ?? null,
    transcript_status: call.transcript_status,
    transcript_quality: call.transcript_quality ?? null,
  };
}

/** Newest first; `id` breaks a tie the way the server's ORDER BY does. Compared as instants,
 * not strings, so two spellings of a timestamp (`Z`/`+00:00`, 3 or 6 decimals) still agree. */
function newerFirst(a: TranscriptRow, b: TranscriptRow): number {
  const difference = Date.parse(b.started_at) - Date.parse(a.started_at);
  return difference !== 0 && !Number.isNaN(difference) ? difference : b.id - a.id;
}

/** `Tue 22 Sep` for a day divider, from the archive's local date (`started_local`). */
function dayLabel(row: TranscriptRow): string {
  const [year, month, day] = row.started_local.slice(0, 10).split("-").map(Number);
  const date = new Date(Date.UTC(year ?? 1970, (month ?? 1) - 1, day ?? 1));
  return date.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** The keyset of a row, as the API spells a cursor: base64url of `<started_at>,<id>`. */
function cursorOf(row: TranscriptRow): string {
  return btoa(`${row.started_at},${row.id}`).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** `18:35:12` — the archive's local wall clock, which is what the rest of Keyup prints. */
function clock(row: TranscriptRow): string {
  return row.started_local.slice(11, 19);
}

function duration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  return whole < 60 ? `${whole}s` : `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function talkgroupLabel(row: TranscriptRow): string {
  return row.talkgroup_alias ?? (row.tgid !== null ? `TG ${row.tgid}` : "Private call");
}

function chipLabel(key: FilterKey, value: string): string {
  if (key === "talkgroup") return `TG ${value}`;
  if (key === "category") return value;
  if (key === "q") return `“${value}”`;
  return `${key}: ${value}`;
}

/**
 * A failed load in the reader's words. The server's raw text (a 500's exception string)
 * stays in the sentence's `title`, not in the sentence.
 */
export function embedErrorText(error: Error): string {
  if (error instanceof RecentError) {
    if (error.status === 404 || error.status === 410) return "This view is no longer available.";
    if (error.status === 403) return "This account cannot see these transcripts.";
    if (error.status === 429) return "Too many requests; it will try again shortly.";
    if (error.status >= 500) return "The server ran into a problem; try again in a moment.";
    return "The server did not accept the request.";
  }
  return "Keyup could not be reached; check the connection.";
}

const STATE_LABEL: Record<LiveState | "error", string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  error: "Offline",
};

/** How close to the top still counts as "reading the newest", in pixels. */
const TOP_SLACK_PX = 8;
/** How far ahead of the end the next page starts loading, in pixels. */
const LOAD_AHEAD_PX = 400;
/**
 * The most rows the page holds. Past it the oldest go (and the cursor moves up to the new
 * last row, so scrolling down reads them again) — a page left open on a busy feed all day
 * must not grow without bound.
 */
const MAX_HELD = 1000;

export function TranscriptFeed({ initial }: { initial: EmbedOptions }) {
  const [options, setOptions] = useState(initial);
  const [rows, setRows] = useState<TranscriptRow[]>([]);
  const [state, setState] = useState<LiveState | "error">("connecting");
  const [error, setError] = useState<RecentError | Error | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [presets, setPresets] = useState<PresetChoice[]>([]);
  const [newCount, setNewCount] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [draft, setDraft] = useState("");
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [hasOlder, setHasOlder] = useState(false);

  const rowsRef = useRef<TranscriptRow[]>([]);
  const fresh = useRef(new Set<number>());
  const listRef = useRef<HTMLOListElement>(null);
  /**
   * The start of the window the first screen was read for (`since`, epoch ms). A call that
   * started before it is not news however recently its transcript landed: the backlog
   * transcribes months-old calls all day, and each one used to be slotted in at the bottom
   * of the list — rows popping in under a scrolling reader, dated only by a time of day.
   */
  const windowStart = useRef(Number.NEGATIVE_INFINITY);
  /** The row a scrolled reader is looking at, and how far below the top edge it sits. */
  const anchor = useRef<{ id: number; offset: number } | null>(null);
  /** Where the next page of older rows starts; null once the archive has run out. */
  const olderCursor = useRef<string | null>(null);
  const olderInFlight = useRef(false);
  const endRef = useRef<HTMLLIElement>(null);

  const origin = window.location.origin;
  const showPending = options.showPending;
  // Only what changes the *data* reconnects; theme and density are CSS.
  const query = recentQuery(options, options.controls);

  const commit = useCallback((next: TranscriptRow[], inserted: number) => {
    if (next.length > MAX_HELD) {
      next = next.slice(0, MAX_HELD);
      const last = next[next.length - 1];
      if (last) {
        olderCursor.current = cursorOf(last);
        setHasOlder(true);
      }
    }
    rowsRef.current = next;
    setRows(next);
    if (inserted > 0 && anchor.current !== null) setNewCount((count) => count + inserted);
  }, []);

  const apply = useCallback(
    (frame: LiveFrame) => {
      const current = rowsRef.current;
      if (frame.type === "call.superseded") {
        if (current.some((row) => row.id === frame.id)) {
          commit(
            current.filter((row) => row.id !== frame.id),
            0,
          );
        }
        return;
      }
      const row = rowOf(frame.call);
      const index = current.findIndex((existing) => existing.id === row.id);
      const wanted = showPending || displayable(row);
      if (index >= 0) {
        // In place: an edit, or a pending row whose words have landed.
        commit(
          wanted
            ? current.map((existing) => (existing.id === row.id ? row : existing))
            : current.filter((existing) => existing.id !== row.id),
          0,
        );
        return;
      }
      // An edit to a call this list never showed is not news; a new call is, only when
      // pending rows were asked for; a transcript landing always is.
      if (!wanted || frame.type === "call.updated") return;
      if (Date.parse(row.started_at) < windowStart.current) return;
      fresh.current.add(row.id);
      commit([...current, row].sort(newerFirst), 1);
    },
    [commit, showPending],
  );

  // Load the window, then tail it. Re-run whenever the filter changes (or on Retry).
  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` is the retry trigger
  useEffect(() => {
    const abort = new AbortController();
    let stop = () => {};
    let cancelled = false;
    setError(null);
    setLoaded(false);
    setState("connecting");
    fresh.current.clear();
    olderCursor.current = null;
    olderInFlight.current = false;
    setHasOlder(false);
    setLoadingOlder(false);

    const refill = async () => {
      // A stream has no replay: after a drop, whatever was transcribed meanwhile is read back.
      try {
        const answer = await fetchRecent(query, abort.signal);
        if (cancelled) return;
        const known = new Set(rowsRef.current.map((row) => row.id));
        const merged = new Map(rowsRef.current.map((row) => [row.id, row]));
        let added = 0;
        for (const item of answer.items) {
          if (!known.has(item.id)) {
            fresh.current.add(item.id);
            added += 1;
          }
          merged.set(item.id, item);
        }
        commit([...merged.values()].sort(newerFirst), added);
      } catch {
        /* the socket is back; a missed refill is not worth an error banner */
      }
    };

    fetchRecent(query, abort.signal)
      .then((answer) => {
        if (cancelled) return;
        if (answer.presets) setPresets(answer.presets);
        const since = Date.parse(answer.since);
        windowStart.current = Number.isNaN(since) ? Number.NEGATIVE_INFINITY : since;
        olderCursor.current = answer.next_cursor ?? null;
        setHasOlder(olderCursor.current !== null);
        commit(answer.items, 0);
        setLoaded(true);
        stop = connectLive(answer.filter, {
          onFrame: apply,
          onState: setState,
          onResume: () => void refill(),
        });
      })
      .catch((failure: unknown) => {
        if (cancelled) return;
        setError(failure instanceof Error ? failure : new Error(String(failure)));
        setState("error");
      });

    return () => {
      cancelled = true;
      abort.abort();
      stop();
    };
  }, [query, attempt, apply, commit]);

  /** The next page of older rows, appended below; never counted or highlighted as new. */
  const loadOlder = useCallback(async () => {
    const cursor = olderCursor.current;
    if (cursor === null || olderInFlight.current) return;
    olderInFlight.current = true;
    setLoadingOlder(true);
    try {
      const answer = await fetchRecent(`${query}&cursor=${encodeURIComponent(cursor)}`);
      // The filter changed (or the list was reset) while this was in flight.
      if (olderCursor.current !== cursor) return;
      const known = new Set(rowsRef.current.map((row) => row.id));
      const older = answer.items.filter((row) => !known.has(row.id));
      olderCursor.current = answer.next_cursor ?? null;
      setHasOlder(olderCursor.current !== null);
      commit([...rowsRef.current, ...older].sort(newerFirst), 0);
    } catch {
      /* the end row stays a button: the reader can ask again */
    } finally {
      olderInFlight.current = false;
      setLoadingOlder(false);
    }
  }, [query, commit]);

  // The end of the list coming into view is the reader reaching it: read the next page.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `rows` re-arms the observer — a short page leaves the end row in view, and an observer only reports changes, so without a fresh one the next page would never be asked for
  useEffect(() => {
    const list = listRef.current;
    const end = endRef.current;
    if (!list || !end || !hasOlder || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadOlder();
      },
      { root: list, rootMargin: `0px 0px ${LOAD_AHEAD_PX}px 0px` },
    );
    observer.observe(end);
    return () => observer.disconnect();
  }, [hasOlder, loadOlder, rows]);

  // Keep the row a scrolled reader is on exactly where it was when rows land above it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: runs after every change to `rows`
  useLayoutEffect(() => {
    const list = listRef.current;
    const held = anchor.current;
    if (!list || !held) return;
    const element = list.querySelector<HTMLElement>(`[data-id="${held.id}"]`);
    if (element) list.scrollTop = element.offsetTop - held.offset;
  }, [rows]);

  const onScroll = () => {
    const list = listRef.current;
    if (!list) return;
    if (list.scrollTop <= TOP_SLACK_PX) {
      anchor.current = null;
      setNewCount(0);
      return;
    }
    for (const element of list.querySelectorAll<HTMLElement>("[data-id]")) {
      if (element.offsetTop + element.offsetHeight > list.scrollTop) {
        anchor.current = { id: Number(element.dataset.id), offset: element.offsetTop - list.scrollTop };
        return;
      }
    }
  };

  const toTop = () => {
    anchor.current = null;
    setNewCount(0);
    listRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  };

  const change = (next: EmbedOptions) => {
    setOptions(next);
    try {
      window.history.replaceState(null, "", `${window.location.pathname}${embedSearch(next)}`);
    } catch {
      /* a sandboxed frame may refuse; the list still follows the new filter */
    }
  };

  const setFilter = (key: FilterKey, values: string[]) => {
    const filters = { ...options.filters };
    if (values.length > 0) filters[key] = values;
    else delete filters[key];
    change({ ...options, filters });
  };

  const addChip = (event: FormEvent) => {
    event.preventDefault();
    const value = draft.trim();
    if (!value) return;
    const key: FilterKey = /^\d+$/.test(value) ? "talkgroup" : "category";
    const normalized = key === "category" ? value.toLowerCase() : value;
    const existing = options.filters[key] ?? [];
    if (!existing.includes(normalized)) setFilter(key, [...existing, normalized]);
    setDraft("");
  };

  const presetName =
    presets.find((preset) => preset.slug === options.preset)?.name ?? options.preset ?? "Custom filter";
  const choices =
    options.preset && !presets.some((preset) => preset.slug === options.preset)
      ? [...presets, { slug: options.preset, name: options.preset }]
      : presets;
  const chips = FILTER_KEYS.flatMap((key) => (options.filters[key] ?? []).map((value) => ({ key, value })));

  return (
    // The page's one landmark: everything an embedding page frames is this view.
    <main className={`kt${options.compact ? " kt-compact" : ""}`}>
      {/* The page's one heading, for screen readers: the visible head is a status line. */}
      <h1 className="kt-sr-only">Live transcripts</h1>
      <header className="kt-head">
        <span className={`kt-dot kt-dot-${state}`} aria-hidden="true" />
        <span className="kt-state" role="status">
          {STATE_LABEL[state]}
        </span>
        <span className="kt-caption">
          {presetName} · last {options.minutes} min
        </span>
      </header>

      {options.controls ? (
        <form className="kt-bar" onSubmit={addChip} aria-label="Filter the transcripts">
          <select
            className="kt-select"
            aria-label="Feed"
            value={options.preset ?? ""}
            onChange={(event) => change({ ...options, preset: event.target.value || null })}
          >
            <option value="">Filters only</option>
            {choices.map((preset) => (
              <option key={preset.slug} value={preset.slug}>
                {preset.name}
              </option>
            ))}
          </select>
          {chips.map(({ key, value }) => (
            <span key={`${key}:${value}`} className="kt-chip">
              {chipLabel(key, value)}
              <button
                type="button"
                aria-label={`Remove ${chipLabel(key, value)}`}
                onClick={() =>
                  setFilter(
                    key,
                    (options.filters[key] ?? []).filter((item) => item !== value),
                  )
                }
              >
                ×
              </button>
            </span>
          ))}
          <input
            className="kt-input"
            aria-label="Add a talkgroup number or a category"
            placeholder="+ talkgroup or category"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
        </form>
      ) : null}

      {newCount > 0 ? (
        <button type="button" className="kt-pill" onClick={toTop}>
          {newCount} new
        </button>
      ) : null}

      {error ? (
        <div className="kt-empty" role="alert">
          {error instanceof RecentError && error.status === 401 ? (
            <p>
              This archive is closed to guests.{" "}
              <a href={`${origin}/login`} target="_blank" rel="noopener">
                Sign in to Keyup
              </a>{" "}
              to see its transcripts.
            </p>
          ) : (
            <p>
              <span title={error.message}>Could not load the transcripts. {embedErrorText(error)}</span>{" "}
              <button type="button" className="kt-link" onClick={() => setAttempt((n) => n + 1)}>
                Retry
              </button>
            </p>
          )}
        </div>
      ) : loaded && rows.length === 0 ? (
        <p className="kt-empty">
          No transcribed calls in the last {options.minutes} minutes. New ones appear here as they are
          transcribed.
        </p>
      ) : null}

      <ol className="kt-list" ref={listRef} onScroll={onScroll} aria-label="Transcribed calls, newest first">
        {rows.flatMap((row, index) => {
          const previous = rows[index - 1];
          const day = row.started_local.slice(0, 10);
          const divider =
            previous !== undefined && previous.started_local.slice(0, 10) !== day ? (
              // A plain item: a list may hold only list items, and the date is worth reading.
              <li key={`day-${day}-${row.id}`} className="kt-day">
                {dayLabel(row)}
              </li>
            ) : null;
          return [
            divider,
            <li
              key={row.id}
              data-id={row.id}
              className={`kt-row${fresh.current.has(row.id) ? " kt-fresh" : ""}`}
            >
              <div className="kt-meta">
                <time dateTime={row.started_at} className="kt-time">
                  {clock(row)}
                </time>
                <span className="kt-tg" title={row.tgid !== null ? `Talkgroup ${row.tgid}` : undefined}>
                  {talkgroupLabel(row)}
                </span>
                <span className="kt-dur">{duration(row.duration_s)}</span>
                <a
                  className="kt-open"
                  href={`${origin}/calls/${row.id}`}
                  target="_blank"
                  rel="noopener"
                  aria-label={`Open the full call from ${clock(row)} on ${talkgroupLabel(row)}`}
                >
                  Full call ↗
                </a>
              </div>
              {displayable(row) ? (
                <p className="kt-text">{row.transcript_text}</p>
              ) : (
                <p className="kt-text kt-pending">
                  {row.transcript_status === "done" || row.transcript_status === "empty"
                    ? "No usable transcript"
                    : "Transcript pending…"}
                </p>
              )}
            </li>,
          ];
        })}
        {loaded && !error && (rows.length > 0 || hasOlder) ? (
          <li ref={endRef} className="kt-end">
            {hasOlder ? (
              <button
                type="button"
                className="kt-link"
                disabled={loadingOlder}
                onClick={() => void loadOlder()}
              >
                {loadingOlder ? "Loading older calls…" : "Load older calls"}
              </button>
            ) : (
              <span>No older calls</span>
            )}
          </li>
        ) : null}
      </ol>
    </main>
  );
}
