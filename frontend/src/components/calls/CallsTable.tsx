import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowDown,
  ArrowUp,
  Bell,
  ClipboardCopy,
  ExternalLink,
  Lock,
  Play,
  Siren,
  Star,
  VolumeX,
} from "lucide-react";
import { type CSSProperties, memo, type ReactNode, type RefObject, useEffect, useMemo, useRef } from "react";
import { Link } from "react-router";
import type { Call } from "../../api/types";
import { isBackfilled, isUnitCall } from "../../api/types";
import { HELP } from "../../help/help";
import { callSummary } from "../../lib/callSummary";
import { copyAndToast } from "../../lib/clipboard";
import { cn } from "../../lib/cn";
import { channelEdgeColor } from "../../lib/color";
import {
  callTalkgroupLabel,
  formatCallTime,
  formatDuration,
  formatFrequency,
  unitCallLabel,
} from "../../lib/format";
import { highlightTerms } from "../../lib/highlight";
import { noTranscriptReason } from "../../lib/transcriptReason";
import { useTheme } from "../../state/theme";
import { CategoryIcon } from "../ui/CategoryIcon";
import { QualityIcon } from "../ui/QualityIcon";
import { Tip } from "../ui/Tooltip";

export const ROW_HEIGHT = 40;

/** Every column the reader can choose to show, in the order they always appear. */
export const CALL_COLUMN_KEYS = [
  "time",
  "talkgroup",
  "radio",
  "source",
  "duration",
  "frequency",
  "flags",
  "quality",
  "ingest",
  "transcript",
] as const;

export type CallColumnKey = (typeof CALL_COLUMN_KEYS)[number];

/** The picker's checkbox labels — fuller than the terse column headers. */
export const CALL_COLUMN_LABELS: Record<CallColumnKey, string> = {
  time: "Time",
  talkgroup: "Talkgroup",
  radio: "Radio",
  source: "Source alias",
  duration: "Duration",
  frequency: "Frequency",
  flags: "Flags (encrypted, tones)",
  quality: "Quality",
  ingest: "Ingest source",
  transcript: "Transcript",
};

/** A sensible starting set: enough to scan and play, without the wide extras. */
export const DEFAULT_CALL_COLUMNS: CallColumnKey[] = [
  "time",
  "talkgroup",
  "radio",
  "duration",
  "flags",
  "quality",
  "transcript",
];

/**
 * The three single-icon columns (emergency, encrypted, tone) that were merged into
 * `flags`. A reader's saved column choice may still name them: any one of them means
 * they wanted the flags, and every other unknown key is dropped rather than crashing
 * the table on a `COLUMNS[key]` that no longer exists.
 */
const RETIRED_FLAG_KEYS = new Set(["emergency", "encrypted", "tone"]);

export function normalizeColumns(saved: readonly string[] | null | undefined): CallColumnKey[] {
  if (!Array.isArray(saved)) return DEFAULT_CALL_COLUMNS;
  const wanted = new Set(saved.map((key) => (RETIRED_FLAG_KEYS.has(key) ? "flags" : key)));
  const next = CALL_COLUMN_KEYS.filter((key) => wanted.has(key));
  return next.length > 0 ? next : DEFAULT_CALL_COLUMNS;
}

interface Column {
  key: CallColumnKey;
  /** The terse header label. */
  label: string;
  help: keyof typeof HELP;
  /** The header's tooltip when it says more than `HELP[help]` alone. */
  tip?: string;
  /** Sort key sent to the API; absent when the column cannot be sorted. */
  sort?: string;
  /** Width and alignment, shared by the header cell and the body cell so they line up. */
  className: string;
  /** The body cell's contents for one call. `quality` is rendered outside the row button. */
  cell: (call: Call, query?: string) => ReactNode;
}

/**
 * A muted "backfilled" tag on a call an archive migration pushed long after it happened.
 *
 * Deliberately quiet — grey text, no icon, no color: nothing is *wrong* with the row, and
 * a warning colour would say there was. It is here because a list sorted by time reads as
 * a timeline, and a 2022 recording that arrived on Tuesday is not part of the timeline the
 * reader thinks they are scanning.
 */
function BackfilledMark({ call }: { call: Call }) {
  if (!isBackfilled(call)) return null;
  return (
    <Tip label={HELP["calls.table.backfilled"]}>
      <span tabIndex={0} className="shrink-0 rounded bg-surface-2 px-1 text-[11px] text-muted">
        backfilled
      </span>
    </Tip>
  );
}

/**
 * The chip that marks a unit-to-unit (private) call.
 *
 * A private call has no talkgroup, so its row would otherwise be a blank where every other
 * row carries a channel name — and a blank reads as missing data rather than as a different
 * kind of traffic. The chip says which it is, and the `caller → callee` pair beside it is
 * the row's actual identity.
 *
 * Marked, not hidden: private traffic is part of the timeline a reader is scanning, and the
 * `kind` filter is there for anyone who wants only one or the other.
 */
function UnitCallChip() {
  return (
    <Tip label={HELP["calls.table.unitCall"]}>
      <span tabIndex={0} className="shrink-0 rounded bg-surface-2 px-1 text-[11px] text-ink-2">
        Unit call
      </span>
    </Tip>
  );
}

/** One mark in the merged flags column. */
interface CallFlag {
  key: "encrypted" | "page" | "tone" | "silent";
  /** What a screen reader hears, and what the hover title says. */
  label: string;
}

/**
 * The marks a row's flags column carries, in a fixed order. Emergency is not one of them:
 * it has its own gutter at the left edge of the row, where it tints the whole row.
 *
 * The column used to be three (Emg, Enc, Tone), each 32 px wide and empty on almost every
 * row; together they cost 96 px a transcript needed. One 48 px column holds the two that
 * can co-occur (encrypted and a tone) side by side.
 */
export function callFlags(call: Call): CallFlag[] {
  const flags: CallFlag[] = [];
  if (call.encrypted) flags.push({ key: "encrypted", label: "encrypted" });
  if (call.audio_meta?.silent === true) flags.push({ key: "silent", label: "silent call" });
  else if (call.tone_detected) {
    flags.push(call.alert === true ? { key: "page", label: "page" } : { key: "tone", label: "tone" });
  }
  return flags;
}

const FLAGS_CLASS = "flex w-12 shrink-0 items-center justify-center gap-1";

/**
 * A channel or talkgroup name: the typographic hero of a row, in the condensed
 * semibold so a long alias fits and reads as the thing a listener tracks.
 */
const CHANNEL_NAME = "truncate font-condensed text-[15px] font-semibold leading-5 text-ink";

/**
 * The row's key-up edge colour, as the custom property `.key-edge` reads (index.css).
 * Memoised on the two things it depends on, so a live update that hands a row the
 * same call costs nothing here.
 */
function useEdgeStyle(call: Call): { color: string; style: CSSProperties } {
  const theme = useTheme();
  const { category } = call;
  return useMemo(() => {
    const edge = channelEdgeColor(category, theme);
    return { color: edge, style: { "--edge": edge } as CSSProperties };
  }, [category, theme]);
}

/**
 * The flags as icons. Rendered inside the row's select button, so they are plain glyphs
 * with a hover title rather than tooltip anchors (a focusable span inside a button is a
 * nested control); the button's own accessible name carries the same words.
 */
function FlagIcons({ call }: { call: Call }) {
  const flags = callFlags(call);
  if (flags.length === 0) return null;
  return (
    <>
      {flags.map((flag) => (
        <span
          key={flag.key}
          title={flag.label}
          data-flag={flag.key}
          className={
            // A page is the filled bell; amber is kept for a keyed-up channel.
            flag.key === "page" ? "text-ink" : flag.key === "silent" ? "text-muted" : "text-ink-2"
          }
        >
          {flag.key === "encrypted" ? (
            <Lock size={12} aria-hidden="true" />
          ) : flag.key === "silent" ? (
            <VolumeX size={12} aria-hidden="true" />
          ) : (
            <Bell size={13} aria-hidden="true" fill={flag.key === "page" ? "currentColor" : "none"} />
          )}
          <span className="sr-only">{flag.label}</span>
        </span>
      ))}
    </>
  );
}

/**
 * Why a call has no text, said quietly: muted italic, never the raw status. "empty"
 * and "skipped" read as faults, and an encrypted call is not one.
 */
function NoTranscript({ call }: { call: Call }) {
  return <span className="italic text-muted">{noTranscriptReason(call)}</span>;
}

/**
 * The reason the quality column shows a neutral glyph for, or null to leave the
 * quality mark alone. A call with words keeps its quality judgment, and so does a
 * transcript the archive rejected as noise: that red ✕ is the one real warning here.
 */
function quietReason(call: Call) {
  if (call.transcript_text || call.transcript_quality !== null) return null;
  return noTranscriptReason(call);
}

const COLUMNS: Record<CallColumnKey, Column> = {
  time: {
    key: "time",
    label: "Time",
    help: "calls.table.time",
    sort: "started_at",
    className: "flex w-32 shrink-0 items-center gap-1.5 tabular-nums text-ink-2",
    // The marker belongs beside the *time*, because the time is the thing it qualifies:
    // this row's clock says 2022 and it only reached the archive last Tuesday.
    cell: (call) => (
      <>
        {formatCallTime(call.started_at)}
        <BackfilledMark call={call} />
      </>
    ),
  },
  talkgroup: {
    key: "talkgroup",
    label: "Talkgroup",
    help: "calls.table.talkgroup",
    sort: "talkgroup",
    className: "flex min-w-28 shrink basis-52 items-center gap-1.5 truncate",
    cell: (call) =>
      isUnitCall(call) ? (
        <>
          <UnitCallChip />
          <span className={CHANNEL_NAME}>{unitCallLabel(call)}</span>
        </>
      ) : (
        <>
          {/* What kind of channel this was, before its name: on a screen of names
              that all look alike, the kind is what the eye can sort on. The row
              already carries it (the calls query joins the talkgroup for the
              alias), so this costs nothing to fetch. */}
          <CategoryIcon category={call.category} serviceTag={call.service_tag} interactive={false} />
          {/* The category's colour is the row's key-up edge; the talkgroup's own raw
              colour, once a dot here, is left to the talkgroup pages. */}
          <span className={CHANNEL_NAME}>{call.patch_label ?? callTalkgroupLabel(call)}</span>
          <span className="shrink-0 text-[11px] tabular-nums text-muted">{call.tgid}</span>
        </>
      ),
  },
  radio: {
    key: "radio",
    label: "Radio",
    help: "calls.table.radio",
    sort: "radio",
    className: "w-24 shrink-0 truncate tabular-nums text-ink-2",
    cell: (call) => call.radio_label ?? <span className="text-muted">unresolved</span>,
  },
  source: {
    key: "source",
    label: "Talker",
    help: "calls.table.source",
    className: "w-28 shrink-0 truncate text-ink-2",
    cell: (call) => call.talker_alias ?? <span className="text-muted">—</span>,
  },
  duration: {
    key: "duration",
    label: "Length",
    help: "calls.table.duration",
    sort: "duration_s",
    className: "w-14 shrink-0 text-right tabular-nums text-ink-2",
    cell: (call) => formatDuration(call.duration_s),
  },
  frequency: {
    key: "frequency",
    label: "Freq",
    help: "calls.table.frequency",
    className: "w-24 shrink-0 text-right tabular-nums text-ink-2",
    cell: (call) =>
      call.frequency_hz ? formatFrequency(call.frequency_hz) : <span className="text-muted">—</span>,
  },
  flags: {
    key: "flags",
    label: "Flags",
    help: "calls.table.tone",
    tip: `A lock marks an encrypted call. ${HELP["calls.table.tone"]}`,
    className: FLAGS_CLASS,
    cell: (call) => <FlagIcons call={call} />,
  },
  quality: {
    key: "quality",
    label: "Q",
    help: "calls.table.quality",
    sort: "quality",
    className: "flex w-8 shrink-0 justify-center",
    // Rendered outside the row's select button (see CallRow) so its tooltip is its own
    // focus target rather than a nested one; this cell is used only for the header.
    cell: () => null,
  },
  ingest: {
    key: "ingest",
    label: "Ingest",
    help: "calls.table.ingest",
    className: "w-24 shrink-0 truncate text-[11px] text-ink-2",
    cell: (call) => call.ingest_source.replace(/_/g, " "),
  },
  transcript: {
    key: "transcript",
    label: "Transcript",
    help: "calls.table.transcript",
    className: "min-w-20 flex-1 truncate text-ink",
    cell: (call, query) =>
      call.transcript_text ? highlightTerms(call.transcript_text, query) : <NoTranscript call={call} />,
  },
};

/**
 * A column's classes for the header and the rows alike. With the transcript on its own
 * row nothing on the first line grows, so the talkgroup takes the slack rather than
 * leaving a gap between the flags and the quality mark.
 */
function columnClass(key: CallColumnKey, transcriptRow: boolean): string {
  const base = COLUMNS[key].className;
  return transcriptRow && key === "talkgroup" ? `${base} grow` : base;
}

/**
 * The results.
 *
 * Virtualized, because a filter matching a whole day is thousands of rows and the
 * archive grows by about four thousand a day. That rules out a real `<table>`
 * (absolutely-positioned `<tr>`s are not a table), and a *fake* one — divs
 * wearing `role="table"` — is worse than none: it promises a screen reader a grid
 * it cannot then navigate. So this is what it actually is: a header strip of sort
 * buttons above a list, where every row carries its own full description.
 *
 * Which columns show, and whether the transcript gets its own wrapping second row,
 * are the reader's to choose (`columns` / `transcriptRow`); both are heights the
 * virtualizer measures per row rather than assuming.
 */
export function CallsTable({
  calls,
  selectedId,
  onSelect,
  sort,
  onSortChange,
  query,
  onToggleStar,
  canWrite,
  columns = DEFAULT_CALL_COLUMNS,
  transcriptRow = false,
  checked,
  onCheck,
  onCheckAll,
  freshIds,
  playingId = null,
  supersededIds,
  containerRef,
  className,
}: {
  calls: Call[];
  selectedId: number | null;
  onSelect: (call: Call) => void;
  sort: string;
  onSortChange: (sort: string) => void;
  /** The active text query, so matches inside the snippet can be marked. */
  query?: string;
  onToggleStar: (call: Call) => void;
  canWrite: boolean;
  /** Which columns to show, in `CALL_COLUMN_KEYS` order. */
  columns?: CallColumnKey[];
  /** Give each call a second, full-width row carrying the whole transcript. */
  transcriptRow?: boolean;
  /**
   * The ids ticked for export. Passing this (with `onCheck`) is what turns the tick
   * column on at all: the selection lives on the page, because it has to outlive a
   * scroll — the virtualizer unmounts every row that leaves the viewport.
   */
  checked?: ReadonlySet<number>;
  onCheck?: (call: Call, checked: boolean) => void;
  /** Tick or untick every call currently loaded. */
  onCheckAll?: (checked: boolean) => void;
  /** Calls that have just arrived on the live feed, marked for a moment. */
  freshIds?: ReadonlySet<number>;
  /** The call the player is sounding right now (loaded *and* playing), if any. */
  playingId?: number | null;
  /**
   * Calls reconciliation has demoted while the list was on screen. The live list drops
   * them; the only one that reaches here is the row somebody is listening to, which is
   * dimmed and labelled rather than pulled out from under them.
   */
  supersededIds?: ReadonlySet<number>;
  /**
   * The scrolling element, handed out so the page can hold its position while rows
   * are inserted above it. Without this the table keeps it to itself.
   */
  containerRef?: RefObject<HTMLDivElement | null>;
  className?: string;
}) {
  const ownRef = useRef<HTMLDivElement | null>(null);
  // The page needs this element to compensate its scroll position when live rows are
  // inserted above the reader, so it may pass its own ref in and keep it.
  const parentRef = containerRef ?? ownRef;
  const virtualizer = useVirtualizer({
    count: calls.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => (transcriptRow ? ROW_HEIGHT + 32 : ROW_HEIGHT),
    // Measured heights are cached per key, and the default key is the index. A live call
    // inserted at the top shifts every index by one while React keeps each row's element
    // (keyed by id), so nothing is re-measured and each row inherited its neighbour's
    // height: a three-line transcript drawn in a one-line slot overlapped the next row.
    getItemKey: (index) => calls[index]?.id ?? index,
    overscan: 12,
  });

  // Toggling the transcript row changes every row's height, so the cached measurements
  // have to be thrown away and taken again. `virtualizer` is stable across renders.
  // biome-ignore lint/correctness/useExhaustiveDependencies: measure on the toggle only
  useEffect(() => {
    virtualizer.measure();
  }, [transcriptRow]);

  const sortKey = sort.replace(/^-/, "");
  const descending = sort.startsWith("-");

  const selectable = checked !== undefined && onCheck !== undefined;
  const checkedHere = selectable ? calls.filter((call) => checked?.has(call.id)).length : 0;
  const allChecked = selectable && calls.length > 0 && checkedHere === calls.length;
  const someChecked = checkedHere > 0;

  // `quality` and the actions live outside the per-row select button; the transcript
  // moves to its own row when `transcriptRow` is on, so it is not an inline column then.
  const showQuality = columns.includes("quality");
  // Memoised so every row gets the same array while the choice stands: `CallRow` is
  // `memo`, and a fresh array per render would re-render every row on every live update.
  const inlineKeys = useMemo(
    () =>
      CALL_COLUMN_KEYS.filter(
        (key) => columns.includes(key) && key !== "quality" && !(transcriptRow && key === "transcript"),
      ),
    [columns, transcriptRow],
  );

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="flex shrink-0 items-center gap-2 border-b border-line bg-surface px-3 py-1.5 text-xs font-medium text-ink-2">
        {selectable ? (
          <span className="flex w-4 shrink-0 items-center">
            <Tip label={HELP["calls.table.selectAll"]}>
              <input
                type="checkbox"
                aria-label={allChecked ? "Clear the selection" : "Select every loaded call"}
                checked={allChecked}
                // Some but not all: the box says "there is a selection" without
                // claiming the whole page is in it.
                ref={(node) => {
                  if (node) node.indeterminate = someChecked && !allChecked;
                }}
                onChange={(event) => onCheckAll?.(event.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--accent)]"
              />
            </Tip>
          </span>
        ) : null}
        {/* The emergency flag gutter — kept in the header so the columns line up with
            the rows, which reserve the same width for the callout badge. */}
        <div className="w-5 shrink-0">
          <span className="sr-only">Emergency</span>
        </div>
        {inlineKeys.map((key) => {
          const column = COLUMNS[key];
          const active = column.sort === sortKey;
          const content = (
            <span className="inline-flex items-center gap-1">
              {column.label}
              {active ? (
                descending ? (
                  <ArrowDown size={11} aria-hidden="true" />
                ) : (
                  <ArrowUp size={11} aria-hidden="true" />
                )
              ) : null}
            </span>
          );
          return (
            <div key={column.key} className={columnClass(key, transcriptRow)}>
              {column.sort ? (
                <Tip label={`${HELP[column.help]}. ${HELP["calls.table.sort"]}`}>
                  <button
                    type="button"
                    aria-label={`Sort by ${column.label}`}
                    onClick={() =>
                      onSortChange(active && descending ? (column.sort as string) : `-${column.sort}`)
                    }
                    className={cn("hover:text-ink", active && "text-accent-ink")}
                  >
                    {content}
                  </button>
                </Tip>
              ) : (
                <Tip label={column.tip ?? HELP[column.help]}>
                  <span tabIndex={0}>{content}</span>
                </Tip>
              )}
            </div>
          );
        })}
        {showQuality ? (
          <div className={COLUMNS.quality.className}>
            <Tip label={`${HELP["calls.table.quality"]}. ${HELP["calls.table.sort"]}`}>
              <button
                type="button"
                aria-label="Sort by Quality"
                onClick={() => onSortChange(sortKey === "quality" && descending ? "quality" : "-quality")}
                className={cn("hover:text-ink", sortKey === "quality" && "text-accent-ink")}
              >
                <span className="inline-flex items-center gap-1">
                  Q
                  {sortKey === "quality" ? (
                    descending ? (
                      <ArrowDown size={11} aria-hidden="true" />
                    ) : (
                      <ArrowUp size={11} aria-hidden="true" />
                    )
                  ) : null}
                </span>
              </button>
            </Tip>
          </div>
        ) : null}
        <div className={cn(ACTIONS_CLASS, "text-right")}>
          <span className="sr-only">Actions</span>
        </div>
      </div>

      <div ref={parentRef} className="min-h-0 flex-1 overflow-auto scrollbar-thin">
        <ul aria-label="Calls" style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const call = calls[virtualRow.index];
            if (!call) return null;
            return (
              <li
                key={call.id}
                data-index={virtualRow.index}
                ref={virtualizer.measureElement}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${virtualRow.start}px)`,
                }}
              >
                <CallRow
                  call={call}
                  selected={call.id === selectedId}
                  fresh={freshIds?.has(call.id) ?? false}
                  playing={call.id === playingId}
                  superseded={supersededIds?.has(call.id) ?? false}
                  onSelect={onSelect}
                  onToggleStar={onToggleStar}
                  canWrite={canWrite}
                  query={query}
                  inlineKeys={inlineKeys}
                  showQuality={showQuality}
                  transcriptRow={transcriptRow}
                  checked={selectable ? (checked?.has(call.id) ?? false) : undefined}
                  onCheck={onCheck}
                />
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

/**
 * The row-actions cell: three 24 px buttons (WCAG 2.5.8) and their gaps. Shared by the
 * header and the rows so the two line up.
 */
const ACTIONS_CLASS = "flex w-20 shrink-0 items-center justify-end gap-0.5";
const ACTION_BUTTON = "inline-flex size-6 items-center justify-center rounded hover:bg-surface";

/**
 * One row. `memo`, because the live feed re-renders the table every time a call
 * arrives or a transcript lands, and without it every visible row (thirty-odd, each
 * with its tooltips) re-rendered for one changed call. The props are all primitives,
 * the call object (kept by identity while unchanged — see `useLiveCalls`), and
 * callbacks the page holds stable.
 */
export const CallRow = memo(function CallRow({
  call,
  selected,
  fresh = false,
  playing = false,
  superseded = false,
  onSelect,
  onToggleStar,
  canWrite,
  query,
  inlineKeys,
  showQuality,
  transcriptRow,
  checked,
  onCheck,
}: {
  call: Call;
  selected: boolean;
  /** Just arrived on the live feed: marked briefly so the reader sees what moved. */
  fresh?: boolean;
  /** This call is what the player is sounding right now. */
  playing?: boolean;
  /** Reconciliation kept another recorder's copy of this transmission instead. */
  superseded?: boolean;
  onSelect: (call: Call) => void;
  onToggleStar: (call: Call) => void;
  canWrite: boolean;
  query?: string;
  inlineKeys: CallColumnKey[];
  showQuality: boolean;
  transcriptRow: boolean;
  /** `undefined` when the table is not in selection mode at all. */
  checked?: boolean;
  onCheck?: (call: Call, checked: boolean) => void;
}) {
  const emergency = call.emergency === true;
  const edge = useEdgeStyle(call);
  return (
    <div
      className={cn(
        // Flat: a hairline under each row, no radius. The key-up edge (index.css) is
        // drawn over the row's left padding, so it never moves a column; an
        // emergency's edge is red and the callout tints the whole wrapper, so both
        // rows of the transcript view are flagged together.
        "key-edge border-b border-line text-sm",
        emergency && "bg-critical-soft",
        selected ? "bg-accent-soft" : emergency ? "" : "hover:bg-surface-2",
        (call.duplicate_of !== null || superseded) && "opacity-60",
      )}
      style={edge.style}
      data-color={edge.color}
      data-selected={selected ? "true" : undefined}
      // An arrival is marked by the edge alone — amber for a few seconds, then the
      // channel's colour — so the top of a list somebody is scanning shows what moved
      // without anything moving.
      data-fresh={fresh ? "true" : undefined}
      data-playing={playing ? "true" : undefined}
      data-emergency={emergency ? "true" : undefined}
      data-superseded={superseded ? "true" : undefined}
    >
      <div className="flex h-10 items-center gap-2 px-3">
        {checked !== undefined ? (
          <span className="flex w-4 shrink-0 items-center">
            <Tip label={HELP["calls.table.select"]}>
              <input
                type="checkbox"
                checked={checked}
                aria-label={`Select the call on ${callTalkgroupLabel(call)} for export`}
                onChange={(event) => onCheck?.(call, event.target.checked)}
                className="h-3.5 w-3.5 accent-[var(--accent)]"
              />
            </Tip>
          </span>
        ) : null}
        <span className="flex w-5 shrink-0 items-center justify-center">
          {/* Decorative here: the words are the first thing inside the row button, so
              they are part of its name rather than a separate stop beside it. */}
          {emergency ? (
            <span className="text-critical-ink" title="Emergency">
              <Siren size={14} aria-hidden="true" />
            </span>
          ) : null}
        </span>
        <Tip label={HELP["calls.table.row"]}>
          <button
            type="button"
            onClick={() => onSelect(call)}
            aria-current={selected ? "true" : undefined}
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
          >
            {/* No aria-label: the button is named by what it shows (time, channel, radio,
                length, flags, transcript), so speech input can say what it sees
                (WCAG 2.5.3). Emergency, shown only as the gutter's colour and icon,
                leads the name in words. */}
            {emergency ? <span className="sr-only">Emergency call</span> : null}
            {inlineKeys.map((key) => (
              <span key={key} className={columnClass(key, transcriptRow)}>
                {COLUMNS[key].cell(call, query)}
              </span>
            ))}
          </button>
        </Tip>
        {superseded ? (
          <Tip label="Another recorder's copy of this transmission is now the primary one">
            <span className="shrink-0 rounded bg-surface-2 px-1 text-[11px] text-muted">superseded</span>
          </Tip>
        ) : null}
        {showQuality ? (
          <span className={COLUMNS.quality.className}>
            <QualityIcon quality={call.transcript_quality} reason={quietReason(call)} />
          </span>
        ) : null}
        <span className={ACTIONS_CLASS}>
          <Tip label={HELP["calls.table.copy"]}>
            <button
              type="button"
              aria-label={`Copy a summary of the call on ${callTalkgroupLabel(call)}`}
              onClick={() => void copyAndToast(callSummary(call), { label: "Call summary" })}
              className={cn(ACTION_BUTTON, "text-ink-2 hover:text-ink")}
            >
              <ClipboardCopy size={13} aria-hidden="true" />
            </button>
          </Tip>
          <Tip label={HELP["player.play"]}>
            <button
              type="button"
              aria-label={`Play the call on ${callTalkgroupLabel(call)}`}
              onClick={() => onSelect(call)}
              className={cn(ACTION_BUTTON, "text-ink-2 hover:text-ink")}
            >
              <Play size={13} aria-hidden="true" />
            </button>
          </Tip>
          {/* Only for a reader who can star: a guest was shown a disabled star on
              every row, an edit control they could never use. The column keeps its
              width either way, so rows line up with the header. */}
          {canWrite ? (
            <Tip label={HELP["call.star"]}>
              <button
                type="button"
                aria-pressed={call.starred}
                aria-label={call.starred ? "Remove the star" : "Star this call"}
                onClick={() => onToggleStar(call)}
                className={cn(ACTION_BUTTON, call.starred ? "text-accent-ink" : "text-ink-2 hover:text-ink")}
              >
                <Star size={13} aria-hidden="true" fill={call.starred ? "currentColor" : "none"} />
              </button>
            </Tip>
          ) : null}
        </span>
      </div>
      {transcriptRow ? (
        <div className="px-3 pb-2 text-sm leading-[1.45] text-ink">
          {call.transcript_text ? (
            <span className="block max-w-[75ch] whitespace-pre-wrap break-words">
              {highlightTerms(call.transcript_text, query)}
            </span>
          ) : (
            <NoTranscript call={call} />
          )}
        </div>
      ) : null}
    </div>
  );
});

/** The same rows as cards, for a phone where seven columns do not fit. */
export function CallCards({
  calls,
  selectedId,
  playingId = null,
  freshIds,
  onSelect,
  query,
  className,
}: {
  calls: Call[];
  selectedId: number | null;
  /** The call the player is sounding right now, if any (see `CallsTable`). */
  playingId?: number | null;
  /** Calls that have just arrived on the live feed (see `CallsTable`). */
  freshIds?: ReadonlySet<number>;
  onSelect: (call: Call) => void;
  query?: string;
  className?: string;
}) {
  return (
    <ul aria-label="Calls" className={cn("divide-y divide-line", className)}>
      {calls.map((call) => (
        <li key={call.id}>
          <CallCard
            call={call}
            selected={call.id === selectedId}
            playing={call.id === playingId}
            fresh={freshIds?.has(call.id) ?? false}
            onSelect={onSelect}
            query={query}
          />
        </li>
      ))}
    </ul>
  );
}

/** One card; `memo` for the same reason as `CallRow`. */
export const CallCard = memo(function CallCard({
  call,
  selected,
  playing = false,
  fresh = false,
  onSelect,
  query,
}: {
  call: Call;
  selected: boolean;
  playing?: boolean;
  fresh?: boolean;
  onSelect: (call: Call) => void;
  query?: string;
}) {
  const edge = useEdgeStyle(call);
  return (
    <div
      className={cn(
        "key-edge flex items-start gap-2 px-3 py-2.5",
        call.emergency === true && "bg-critical-soft",
        selected ? "bg-accent-soft" : "",
      )}
      style={edge.style}
      data-color={edge.color}
      data-selected={selected ? "true" : undefined}
      data-playing={playing ? "true" : undefined}
      data-fresh={fresh ? "true" : undefined}
      data-emergency={call.emergency === true ? "true" : undefined}
    >
      <Tip label={HELP["calls.table.row"]}>
        <button type="button" onClick={() => onSelect(call)} className="min-w-0 flex-1 text-left">
          <div className="flex items-baseline gap-2">
            {/* Plain marks with their words in sr-only text, not tooltip anchors: inside
                the button only its content is read, so a tooltip's description on a
                child was never announced, and a focusable child is a nested control. */}
            {call.emergency === true ? (
              <span className="shrink-0 text-critical-ink" title="Emergency">
                <Siren size={13} aria-hidden="true" />
                <span className="sr-only">Emergency call</span>
              </span>
            ) : null}
            <FlagIcons call={call} />
            <CategoryIcon category={call.category} serviceTag={call.service_tag} interactive={false} />
            <span className={CHANNEL_NAME}>{call.patch_label ?? callTalkgroupLabel(call)}</span>
            <span className="ml-auto flex shrink-0 items-center gap-1.5 text-xs tabular-nums text-ink-2">
              <BackfilledMark call={call} />
              {formatCallTime(call.started_at)}
            </span>
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-xs text-ink-2">
            <span className="tabular-nums">{call.radio_label ?? "unresolved"}</span>
            <span className="tabular-nums">{formatDuration(call.duration_s)}</span>
            <QualityIcon quality={call.transcript_quality} reason={quietReason(call)} />
          </div>
          <p className="mt-1 line-clamp-2 max-w-[75ch] text-sm leading-[1.45] text-ink">
            {call.transcript_text ? (
              highlightTerms(call.transcript_text, query)
            ) : (
              <NoTranscript call={call} />
            )}
          </p>
        </button>
      </Tip>
      <Tip label={HELP["calls.table.open"]}>
        <Link
          to={`/calls/${call.id}`}
          aria-label={`Open the detail page for the call on ${callTalkgroupLabel(call)}`}
          className="inline-flex size-6 shrink-0 items-center justify-center rounded text-ink-2 hover:bg-surface hover:text-ink"
        >
          <ExternalLink size={14} aria-hidden="true" />
        </Link>
      </Tip>
    </div>
  );
});
