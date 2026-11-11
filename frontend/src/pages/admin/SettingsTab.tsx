import { Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useSaveSettings, useSettings } from "../../api/hooks";
import type { SettingDefinition } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { Card, CardHeader } from "../../components/ui/Card";
import { inputClass } from "../../components/ui/Field";
import { SkeletonText } from "../../components/ui/Skeleton";
import { EmptyState } from "../../components/ui/States";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import { mutationErrorToast, toast } from "../../state/toasts";
import { SettingField } from "./SettingField";
import { GROUP_BLURB, groupAnchor, layoutSettings, settingMatches } from "./settingsLayout";

/**
 * The Settings tab is the generic form: every group of `SETTINGS_SPEC`, rendered by
 * `kind`. Three groups outgrew it. Broadcastify and OpenMHz need the context a
 * key/value list cannot carry — which service a knob belongs to, how that service's
 * queue is doing, and a confirm step in front of the switch that starts publishing to
 * somebody else's archive — so Admin → Exports owns them. The feed-owner poller belongs
 * on the page that shows what it collected, and Audio belongs beside the preview and the
 * reprocess button that act on it. Rendering a group in both places meant two forms with
 * two drafts over one table, where saving one silently discarded what was typed in the
 * other; the note at the foot of this tab says where each one went.
 */
export const DEDICATED_GROUPS: Record<string, { label: string; href: string; blurb: string }> = {
  Export: {
    label: "Exports tab",
    href: "/admin?tab=exports",
    blurb: "Broadcastify and OpenMHz settings live on the",
  },
  "Feed stats": {
    label: "Feed Stats page",
    href: "/feed-stats",
    blurb: "Broadcastify feed-owner settings live on the",
  },
  Audio: {
    label: "Audio tab",
    href: "/admin?tab=audio",
    blurb: "Audio processing settings live on the",
  },
  // Held back for the same reason as the three above, and one more: the group holds a
  // Cloudflare API token, and saving any of it re-configures the running proxy — that
  // belongs beside the certificate status and the token check, not in a generic form.
  HTTPS: {
    label: "HTTPS tab",
    href: "/admin?tab=https",
    blurb: "Certificates, domains and the Cloudflare token live on the",
  },
};

const HIDDEN_GROUPS: ReadonlySet<string> = new Set(Object.keys(DEDICATED_GROUPS));

/**
 * Admin → Settings: forty runtime knobs from eight unrelated groups, on one screen.
 *
 * The old form was those forty settings poured into a two-column grid under a thin group
 * heading, and the two columns were what broke it: the eye reads a grid across, so
 * "Dedupe window" sat beside "Reconcile overlap" and a group boundary halfway down the
 * left column was invisible. Finding a setting meant scrolling the whole page.
 *
 * So: one column, one card per group, an index down the left that says how many settings
 * each group holds and which of them you have unsaved edits in, and sub-sections inside a
 * card (`settingsLayout.ts`) so ten transcription knobs read as four short questions
 * rather than one list. The filter box searches label, key *and* description across every
 * group at once — the description is where the words an operator actually remembers live
 * ("hallucinated", "squelch tail") — and the index narrows with it, so a search says how
 * many hits are in each group rather than just scrolling you somewhere.
 *
 * Saving is unchanged: the draft holds only keys somebody touched, `PUT /admin/settings`
 * gets exactly those, and a blank secret is a key nobody touched, so it keeps what is
 * stored. The save button moved into a bar that appears at the foot of the viewport once
 * the draft is non-empty, because on a page this tall a button in the header is off-screen
 * by the time you have changed anything.
 */
export function SettingsTab({ canWrite }: { canWrite: boolean }) {
  const settings = useSettings();
  const save = useSaveSettings();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [query, setQuery] = useState("");

  const rows = useMemo(() => settings.data ?? [], [settings.data]);

  /**
   * Write one field into the draft — or take it back out when it has been typed back to
   * what is stored. Without that, editing a number and undoing it left the page claiming
   * an unsaved change and re-sent a value the row already had.
   */
  const setValue = (setting: SettingDefinition, next: string) =>
    setDraft((current) => {
      const { [setting.key]: _dropped, ...rest } = current;
      return next === setting.value ? rest : { ...rest, [setting.key]: next };
    });

  /** Every group as it is rendered, after the filter. */
  const groups = useMemo(
    () =>
      layoutSettings(
        rows.filter((setting) => settingMatches(setting, query)),
        HIDDEN_GROUPS,
      ),
    [rows, query],
  );

  /**
   * Which groups hold an unsaved edit — read from the *unfiltered* list on purpose, so
   * typing in the filter box cannot hide the fact that a group off-screen has been
   * changed and is about to be saved.
   */
  const dirtyGroups = useMemo(() => {
    const groupOf = new Map(rows.map((setting) => [setting.key, setting.group]));
    return new Set(Object.keys(draft).map((key) => groupOf.get(key) ?? ""));
  }, [rows, draft]);

  /** The held-back groups, in the order the API sent them, so the note names only the
      ones this deployment actually has. */
  const elsewhere = useMemo(() => {
    const seen: string[] = [];
    for (const setting of rows) {
      if (HIDDEN_GROUPS.has(setting.group) && !seen.includes(setting.group)) seen.push(setting.group);
    }
    return seen;
  }, [rows]);

  const changed = Object.keys(draft).length;

  if (settings.isLoading) {
    return (
      <Card>
        <CardHeader title="Runtime settings" hint={HELP["admin.settings.form"]} />
        <SkeletonText lines={8} className="p-4" />
      </Card>
    );
  }

  return (
    <div className="space-y-3">
      {/* The page's own heading and filter, not a card: nothing below it belongs to it
          more than to the groups, and a frame around a heading alone is a box for its own
          sake. */}
      <CardHeader
        className="border-b-0 px-0 pt-0 pb-1"
        title="Runtime settings"
        hint={HELP["admin.settings.form"]}
        actions={
          <div className="relative">
            <Search
              size={13}
              aria-hidden="true"
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted"
            />
            <input
              type="search"
              value={query}
              aria-label="Filter settings"
              placeholder="Filter settings…"
              onChange={(event) => setQuery(event.target.value)}
              className={cn(inputClass, "w-56 pl-7")}
            />
          </div>
        }
      />

      <div className="grid gap-3 lg:grid-cols-[11rem_minmax(0,1fr)] lg:items-start">
        <SettingsIndex groups={groups} dirtyGroups={dirtyGroups} />

        <div className="min-w-0 space-y-3">
          {groups.length === 0 ? (
            <Card>
              <EmptyState
                title="No setting matches that"
                hint="The filter looks at the label, the key and the description of every group on this tab."
                action={
                  <Button tip={HELP["admin.settings.filterClear"]} size="sm" onClick={() => setQuery("")}>
                    <X size={13} aria-hidden="true" /> Clear filter
                  </Button>
                }
              />
            </Card>
          ) : (
            groups.map(({ group, subsections, count }) => (
              <Card key={group} id={groupAnchor(group)} className="scroll-mt-20">
                <CardHeader
                  title={
                    <span className="flex items-center gap-2">
                      {group}
                      <span className="text-[11px] font-normal text-muted">
                        {count} setting{count === 1 ? "" : "s"}
                      </span>
                    </span>
                  }
                  hint={GROUP_BLURB[group]}
                />
                {subsections.map((section) => (
                  <div key={section.title || group}>
                    {section.title ? (
                      <h3 className="border-t border-line bg-surface-2/50 px-3 py-1 text-xs font-medium text-ink-2 first:border-t-0">
                        {section.title}
                      </h3>
                    ) : null}
                    <div>
                      {section.items.map((setting) => (
                        <SettingField
                          key={setting.key}
                          variant="row"
                          setting={setting}
                          value={draft[setting.key] ?? setting.value}
                          dirty={setting.key in draft}
                          disabled={!canWrite}
                          onChange={(next) => setValue(setting, next)}
                          // A secret's stored value never comes back, so "put it back to
                          // the default" could not tell "clear it" from "leave it be".
                          onReset={
                            setting.is_default || setting.kind === "secret"
                              ? undefined
                              : () => setValue(setting, setting.default_value)
                          }
                        />
                      ))}
                    </div>
                  </div>
                ))}
              </Card>
            ))
          )}

          {elsewhere.length > 0 ? (
            <div className="space-y-1 px-1 text-[11px] leading-snug text-muted">
              {elsewhere.map((group) => (
                <p key={group}>
                  {DEDICATED_GROUPS[group]?.blurb}{" "}
                  <Link to={DEDICATED_GROUPS[group]?.href ?? ""} className="text-accent hover:underline">
                    {DEDICATED_GROUPS[group]?.label}
                  </Link>
                  , beside the status they act on.
                </p>
              ))}
            </div>
          ) : null}
        </div>
      </div>

      {changed > 0 ? (
        <div className="sticky bottom-2 z-20 flex flex-wrap items-center gap-2 rounded-md border border-accent bg-surface px-3 py-2 shadow-lg">
          <span className="text-xs text-ink">
            {changed} unsaved change{changed === 1 ? "" : "s"}
          </span>
          <span className="ml-auto flex items-center gap-2">
            <Button
              tip={HELP["admin.settings.discard"]}
              size="sm"
              disabled={save.isPending}
              onClick={() => setDraft({})}
            >
              Discard
            </Button>
            <Button
              tip={canWrite ? HELP["admin.settings.save"] : HELP["app.readOnly"]}
              size="sm"
              variant="primary"
              disabled={!canWrite || save.isPending}
              onClick={() =>
                save.mutate(draft, {
                  onSuccess: () => {
                    toast("Settings saved", { tone: "success" });
                    setDraft({});
                  },
                  onError: (error) => mutationErrorToast(error, "Could not save the settings"),
                })
              }
            >
              Save changes
            </Button>
          </span>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The group index: a link per group with its count and, where there are unsaved edits, a
 * dot. Plain anchors rather than scroll handlers, so it works with the keyboard, with a
 * middle click, and in a test that has no layout engine to scroll.
 */
function SettingsIndex({
  groups,
  dirtyGroups,
}: {
  groups: { group: string; count: number }[];
  dirtyGroups: ReadonlySet<string>;
}) {
  if (groups.length === 0) return null;
  return (
    <nav aria-label="Settings groups" className="lg:sticky lg:top-16">
      <ul className="flex flex-wrap gap-1 lg:flex-col lg:gap-0.5">
        {groups.map(({ group, count }) => (
          <li key={group}>
            <a
              href={`#${groupAnchor(group)}`}
              className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink"
            >
              {dirtyGroups.has(group) ? (
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
                  title="Unsaved changes in this group"
                />
              ) : (
                <span className="h-1.5 w-1.5 shrink-0" aria-hidden="true" />
              )}
              <span className="truncate">{group}</span>
              <span className="ml-auto text-[10px] text-muted">{count}</span>
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
