import type { TranscriptQuality } from "../../api/types";
import { cn } from "../../lib/cn";
import { HIT_24 } from "./QualityIcon";
import { Tip } from "./Tooltip";

const TONE: Record<TranscriptQuality, string> = {
  good: "bg-good",
  suspect: "bg-warning",
  noise: "bg-critical",
};

const WHY: Record<TranscriptQuality, string> = {
  good: "The transcript looks reliable",
  suspect: "The transcript may be wrong — odd model scores, or audio too short to be sure",
  noise: "The engine almost certainly hallucinated: treat this transcript as empty",
};

/**
 * Transcript confidence as a dot. Color is never the only cue — the tooltip and
 * the screen-reader text both say the word.
 */
export function QualityDot({
  quality,
  reasons,
  className,
}: {
  quality: TranscriptQuality | null;
  reasons?: string[];
  className?: string;
}) {
  if (quality === null) {
    return (
      <Tip label="No transcript, so there is nothing to judge">
        <span
          tabIndex={0}
          className={cn("inline-block h-2 w-2 rounded-full border border-control-border", HIT_24, className)}
        >
          <span className="sr-only">No transcript</span>
        </span>
      </Tip>
    );
  }
  const label = reasons?.length ? `${WHY[quality]} — ${reasons.join("; ")}` : WHY[quality];
  return (
    <Tip label={label}>
      <span
        tabIndex={0}
        className={cn("inline-block h-2 w-2 rounded-full", TONE[quality], HIT_24, className)}
      >
        <span className="sr-only">{`Transcript quality: ${quality}`}</span>
      </span>
    </Tip>
  );
}
