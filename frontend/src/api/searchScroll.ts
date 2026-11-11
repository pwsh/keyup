/**
 * The Search page's scroll: which page to ask for next, and the list the pages make.
 *
 * Kept apart from the hook so the rules are testable without a network:
 *
 * - **By `page`, never by cursor.** `/search` with a query ranks by `ts_rank_cd` whatever
 *   `sort` says, and a keyset cursor can only continue a time sort, so the API ignores one
 *   there. Offset paging is what is left; an arrival that shifts a row across a page
 *   boundary is caught by the de-duplication below.
 * - **One question across pages.** `fallback=true` widens to spelling variants only
 *   when the exact query matched nothing. When page 1 widened by itself, every later page
 *   asks for `fuzzy=true` outright, so page 2 cannot come back exact (a call that matches
 *   exactly arriving mid-scroll would otherwise flip it) and every page is a slice of the
 *   same ranking.
 * - **A ceiling.** The list stops at {@link SEARCH_LIST_CAP} rows; past that the reader is
 *   better served by a narrower question than by a longer scroll.
 */
import type { SearchHit, SearchResponse } from "./types";

/** Rows per `/search` request. */
export const SEARCH_PAGE_SIZE = 100;

/** The most rows the Search page holds; it stops asking for more there. */
export const SEARCH_LIST_CAP = 1000;

/** What a scroll page adds to the search params: nothing for the first, then a page. */
export interface SearchPageParam {
  page?: number;
  /** Sent when page 1 widened by itself, so later pages search the same widened set. */
  fuzzy?: true;
}

/** True when the first page is the API's own fallback to spelling variants. */
export function autoWidened(first: SearchResponse | undefined): boolean {
  return Boolean(first?.fuzzy?.automatic && first.fuzzy.widened);
}

/** The next page to ask for, or `undefined` at the end of the results or at the cap. */
export function nextSearchPageParam(
  last: SearchResponse,
  pages: readonly SearchResponse[],
  cap = SEARCH_LIST_CAP,
): SearchPageParam | undefined {
  // A short page is the last one; so is the one whose end reaches the count or the cap.
  if (last.items.length < last.page_size) return undefined;
  const reached = last.page * last.page_size;
  if (reached >= cap || (reached >= last.total && !last.total_capped)) return undefined;
  return { page: last.page + 1, ...(autoWidened(pages[0]) ? { fuzzy: true as const } : {}) };
}

export interface SearchList {
  hits: SearchHit[];
  /** The first page's count — the same on every page, but the first one is the question's. */
  total: number;
  totalCapped: boolean;
  /** The list reached {@link SEARCH_LIST_CAP} with more matches left unloaded. */
  capped: boolean;
}

/** The pages as one list: first-seen order, each call once, at most `cap` rows. */
export function flattenSearchPages(
  pages: readonly SearchResponse[] | undefined,
  cap = SEARCH_LIST_CAP,
): SearchList {
  const first = pages?.[0];
  const seen = new Set<number>();
  const hits: SearchHit[] = [];
  for (const page of pages ?? []) {
    for (const hit of page.items) {
      if (seen.has(hit.call.id)) continue;
      seen.add(hit.call.id);
      hits.push(hit);
    }
  }
  const total = first?.total ?? 0;
  const totalCapped = first?.total_capped ?? false;
  const kept = hits.slice(0, cap);
  return {
    hits: kept,
    total,
    totalCapped,
    capped: kept.length >= cap && (totalCapped || total > kept.length),
  };
}
