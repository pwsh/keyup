/**
 * The directory's filter state, and the controls that set it.
 *
 * Three things are true at once about the talkgroup list and they force this shape:
 *
 * 1. **The server does the filtering.** A page caps at 500 rows, so narrowing the rows
 *    the page happens to hold would answer "of the channels on this screen, which are
 *    not transcribed" under a header that says "not transcribed". Every control here
 *    maps to a query parameter `GET /talkgroups` understands.
 * 2. **The URL is the state.** No `useState` mirror of it: a filtered directory is a
 *    thing operators send each other ("the seventeen channels still going to OpenMHz"),
 *    and it has to survive a reload and a back button. The query string uses the API's
 *    own parameter names, so the link and the request say the same words.
 * 3. **Every filter is removable from one place.** A control tucked into a column header
 *    is invisible once the reader has scrolled sideways; the chips above the table are
 *    the inventory of what is being asked, and they are also how it is taken back.
 *
 * The policy and export filters are three-valued rather than checkboxes. "Any / on /
 * off" is the real question — an operator auditing what reaches Broadcastify wants the
 * channels that are *off*, and a checkbox can only ever offer "on" and "unasked".
 */

import { X } from "lucide-react";
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";
import type { TalkgroupsParams } from "../../api/hooks";
import { Select } from "../ui/Select";

/** Any / on / off. The empty string is "not asked", which is not the same as "off". */
export type Tri = "" | "true" | "false";

/** The three-valued filters, by the name the API and the URL both use. */
export const TRI_FILTERS = [
  "shared",
  "record",
  "transcribe",
  "export_broadcastify",
  "export_bcfy_calls",
  "export_openmhz",
] as const;

export type TriFilterName = (typeof TRI_FILTERS)[number];

/** The one-of-a-list filters, likewise. */
export const CHOICE_FILTERS = ["category", "service_tag", "agency", "system_id"] as const;

export type ChoiceFilterName = (typeof CHOICE_FILTERS)[number];

/** The API's default order, and so the URL's: the busiest channels first. */
export const DEFAULT_SORT = "-call_count";

export interface TalkgroupFilterState {
  /** The free-text search, as the *server* has it — the box debounces into this. */
  q: string;
  category: string;
  service_tag: string;
  agency: string;
  system_id: string;
  shared: Tri;
  record: Tri;
  transcribe: Tri;
  export_broadcastify: Tri;
  export_bcfy_calls: Tri;
  export_openmhz: Tri;
  /** `include_hidden`, under the shorter name the switch has always had. */
  hidden: boolean;
  /** The API sort key with its `-`, e.g. `-call_count`. */
  sort: string;
  page: number;
}

/** Everything a filter can be set to, plus `null` for "take this one off". */
type Change = string | number | boolean | null;

export interface TalkgroupFilters {
  state: TalkgroupFilterState;
  /** Set one filter. Anything but the page resets the page — see `set` below. */
  set: (key: keyof TalkgroupFilterState, value: Change) => void;
  clearAll: () => void;
  /** Whether anything beyond the sort and the page is being asked. */
  active: boolean;
  /**
   * The whole question as one string. A page that has to react to "the question
   * changed" — dropping a selection that now refers to rows nobody can see — depends
   * on this rather than on a list of filter names it would have to remember to grow.
   */
  signature: string;
  /** What `useTalkgroups` should be asked for. */
  params: TalkgroupsParams;
}

const asTri = (value: string | null): Tri => (value === "true" || value === "false" ? value : "");

const fromTri = (value: Tri): boolean | undefined => (value === "" ? undefined : value === "true");

const oneOf = (value: string): string[] | undefined => (value ? [value] : undefined);

/**
 * Read the filters out of the URL, and write them back.
 *
 * Unknown values are read as absent rather than passed through: a hand-edited
 * `?transcribe=maybe` would otherwise reach the API as a 422 and empty a page whose
 * controls all look fine.
 */
export function useTalkgroupFilters(pageSize: number): TalkgroupFilters {
  const [searchParams, setSearchParams] = useSearchParams();

  const state: TalkgroupFilterState = useMemo(() => {
    const get = (key: string): string => searchParams.get(key) ?? "";
    const page = Number.parseInt(get("page"), 10);
    return {
      q: get("q"),
      category: get("category"),
      service_tag: get("service_tag"),
      agency: get("agency"),
      system_id: get("system_id"),
      shared: asTri(searchParams.get("shared")),
      record: asTri(searchParams.get("record")),
      transcribe: asTri(searchParams.get("transcribe")),
      export_broadcastify: asTri(searchParams.get("export_broadcastify")),
      export_bcfy_calls: asTri(searchParams.get("export_bcfy_calls")),
      export_openmhz: asTri(searchParams.get("export_openmhz")),
      hidden: get("hidden") === "1",
      sort: get("sort") || DEFAULT_SORT,
      page: Number.isFinite(page) && page > 0 ? page : 1,
    };
  }, [searchParams]);

  const set = useCallback(
    (key: keyof TalkgroupFilterState, value: Change) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          const text_ = value === null || value === false ? "" : value === true ? "1" : String(value);
          if (
            text_ === "" ||
            (key === "sort" && text_ === DEFAULT_SORT) ||
            (key === "page" && text_ === "1")
          ) {
            next.delete(key);
          } else {
            next.set(key, text_);
          }
          // Changing what is being asked changes which rows exist, so page 4 of the old
          // question is not page 4 of the new one — and is often past the end of it.
          if (key !== "page") next.delete("page");
          return next;
        },
        // Replace rather than push: a filter row is a control, and every keystroke and
        // every toggle would otherwise be a separate press of the back button.
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const clearAll = useCallback(() => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams();
        // The sort is not a filter: "clear the filters" must not also throw away the
        // column the reader chose to order by.
        const sort = current.get("sort");
        if (sort) next.set("sort", sort);
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  const active =
    Boolean(
      state.q || state.category || state.service_tag || state.agency || state.system_id || state.hidden,
    ) || TRI_FILTERS.some((name) => state[name] !== "");

  const params: TalkgroupsParams = useMemo(
    () => ({
      q: state.q || undefined,
      category: oneOf(state.category),
      service_tag: oneOf(state.service_tag),
      agency: oneOf(state.agency),
      system_id: state.system_id ? Number(state.system_id) : undefined,
      shared: fromTri(state.shared),
      record: fromTri(state.record),
      transcribe: fromTri(state.transcribe),
      export_broadcastify: fromTri(state.export_broadcastify),
      export_bcfy_calls: fromTri(state.export_bcfy_calls),
      export_openmhz: fromTri(state.export_openmhz),
      include_hidden: state.hidden,
      sort: state.sort,
      page: state.page,
      page_size: pageSize,
    }),
    [state, pageSize],
  );

  return { state, set, clearAll, active, signature: searchParams.toString(), params };
}

/**
 * One column's three-valued filter.
 *
 * Rendered as a select rather than a cycling button: a button that goes any → on → off
 * hides two of its three states behind a click, and eight of them in a row would be a
 * puzzle. The label carries the column name because the header above it is a separate
 * cell and screen readers do not read the two together.
 */
export function TriFilter({
  value,
  onChange,
  label,
  tip,
  onLabel = "on",
  offLabel = "off",
}: {
  value: Tri;
  onChange: (next: Tri) => void;
  /** The column's name, e.g. "Transcribe". */
  label: string;
  tip: string;
  onLabel?: string;
  offLabel?: string;
}) {
  return (
    <Select
      value={value}
      onValueChange={(next) => onChange(next as Tri)}
      options={[
        { value: "", label: "any" },
        { value: "true", label: onLabel },
        { value: "false", label: offLabel },
      ]}
      tip={tip}
      ariaLabel={`Filter by ${label}`}
      size="sm"
      className="h-7 w-full min-w-16 text-xs"
    />
  );
}

/** One chip's worth of "this is being asked, and here is how to stop asking it". */
export interface ActiveFilter {
  key: string;
  label: string;
  onRemove: () => void;
}

/**
 * What is currently being asked, and the way back out.
 *
 * Above the table rather than beside each control: several of the filters live in
 * columns that are off the right of a phone, and a filter the reader cannot find is a
 * directory that looks broken. "Clear all" is deliberately last — it is the big
 * destructive one, and it should not be what the thumb lands on.
 */
export function FilterChips({ filters, onClearAll }: { filters: ActiveFilter[]; onClearAll: () => void }) {
  if (filters.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 pb-2">
      <span className="text-xs text-ink-2">Filtered by</span>
      {/* A list, so a screen reader says how many filters are in force before reading
          them — "seven filters" is the fact that explains an empty table. */}
      <ul aria-label="Active filters" className="flex flex-wrap items-center gap-1.5">
        {filters.map((filter) => (
          <li key={filter.key}>
            <button
              type="button"
              onClick={filter.onRemove}
              aria-label={`Remove filter ${filter.label}`}
              title={`Remove filter ${filter.label}`}
              className="inline-flex items-center gap-1 rounded-full border border-accent bg-accent-soft px-2.5 py-1 text-xs font-medium text-accent-ink hover:bg-surface-2"
            >
              {filter.label}
              <X size={12} aria-hidden="true" />
            </button>
          </li>
        ))}
      </ul>
      {filters.length > 1 ? (
        <button
          type="button"
          onClick={onClearAll}
          className="rounded-full border border-line px-2.5 py-1 text-xs font-medium text-ink-2 hover:bg-surface-2 hover:text-ink"
        >
          Clear all
        </button>
      ) : null}
    </div>
  );
}
