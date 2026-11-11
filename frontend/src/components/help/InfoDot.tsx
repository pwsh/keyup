import { Info } from "lucide-react";
import { Link } from "react-router";
import { HELP, type HelpId, useHelpText } from "../../help/help";
import { cn } from "../../lib/cn";
import { Tip } from "../ui/Tooltip";

/**
 * The width and height of the button, in pixels. A 16 px glyph inside a 24 px
 * box: the icon stays quiet next to a 12 px label, while the thing the mouse
 * has to hit is the box. Exported so a test can assert the hit target rather
 * than re-typing the number.
 */
export const INFO_DOT_HIT_PX = 24;

/**
 * The "i" beside a label. It is focusable so the explanation is reachable from
 * the keyboard — Radix opens the tooltip on focus as well as on hover — and it
 * carries the label's own words in `aria-label` so a screen reader hears what
 * it explains rather than a bare "info".
 *
 * The box is deliberately larger than the glyph: at 16 px square the dot was
 * a target an operator had to aim at. The negative margin keeps that larger box
 * from pushing the label row taller, so the extra area is bought from the
 * padding around the line rather than from the layout.
 */
export function InfoDot({ id, to, className }: { id: HelpId; to?: string; className?: string }) {
  // The text is a lazily loaded chunk (`help/help.ts`): "" for a moment, then the sentence.
  const text = useHelpText(id);
  const dotClass = cn(
    "-my-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-muted",
    "transition-colors hover:bg-surface-2 hover:text-ink focus-visible:text-ink",
    className,
  );
  const glyph = <Info size={16} aria-hidden="true" />;

  // A few fields need more than a sentence — what belongs in a category, what
  // RadioReference means by a tag — and for those the dot is a link to the
  // section of `/help` that says so. The tooltip still carries the sentence, so
  // the link is the *extra*, never the only way to find out what the field is.
  if (to !== undefined) {
    return (
      <Tip label={`${HELP[id]} — open the full explanation`}>
        <Link
          to={to}
          data-testid="info-dot"
          aria-label={`What this does: ${text}. Opens the help page.`}
          className={dotClass}
        >
          {glyph}
        </Link>
      </Tip>
    );
  }

  return (
    <Tip label={HELP[id]}>
      <button
        type="button"
        data-testid="info-dot"
        aria-label={`What this does: ${text}`}
        className={dotClass}
      >
        {glyph}
      </button>
    </Tip>
  );
}
