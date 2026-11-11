/**
 * Merging live arrivals into a list somebody is reading.
 *
 * The rules the operator asked for are all about *not moving things*: a call that
 * is selected must not be unselected and must not shift on screen, and a reader
 * scrolled up into yesterday must be able to keep reading while the archive grows
 * underneath them. That makes this a pure list operation with two guarantees —
 * an id already in the list is **updated in place** (a transcript landing on a row
 * is the common case) rather than re-inserted, and a genuinely new call is placed
 * where the sort says it belongs, which for the default `-started_at` is the top.
 *
 * The scroll compensation that keeps the selected row still lives in
 * `useLiveCalls`; this module only decides what the list contains.
 */
import type { Call } from "../../api/types";

/** How many rows the page keeps in memory before dropping the oldest off the bottom. */
export const LIVE_LIST_CAP = 500;

/** Milliseconds for the call's start, or 0 for a timestamp we cannot read. */
const startedAt = (call: Call): number => {
  const at = Date.parse(call.started_at);
  return Number.isNaN(at) ? 0 : at;
};

/** Milliseconds for the row's last change, or null when it does not say. */
const updatedAt = (call: Call): number | null => {
  if (!call.updated_at) return null;
  const at = Date.parse(call.updated_at);
  return Number.isNaN(at) ? null : at;
};

/**
 * Whether `incoming` is an older copy of the row than `held`.
 *
 * Two sources write into one list — the socket and the REST refetch — and they race: a
 * page requested before an edit can land after the edit's `call.updated` frame, and used
 * to put the pre-edit row back. `updated_at` moves on every write to the row, so the
 * newer one wins. A tie, or a row that does not say, takes the incoming copy as before.
 */
export function isOlderCopy(incoming: Call, held: Call): boolean {
  const a = updatedAt(incoming);
  const b = updatedAt(held);
  return a !== null && b !== null && a < b;
}

/**
 * The page's default order: newest first, id descending as the tie-break so two
 * calls recorded in the same instant still have one stable answer.
 */
export function compareNewestFirst(a: Call, b: Call): number {
  const difference = startedAt(b) - startedAt(a);
  return difference !== 0 ? difference : b.id - a.id;
}

export interface MergeOptions {
  /** Drop rows past this many, oldest first. */
  cap?: number;
  /** Never dropped by the cap: the row the reader has open. */
  protectedId?: number | null;
}

export interface MergeResult {
  items: Call[];
  /** Ids that were not in the list before — what the "N new calls" pill counts. */
  inserted: number[];
  /** Ids that were already there and got fresher data (a transcript, an edit). */
  updated: number[];
  /** Ids the cap pushed off the bottom. */
  dropped: number[];
  /** False when nothing about the list changed, so the caller can keep the old array. */
  changed: boolean;
}

/**
 * Fold `arrivals` into `current`, newest-first.
 *
 * An arrival whose id is already present replaces that entry **at its existing
 * index** — the row's contents change, the row does not move — unless it is an older
 * copy than the one held (`isOlderCopy`), in which case it is ignored. An unseen call is
 * inserted at the position `compareNewestFirst` gives it, which is the top for a
 * call that has just been recorded; one that sorts below a full list is dropped,
 * because it belongs to a page nobody has loaded.
 */
export function mergeCalls(current: Call[], arrivals: Call[], options: MergeOptions = {}): MergeResult {
  const cap = options.cap ?? LIVE_LIST_CAP;
  const protectedId = options.protectedId ?? null;
  const inserted: number[] = [];
  const updated: number[] = [];
  const dropped: number[] = [];

  let items = current;
  let copied = false;
  const mutable = (): Call[] => {
    if (!copied) {
      items = [...items];
      copied = true;
    }
    return items;
  };

  const index = new Map<number, number>();
  for (const [position, call] of current.entries()) index.set(call.id, position);

  for (const arrival of arrivals) {
    const at = index.get(arrival.id);
    if (at !== undefined) {
      // Already on screen. The same object means the same data — say nothing
      // happened, so a repeated frame cannot flash a highlight or bump a count.
      if (items[at] === arrival) continue;
      // An older copy than the one on screen (a refetch that was in flight when the
      // edit arrived live) changes nothing.
      if (isOlderCopy(arrival, items[at] as Call)) continue;
      mutable()[at] = arrival;
      updated.push(arrival.id);
      continue;
    }

    let position = items.length;
    for (const [candidate, call] of items.entries()) {
      if (compareNewestFirst(arrival, call) < 0) {
        position = candidate;
        break;
      }
    }
    // Older than everything held: this is a later page's row, not this one's. Held or not
    // by the cap — the backlog re-transcribes months-old calls all day, and appending each
    // one piled them up under a scrolling reader. Only an empty list takes any arrival.
    if (position === items.length && items.length > 0) continue;
    mutable().splice(position, 0, arrival);
    inserted.push(arrival.id);
    for (let i = position; i < items.length; i++) index.set((items[i] as Call).id, i);
  }

  while (items.length > cap) {
    const last = items[items.length - 1] as Call;
    // The reader has this one open. Sitting one row over the cap is cheaper than
    // yanking the call they are listening to out from under them.
    if (last.id === protectedId) break;
    mutable().pop();
    dropped.push(last.id);
  }

  return { items, inserted, updated, dropped, changed: copied };
}
