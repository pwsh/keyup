/**
 * One call as plain text, for pasting into a CAD note, an email or a chat window.
 *
 * Shared by the calls table's row action and the call detail page, so the two can never
 * drift into describing the same call differently. Deliberately plain: no markdown, no
 * table alignment, nothing that depends on the receiving application rendering it.
 *
 * Every field is optional and every empty one is dropped rather than printed as an em
 * dash — a summary of what is known reads better than a form with blanks in it.
 */

import { categoryLabel, formatDateTime, formatDuration, formatFrequency } from "./format";

/**
 * What the formatter needs, structurally — satisfied by both `Call` (the list row) and
 * `CallDetail`, which carry these under the same names.
 */
export interface CallSummaryInput {
  id: number;
  /** The wall clock the dispatcher lived; `started_at` is the UTC fallback. */
  started_local?: string | null;
  started_at?: string | null;
  system_name?: string | null;
  site_name?: string | null;
  tgid?: number | null;
  talkgroup_alias?: string | null;
  category?: string | null;
  service_tag?: string | null;
  patch_label?: string | null;
  frequency_hz?: number | null;
  /** Unit id, alias, or the radio id — whatever the archive knows this radio by. */
  radio_label?: string | null;
  /** The radio id as the air carried it: `5250041`, or a qualified `781824.57.5701025`. */
  radio_raw?: string | null;
  /** `unit` for a P25 private call, which has a radio at the far end instead of a talkgroup. */
  kind?: string | null;
  /** The far end of a private call, labeled the way the near end is. */
  target_radio_label?: string | null;
  talker_alias?: string | null;
  /** Every radio that keyed up inside the call, when the recorder said (§1.7). */
  sources?: { radio_label?: string | null; src?: number | null }[];
  duration_s?: number | null;
  transcript_text?: string | null;
}

const clean = (value: string | null | undefined): string => (value ?? "").trim();

interface RadioRef {
  /** the id the air carried */
  id: string;
  /** the name the archive knows it by, when it has one */
  alias: string;
}

/** One radio as `5250041 (RC ENG 1)` — the id first, because that is what identifies it. */
function radioText({ id, alias }: RadioRef): string {
  if (id && alias && id !== alias) return `${id} (${alias})`;
  return id || alias;
}

/**
 * Every radio heard on this call, in the order they keyed up, each with both halves of
 * its identity and none of them twice.
 *
 * A radio arrives here from two places at once — the call's own `radio_raw`/`radio_label`
 * pair, and the `sources` list a multi-source recorder posts — and the two halves are not
 * always on the same sighting: the row may know the alias while the source knows only the
 * number. So sightings are merged when *either* half matches rather than compared as
 * rendered strings, which would print one radio twice under two spellings.
 */
function radioRefs(call: CallSummaryInput): RadioRef[] {
  const refs: RadioRef[] = [];
  const add = (rawId: string, rawAlias: string): void => {
    const id = clean(rawId);
    const alias = clean(rawAlias);
    if (!id && !alias) return;
    const seen = refs.find(
      (ref) =>
        (id && ref.id === id) || (alias && ref.alias === alias) || ref.id === alias || ref.alias === id,
    );
    if (seen) {
      if (!seen.id) seen.id = id;
      if (!seen.alias) seen.alias = alias;
      return;
    }
    refs.push({ id, alias });
  };
  add(call.radio_raw ?? "", call.radio_label ?? "");
  for (const source of call.sources ?? []) {
    add(source.src != null ? String(source.src) : "", source.radio_label ?? "");
  }
  // A label that is only the number again is not an alias; drop the repetition.
  for (const ref of refs) if (ref.alias === ref.id) ref.alias = "";
  return refs;
}

/** A one-call plaintext summary: identity, when, where, who, and what was said. */
export function callSummary(call: CallSummaryInput): string {
  const lines: string[] = [];
  const add = (label: string, value: string): void => {
    if (value) lines.push(`${label}: ${value}`);
  };

  const when = call.started_local || call.started_at;
  lines.push(`Call ${call.id}${when ? ` — ${formatDateTime(when)}` : ""}`);

  add("System", [clean(call.system_name), clean(call.site_name)].filter(Boolean).join(" / "));

  const alias = clean(call.talkgroup_alias);
  const tgid = call.tgid != null ? String(call.tgid) : "";
  add("Talkgroup", tgid && alias ? `${tgid} (${alias})` : tgid || alias);
  add("Patch", clean(call.patch_label));

  // The local bucket and RadioReference's own word for the channel say different things,
  // and an operator pasting this wants whichever ones somebody actually set.
  add("Category", clean(call.category) ? categoryLabel(clean(call.category)) : "");
  add("Service tag", clean(call.service_tag));

  add("Frequency", call.frequency_hz != null ? formatFrequency(call.frequency_hz) : "");

  // Who was talking, which the summary used to leave out on the one call where it is the
  // *only* identity there is: a private call is on no talkgroup at all, so without both
  // radios the paste says nothing about who it was between.
  const refs = radioRefs(call);
  add(refs.length > 1 ? "Radios" : "Radio", refs.map(radioText).join(", "));
  add(call.kind === "unit" ? "To radio" : "Target radio", clean(call.target_radio_label));
  add("Talker alias", clean(call.talker_alias));
  add("Duration", call.duration_s != null ? formatDuration(call.duration_s) : "");

  const transcript = clean(call.transcript_text);
  // On its own paragraph: it is prose, not a field, and it is what the reader came for.
  if (transcript) lines.push("", transcript);
  return lines.join("\n");
}
