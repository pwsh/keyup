import type { Call } from "../api/types";

/**
 * Why a call has no transcript text, in the reader's words.
 *
 * The raw status ("empty", "skipped") read like an error and said nothing about the
 * cause — an encrypted call, which nobody can transcribe, looked broken. Each value
 * here is something the reader can act on or at least understand:
 *
 * - `encrypted` — the call was encrypted; there was no audio to transcribe.
 * - `no speech` — it was transcribed (or measured) and nobody was talking; a
 *   transcript the engine produced and the archive rejected as noise ends here too.
 * - `duplicate` — another recorder's copy of the same transmission carries the text.
 * - `not transcribed` — skipped by policy: the talkgroup is not transcribed, the call
 *   is shorter than the minimum, or its audio is gone.
 * - `queued` / `transcribing` / `transcription failed` — the pipeline's own state.
 */
export type NoTranscriptReason =
  | "encrypted"
  | "no speech"
  | "duplicate"
  | "not transcribed"
  | "queued"
  | "transcribing"
  | "transcription failed";

type ReasonFields = Pick<Call, "encrypted" | "transcript_status" | "duplicate_of" | "audio_meta">;

export function noTranscriptReason(call: ReasonFields): NoTranscriptReason {
  if (call.encrypted === true) return "encrypted";
  switch (call.transcript_status) {
    case "empty":
    case "done":
      return "no speech";
    case "failed":
      return "transcription failed";
    case "running":
      return "transcribing";
    case "pending":
    case "queued":
      return "queued";
    default:
      if (call.audio_meta?.silent === true) return "no speech";
      if (call.duplicate_of !== null && call.duplicate_of !== undefined) return "duplicate";
      return "not transcribed";
  }
}
