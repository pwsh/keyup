import type { SettingDefinition } from "../../api/types";

/**
 * How the Settings tab arranges what the API sends it.
 *
 * `GET /admin/settings` hands back a flat list whose only structure is a `group` string,
 * and a group is too coarse to read: "Transcription" is ten knobs answering four unrelated
 * questions (what gets queued, what the prompt says, how the audio is conditioned, what
 * happens when a job fails), and rendered as one run of fields they are indistinguishable.
 * The map below is the second level the API does not carry — deliberately *here* rather
 * than in `SETTINGS_SPEC`, because it is a statement about reading order on one screen,
 * not about what a setting means.
 *
 * It is a partial map on purpose. A key nobody has placed still renders: it falls into
 * `OTHER_SUBSECTION` at the foot of its group, so a setting added to the backend appears
 * the day it ships rather than waiting for a frontend edit. `settingsLayout.test.ts` is
 * what keeps that escape hatch from becoming the norm — it fails on a key here that no
 * longer exists (a rename, a typo) and on a shipped key that has drifted into "Other".
 */

/** Where an unplaced key lands: last in its group, under a heading that says so. */
export const OTHER_SUBSECTION = "Other";

/**
 * The order the groups are stacked in, top to bottom.
 *
 * It follows the life of a recording — what gets transcribed, how copies arriving from
 * several receivers are matched, how long any of it is kept — and leaves the things you
 * set once and forget (who may read, what the API will hand out, where events are posted)
 * at the bottom. A group the API sends that is missing here sorts after these,
 * alphabetically, so a new group is never dropped.
 */
export const GROUP_ORDER: string[] = [
  "Transcription",
  "Ingest",
  "Recorders",
  "Directory",
  "Upload",
  "Retention",
  "Health",
  "Access",
  "API",
  "Webhooks",
];

/** One line under each group heading saying what the group is for. */
export const GROUP_BLURB: Record<string, string> = {
  Transcription: "What reaches the speech engine, what it is told, and what happens to a failed job.",
  Ingest: "How recordings arriving from more than one receiver are matched, merged and threaded.",
  Recorders: "What keyup does on its own when a receiver stops reporting, or two of them overlap.",
  Directory: "Who the shared dispatch channels belong to when no single agency owns them.",
  Upload: "The flow control in front of the push API, so a backfill can never starve live traffic.",
  Retention:
    "How long calls, audio and radio-presence events are kept before the nightly purge, and when a filling disk raises an alert.",
  Health:
    "When the worker raises a health alert: a recorder gone quiet, calls or transcripts stalled, a backup overdue.",
  Access: "Who has to sign in before the archive will answer.",
  API: "Ceilings on what one export request may return or assemble.",
  Webhooks: "Where keyup posts an event when something a consumer can act on happens.",
};

/**
 * Group → its sub-sections, in render order, each naming the keys it holds.
 *
 * Keys are listed in the order they should appear *within* the sub-section, which is not
 * always the order the API sends them: a switch comes before the numbers that only matter
 * once it is on.
 */
export const SUBSECTIONS: Record<string, { title: string; keys: string[] }[]> = {
  Transcription: [
    { title: "Policy", keys: ["transcribe_default", "transcribe_min_seconds", "stt_language"] },
    { title: "Prompting", keys: ["stt_prompt_extra", "stt_places", "stt_word_timestamps"] },
    { title: "Hallucinations", keys: ["stt_hallucination_patterns"] },
    {
      title: "Audio conditioning",
      keys: ["stt_vad", "stt_normalize", "stt_normalize_always", "stt_normalize_filter"],
    },
    { title: "Backends/queue", keys: ["transcribe_max_attempts"] },
  ],
  Ingest: [
    { title: "Duplicates", keys: ["dedupe_window_seconds", "dedupe_duration_tolerance"] },
    {
      title: "Reconcile",
      keys: ["reconcile_enabled", "reconcile_clock_skew_seconds", "reconcile_min_overlap"],
    },
    { title: "Conversations", keys: ["conversation_gap_seconds"] },
    { title: "Watcher", keys: ["gap_minutes"] },
  ],
  Recorders: [
    {
      title: "Standby arbiter",
      keys: ["arbiter_enabled", "arbiter_stale_seconds", "arbiter_recovery_seconds"],
    },
    { title: "Overlapping receivers", keys: ["best_source_enabled"] },
  ],
  Upload: [
    { title: "Backlog switch", keys: ["upload_pause_backlog"] },
    { title: "Queue ceilings", keys: ["upload_max_queue_live", "upload_max_queue_backlog"] },
    {
      title: "Backlog pacing",
      keys: ["upload_backlog_max_eta_seconds", "upload_backlog_max_concurrency"],
    },
  ],
  Retention: [
    {
      title: "Calls",
      keys: [
        "retention_keep_days",
        "retention_keep_audio_days",
        "retention_keep_transcripts",
        "retention_archive_path",
      ],
    },
    { title: "Radio events", keys: ["radio_events_keep_days"] },
    {
      title: "Disk space alert",
      keys: [
        "disk_warn_free_percent",
        "disk_warn_free_gb",
        "disk_critical_free_percent",
        "disk_critical_free_gb",
      ],
    },
  ],
  Health: [
    { title: "Recording", keys: ["health_recorder_silent_minutes", "health_ingest_lag_minutes"] },
    { title: "Transcripts", keys: ["health_stt_backlog_minutes"] },
    { title: "Backups", keys: ["health_backup_max_age_hours"] },
  ],
  Access: [
    { title: "Who may read", keys: ["auth_require_read"] },
    // The two session deadlines belong together and apart from the read switch: one is
    // "who gets in", these are "how long they stay in" (docs/HTTPS.md, and app/auth.py).
    { title: "How long a sign-in lasts", keys: ["auth_session_days", "auth_idle_hours"] },
  ],
  API: [
    { title: "Limits", keys: ["export_max_rows"] },
    { title: "Exports", keys: ["export_max_calls", "export_max_bytes", "export_keep_days"] },
  ],
  Webhooks: [
    { title: "Endpoint", keys: ["webhook_url", "webhook_events", "webhook_call_scope"] },
    { title: "Signing", keys: ["webhook_secret"] },
    { title: "Delivery", keys: ["webhook_max_attempts"] },
  ],
};

export type SettingsSubsection = { title: string; items: SettingDefinition[] };
export type SettingsGroup = { group: string; subsections: SettingsSubsection[]; count: number };

/** Every key this map places, for the test that checks none of them has been renamed away. */
export function mappedKeys(): string[] {
  return Object.values(SUBSECTIONS).flatMap((sections) => sections.flatMap((section) => section.keys));
}

/**
 * Split one group's settings into its sub-sections.
 *
 * Anything the map does not name keeps the order the API sent it and lands in a trailing
 * "Other" — never dropped, and visibly unplaced so it gets a home next time somebody
 * looks. An empty sub-section (its keys are not in this deployment, or the filter removed
 * them) is left out rather than rendered as a heading over nothing, and a group nobody has
 * mapped at all is one untitled run of fields rather than a card whose only section is
 * called "Other".
 */
export function layoutGroup(group: string, items: SettingDefinition[]): SettingsSubsection[] {
  const byKey = new Map(items.map((item) => [item.key, item]));
  const placed = new Set<string>();
  const sections: SettingsSubsection[] = [];
  for (const section of SUBSECTIONS[group] ?? []) {
    const found: SettingDefinition[] = [];
    for (const key of section.keys) {
      const item = byKey.get(key);
      if (item === undefined) continue;
      found.push(item);
      placed.add(key);
    }
    if (found.length > 0) sections.push({ title: section.title, items: found });
  }
  const rest = items.filter((item) => !placed.has(item.key));
  if (rest.length > 0) {
    sections.push({ title: sections.length === 0 ? "" : OTHER_SUBSECTION, items: rest });
  }
  return sections;
}

/**
 * The whole tab's shape: the groups to render, in `GROUP_ORDER`, each already split.
 *
 * `hidden` is `DEDICATED_GROUPS` — the groups with a page of their own, which this tab
 * never renders — passed in rather than imported so the layout stays a pure function of
 * its arguments and the test does not have to stand the tab up to exercise it.
 */
export function layoutSettings(settings: SettingDefinition[], hidden: ReadonlySet<string>): SettingsGroup[] {
  const groups = new Map<string, SettingDefinition[]>();
  for (const setting of settings) {
    if (hidden.has(setting.group)) continue;
    const list = groups.get(setting.group) ?? [];
    list.push(setting);
    groups.set(setting.group, list);
  }
  const rank = (group: string) => {
    const index = GROUP_ORDER.indexOf(group);
    return index === -1 ? GROUP_ORDER.length : index;
  };
  return [...groups.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([group, items]) => ({ group, subsections: layoutGroup(group, items), count: items.length }));
}

/** The anchor id the index scrolls to, which is also the group heading's own id. */
export function groupAnchor(group: string): string {
  return `settings-group-${group.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}

/** True when a setting matches the filter box — by its label, its key or its description. */
export function settingMatches(setting: SettingDefinition, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return (
    setting.label.toLowerCase().includes(needle) ||
    setting.key.toLowerCase().includes(needle) ||
    (setting.description ?? "").toLowerCase().includes(needle)
  );
}
