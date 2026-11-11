/**
 * "Showing 1–200 of 4,318", and the two buttons that move through the rest.
 *
 * Every directory list is served by an endpoint that caps `page_size` at 500, so
 * a page that asks for 500 rows and renders whatever comes back is not showing
 * "everything" — it is showing the first 500 and saying nothing about the other
 * 3,818. Talkgroups, Units and Personnel all did exactly that. The count is the
 * point of this component; the buttons are what makes the count actionable.
 *
 * Lifted out of the radios page, which had this markup inline, so that the four
 * lists count and page identically rather than three of them not at all.
 */

import { ChevronLeft, ChevronRight } from "lucide-react";
import { HELP } from "../../help/help";
import { formatNumber } from "../../lib/format";
import { Button } from "./Button";
import { Tip } from "./Tooltip";

export function ListPager({
  page,
  pageSize,
  total,
  onPageChange,
  noun,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  /** Plural, for the screen-reader label: "talkgroups", "units", "people". */
  noun: string;
}) {
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);
  const hasPrev = page > 1;
  const hasNext = to < total;

  // The count is worth showing even on a single page — "of 37" is the answer to
  // "is that all of them?" — but a pair of dead buttons is not.
  return (
    <div className="mt-3 flex items-center justify-between gap-2">
      <Tip label={HELP["app.list.count"]}>
        <p tabIndex={0} className="text-xs tabular-nums text-ink-2">
          Showing <span className="font-semibold text-ink">{formatNumber(from)}</span>–
          <span className="font-semibold text-ink">{formatNumber(to)}</span> of{" "}
          <span className="font-semibold text-ink">{formatNumber(total)}</span> {noun}
        </p>
      </Tip>
      {hasPrev || hasNext ? (
        <div className="flex items-center gap-1.5">
          <Button
            tip={HELP["app.list.prev"]}
            size="sm"
            variant="ghost"
            disabled={!hasPrev}
            aria-label="Previous page"
            onClick={() => onPageChange(Math.max(1, page - 1))}
          >
            <ChevronLeft size={14} aria-hidden="true" /> Previous
          </Button>
          <Button
            tip={HELP["app.list.next"]}
            size="sm"
            variant="ghost"
            disabled={!hasNext}
            aria-label="Next page"
            onClick={() => onPageChange(page + 1)}
          >
            Next <ChevronRight size={14} aria-hidden="true" />
          </Button>
        </div>
      ) : null}
    </div>
  );
}
