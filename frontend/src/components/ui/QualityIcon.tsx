import { CircleCheck, CircleDashed, CircleMinus, CircleX, Lock, MicOff, TriangleAlert } from "lucide-react";
import type { ComponentType } from "react";
import type { TranscriptQuality } from "../../api/types";
import { cn } from "../../lib/cn";
import type { NoTranscriptReason } from "../../lib/transcriptReason";
import { Tip } from "./Tooltip";

/**
 * A 24 px hit area around a mark smaller than that (WCAG 2.5.8), without moving it:
 * an absolutely positioned `::before` that grows the box the pointer can land on
 * while the glyph keeps its size and its place in the row.
 */
export const HIT_24 =
  "relative before:absolute before:left-1/2 before:top-1/2 before:size-6 before:-translate-x-1/2 before:-translate-y-1/2 before:content-['']";

/** Neutral glyphs for a call that has no transcript: none of these is a fault. */
const REASON_ICONS: Record<NoTranscriptReason, ComponentType<{ size?: number }>> = {
  encrypted: Lock,
  "no speech": MicOff,
  duplicate: CircleMinus,
  "not transcribed": CircleMinus,
  queued: CircleDashed,
  transcribing: CircleDashed,
  "transcription failed": CircleMinus,
};

/** One glyph per confidence level, colored by the same tokens as the quality dot. */
const ICONS: Record<TranscriptQuality, { Icon: ComponentType<{ size?: number }>; tone: string }> = {
  good: { Icon: CircleCheck, tone: "text-good-ink" },
  suspect: { Icon: TriangleAlert, tone: "text-warning-ink" },
  noise: { Icon: CircleX, tone: "text-critical-ink" },
};

const WHY: Record<TranscriptQuality, string> = {
  good: "The transcript looks reliable",
  suspect: "The transcript may be wrong — odd model scores, or audio too short to be sure",
  noise: "The engine almost certainly hallucinated: treat this transcript as empty",
};

/**
 * Transcript confidence as a compact icon, for a dense list row where the word would
 * cost too much width. Color is never the only cue: the tooltip and the screen-reader
 * text both carry the word ("good"/"suspect"/"noise").
 */
export function QualityIcon({
  quality,
  reason,
  className,
}: {
  quality: TranscriptQuality | null;
  /**
   * Why there is no transcript, when there is none. A call with no words gets a
   * neutral glyph for its reason — a lock, a muted mic — rather than the red ✕,
   * which is kept for a transcript the archive judged to be noise.
   */
  reason?: NoTranscriptReason | null;
  className?: string;
}) {
  if (quality === null) {
    if (reason) {
      const Icon = REASON_ICONS[reason];
      return (
        <Tip label={`No transcript — ${reason}`}>
          <span tabIndex={0} className={cn("inline-flex text-muted", HIT_24, className)} data-reason={reason}>
            <Icon size={13} />
            <span className="sr-only">{`No transcript: ${reason}`}</span>
          </span>
        </Tip>
      );
    }
    return (
      <Tip label="No transcript, so there is nothing to judge">
        <span tabIndex={0} className={cn("inline-flex text-muted", HIT_24, className)} aria-hidden="false">
          <span aria-hidden="true">–</span>
          <span className="sr-only">No transcript</span>
        </span>
      </Tip>
    );
  }
  const { Icon, tone } = ICONS[quality];
  return (
    <Tip label={`${quality} — ${WHY[quality]}`}>
      <span tabIndex={0} className={cn("inline-flex", tone, HIT_24, className)}>
        <Icon size={14} />
        <span className="sr-only">{`Transcript quality: ${quality}`}</span>
      </span>
    </Tip>
  );
}
