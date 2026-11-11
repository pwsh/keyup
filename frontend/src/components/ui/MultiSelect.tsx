import { Check, ChevronDown, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { loadHelp, ResolvedHelp } from "../../help/help";
import { cn } from "../../lib/cn";
import { ColorDot } from "./ColorDot";
import { Popover } from "./Popover";
import { Tip } from "./Tooltip";

export interface MultiSelectOption {
  value: string;
  label: string;
  group?: string;
  count?: number;
  /** Color swatch — the talkgroup list is grouped and colored by category. */
  color?: string;
  /**
   * When this option was last heard (ISO time). Once any option carries one, the list is
   * ordered by it — most recent first, and groups by their most recent member — so the
   * channels actually in use (the local county's) lead instead of sitting under twenty
   * other counties in alphabetical order. Without it the order stays alphabetical by group.
   */
  lastHeard?: string | null;
}

const heardAt = (option: MultiSelectOption): number => {
  const at = option.lastHeard ? Date.parse(option.lastHeard) : Number.NaN;
  return Number.isNaN(at) ? Number.NEGATIVE_INFINITY : at;
};

/**
 * The options as the picker lists them, grouped: by recency when the options say when
 * they were last heard, else alphabetically by group in the order given. Sorted *before*
 * the `maxVisible` cut, so the cut drops the stalest options, not the latest ones.
 */
export function orderOptions(
  options: MultiSelectOption[],
  maxVisible: number,
): { recent: boolean; groups: [string, MultiSelectOption[]][] } {
  const recent = options.some((o) => heardAt(o) > Number.NEGATIVE_INFINITY);
  const ordered = recent
    ? [...options].sort((a, b) => heardAt(b) - heardAt(a) || a.label.localeCompare(b.label))
    : options;
  const map = new Map<string, MultiSelectOption[]>();
  for (const opt of ordered.slice(0, maxVisible)) {
    const key = opt.group ?? "";
    if (!map.has(key)) map.set(key, []);
    (map.get(key) as MultiSelectOption[]).push(opt);
  }
  const groups = [...map.entries()];
  // Recency: a group's place is its newest member's, which is insertion order already.
  if (!recent) groups.sort((a, b) => a[0].localeCompare(b[0]));
  return { recent, groups };
}

export function MultiSelect({
  label,
  tip,
  options,
  selected,
  onChange,
  searchPlaceholder = "Search…",
  emptyText = "No matches",
  className,
  maxVisible = 400,
}: {
  label: string;
  tip: string;
  options: MultiSelectOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  searchPlaceholder?: string;
  emptyText?: string;
  className?: string;
  maxVisible?: number;
}) {
  const [query, setQuery] = useState("");
  const selectedSet = useMemo(() => new Set(selected), [selected]);

  const { recent, groups } = useMemo(() => {
    const q = query.trim().toUpperCase();
    const filtered = options.filter(
      (o) => !q || o.label.toUpperCase().includes(q) || (o.group ?? "").toUpperCase().includes(q),
    );
    return orderOptions(filtered, maxVisible);
  }, [options, query, maxVisible]);

  const toggle = (value: string) => {
    onChange(selectedSet.has(value) ? selected.filter((v) => v !== value) : [...selected, value]);
  };

  return (
    <Popover
      className={cn("w-[min(22rem,calc(100vw-2rem))]", className)}
      trigger={
        <button
          type="button"
          className={cn(
            "inline-flex h-9 items-center gap-1.5 rounded-md border px-2.5 text-sm",
            selected.length
              ? "border-accent bg-accent-soft text-accent-ink"
              : "border-control-border bg-surface text-ink",
          )}
          aria-label={`${label}${selected.length ? `, ${selected.length} selected` : ""}`}
        >
          {label}
          {selected.length ? (
            <span className="rounded-full bg-accent-fill px-1.5 text-[11px] font-semibold tabular-nums text-white">
              {selected.length}
            </span>
          ) : null}
          <ChevronDown size={14} aria-hidden="true" />
        </button>
      }
    >
      <div className="mb-2 flex items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={searchPlaceholder}
          aria-label={`Search ${label}`}
          className="h-8 w-full rounded-md border border-control-border bg-surface px-2 text-sm text-ink placeholder:text-ink-2"
        />
        {selected.length ? (
          <Tip label={`Remove all ${label.toLowerCase()} filters`}>
            <button
              type="button"
              onClick={() => onChange([])}
              className="inline-flex h-8 shrink-0 items-center gap-1 rounded-md border border-line px-2 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink"
            >
              <X size={12} aria-hidden="true" /> Clear
            </button>
          </Tip>
        ) : null}
      </div>
      <PickerHint tip={tip} recent={recent} />
      <div className="max-h-72 overflow-auto scrollbar-thin">
        {groups.length === 0 ? <p className="px-1 py-6 text-center text-xs text-ink-2">{emptyText}</p> : null}
        {groups.map(([group, items]) => (
          <div key={group} className="mb-2">
            {group ? (
              <p className="sticky top-0 bg-surface px-1 py-1 text-xs font-medium text-ink-2">{group}</p>
            ) : null}
            <ul>
              {items.map((opt) => {
                const active = selectedSet.has(opt.value);
                return (
                  <li key={opt.value}>
                    <button
                      type="button"
                      aria-pressed={active}
                      onClick={() => toggle(opt.value)}
                      className="flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-sm text-ink hover:bg-surface-2"
                    >
                      <span
                        className={cn(
                          "flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                          active ? "border-accent bg-accent text-white" : "border-control-border",
                        )}
                        aria-hidden="true"
                      >
                        {active ? <Check size={11} /> : null}
                      </span>
                      {opt.color ? <ColorDot color={opt.color} /> : null}
                      <span className="min-w-0 flex-1 truncate">{opt.label}</span>
                      {opt.count !== undefined ? (
                        <span className="shrink-0 text-xs tabular-nums text-ink-2">{opt.count}</span>
                      ) : null}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </Popover>
  );
}

/**
 * The line under the search box. `tip` is usually a `HELP[...]` entry, which is only a
 * placeholder until the help text has loaded — so ask for it, and resolve it when it lands.
 */
function PickerHint({ tip, recent }: { tip: string; recent: boolean }) {
  useEffect(() => {
    void loadHelp();
  }, []);
  return (
    <p className="mb-2 text-[11px] text-ink-2">
      <ResolvedHelp>{tip}</ResolvedHelp>
      {recent ? <span className="mt-0.5 block">Most recently heard first.</span> : null}
    </p>
  );
}
