import { useEffect, useState } from "react";
import { useAudioProfiles, useUpdateTalkgroup } from "../../api/hooks";
import {
  RR_SERVICE_TAGS,
  TALKGROUP_CATEGORIES,
  TALKGROUP_PRIORITY_MAX,
  TALKGROUP_PRIORITY_MIN,
  type Talkgroup,
  type TalkgroupCategory,
} from "../../api/types";
import { HELP } from "../../help/help";
import { CATEGORY_LABELS } from "../../lib/format";
import { mutationErrorToast, toast } from "../../state/toasts";
import { helpHref } from "../help/TalkgroupFields";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, inputClass, TextArea, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { Switch } from "../ui/Switch";
import { AgencySelect } from "./AgencySelect";
import { NOT_TRUNKED_TIP, useTrunkedSystemIds } from "./trunked";

/**
 * The "no service tag" entry's value. Not the empty string, which a Radix select
 * keeps for "nothing is selected" — an item using it cannot be chosen back. No
 * RadioReference tag is spelled this way, so the sentinel cannot collide.
 */
const NO_SERVICE_TAG = "none";

/**
 * The same trick for the audio profile, whose values are row ids rendered as strings.
 * "0" is not a row id, so it cannot collide with a real profile.
 */
const NO_AUDIO_PROFILE = "0";

/** Inline edit for one channel's name, category, color and policies. */
export function EditTalkgroupDialog({
  talkgroup,
  open,
  onOpenChange,
}: {
  talkgroup: Talkgroup | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const update = useUpdateTalkgroup();
  const trunkedSystems = useTrunkedSystemIds();
  const profiles = useAudioProfiles(open);
  const [draft, setDraft] = useState<Talkgroup | null>(talkgroup);
  useEffect(() => {
    setDraft(talkgroup);
  }, [talkgroup]);

  if (!draft) return null;
  // Broadcastify Calls takes trunked systems only; on anything else the box is shown
  // fixed off with the reason rather than offered and then quietly refused.
  const trunked = trunkedSystems.has(draft.system_id);
  const set = <K extends keyof Talkgroup>(key: K, value: Talkgroup[K]) =>
    setDraft((previous) => (previous ? { ...previous, [key]: value } : previous));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit ${talkgroup?.alias ?? "talkgroup"}`}
      description="Changes apply everywhere in this app straight away; the alias recorded on each past call is kept as it was."
      // Fourteen controls. At the dialog's default 38rem they were a single column
      // of eight rows plus two rows of switches, and the whole form scrolled inside
      // its own 85vh box on a laptop. Three columns from `lg` up puts all of it on
      // one screen; the width still collapses to the viewport on a phone.
      className="w-[min(60rem,calc(100vw-2rem))]"
      footer={
        <>
          <Button tip="Close without saving" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["talkgroups.edit.save"]}
            size="sm"
            variant="primary"
            disabled={update.isPending}
            onClick={() =>
              update.mutate(
                {
                  id: draft.id,
                  patch: {
                    alias: draft.alias,
                    category: draft.category,
                    service_tag: draft.service_tag,
                    agency_code: draft.agency_code,
                    color: draft.color,
                    priority: draft.priority,
                    transcribe: draft.transcribe,
                    transcribe_min_seconds: draft.transcribe_min_seconds,
                    retention_days: draft.retention_days,
                    audio_profile_id: draft.audio_profile_id,
                    is_hidden: draft.is_hidden,
                    shared: draft.shared,
                    export_broadcastify: draft.export_broadcastify,
                    export_bcfy_calls: draft.export_bcfy_calls,
                    export_openmhz: draft.export_openmhz,
                    notes: draft.notes,
                  },
                },
                {
                  onSuccess: () => {
                    toast("Talkgroup saved", { tone: "success" });
                    onOpenChange(false);
                  },
                  onError: (error) => mutationErrorToast(error, "Could not save the talkgroup"),
                },
              )
            }
          >
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Field label="Alias" help="talkgroups.edit.alias" className="sm:col-span-2 lg:col-span-3">
          {(id) => (
            <TextInput
              id={id}
              value={draft.alias ?? ""}
              placeholder={`TG ${draft.tgid}`}
              onChange={(event) => set("alias", event.target.value)}
            />
          )}
        </Field>

        <Field label="Category" help="talkgroups.edit.category" helpTo={helpHref("category")}>
          {() => (
            <Select
              value={draft.category ?? "other"}
              onValueChange={(value) => set("category", value as TalkgroupCategory)}
              options={TALKGROUP_CATEGORIES.map((category) => ({
                value: category,
                label: CATEGORY_LABELS[category] ?? category,
              }))}
              tip={HELP["talkgroups.edit.category"]}
              ariaLabel="Category"
              className="w-full"
            />
          )}
        </Field>

        <Field label="Service tag" help="talkgroups.edit.serviceTag" helpTo={helpHref("serviceTag")}>
          {() => (
            <Select
              // Most channels have no service tag — nobody has synced or classified
              // them — so "none" is a real choice a reader has to be able to pick
              // again after a wrong one, not just an initial state. The API takes
              // null for it.
              value={draft.service_tag ?? NO_SERVICE_TAG}
              onValueChange={(value) => set("service_tag", value === NO_SERVICE_TAG ? null : value)}
              options={[
                { value: NO_SERVICE_TAG, label: "— none —" },
                ...RR_SERVICE_TAGS.map((tag) => ({ value: tag, label: tag })),
              ]}
              tip={HELP["talkgroups.edit.serviceTag"]}
              ariaLabel="Service tag"
              className="w-full"
            />
          )}
        </Field>

        <Field label="Agency" help="talkgroups.edit.agency" helpTo={helpHref("agency")}>
          {() => (
            <AgencySelect
              value={draft.agency_code}
              onValueChange={(code) => set("agency_code", code)}
              tip={HELP["talkgroups.edit.agency"]}
              ariaLabel="Agency"
            />
          )}
        </Field>

        <Field label="Priority" help="talkgroups.edit.priority">
          {(id) => (
            <TextInput
              id={id}
              type="number"
              // The API bound: a live job inherits this number and the backlog
              // re-runs it 500 higher, in a queue that stops at 999.
              min={TALKGROUP_PRIORITY_MIN}
              max={TALKGROUP_PRIORITY_MAX}
              value={draft.priority}
              onChange={(event) => set("priority", Number(event.target.value))}
            />
          )}
        </Field>

        <Field label="Audio profile" help="talkgroups.edit.audioProfile">
          {() => (
            <Select
              // Almost every channel is on the global chain, so "none" has to be a state a
              // reader can get back to after picking a profile by mistake — the same reason
              // the service tag carries a sentinel rather than the empty string.
              value={draft.audio_profile_id === null ? NO_AUDIO_PROFILE : String(draft.audio_profile_id)}
              onValueChange={(value) =>
                set("audio_profile_id", value === NO_AUDIO_PROFILE ? null : Number(value))
              }
              options={[
                { value: NO_AUDIO_PROFILE, label: "— none —" },
                ...(profiles.data ?? []).map((profile) => ({
                  value: String(profile.id),
                  label: profile.name,
                })),
              ]}
              tip={HELP["talkgroups.edit.audioProfile"]}
              ariaLabel="Audio profile"
              className="w-full"
            />
          )}
        </Field>

        <Field label="Color" help="talkgroups.edit.color">
          {(id) => (
            <input
              id={id}
              type="color"
              value={draft.color ?? "#2a78d6"}
              onChange={(event) => set("color", event.target.value)}
              className={`${inputClass} p-1`}
            />
          )}
        </Field>

        <Field label="Minimum length to transcribe" help="talkgroups.edit.minSeconds">
          {(id) => (
            <TextInput
              id={id}
              type="number"
              step={0.25}
              min={0}
              // Null means "use the global default"; the placeholder says so.
              value={draft.transcribe_min_seconds ?? ""}
              placeholder="default"
              onChange={(event) =>
                set("transcribe_min_seconds", event.target.value === "" ? null : Number(event.target.value))
              }
            />
          )}
        </Field>

        <Field label="Retention (days)" help="talkgroups.edit.retention">
          {(id) => (
            <TextInput
              id={id}
              type="number"
              min={1}
              value={draft.retention_days ?? ""}
              placeholder="Follow the global policy"
              onChange={(event) =>
                set("retention_days", event.target.value === "" ? null : Number(event.target.value))
              }
            />
          )}
        </Field>

        <div className="flex flex-wrap items-center gap-4 sm:col-span-2 lg:col-span-3">
          <Switch
            checked={draft.transcribe}
            onCheckedChange={(next) => set("transcribe", next)}
            label="Transcribe this channel"
            tip={HELP["talkgroups.edit.transcribe"]}
          />
          <Switch
            checked={draft.is_hidden}
            onCheckedChange={(next) => set("is_hidden", next)}
            label="Hide from default views"
            tip={HELP["talkgroups.edit.hidden"]}
          />
          <Switch
            checked={draft.shared}
            onCheckedChange={(next) => set("shared", next)}
            label="Shared county channel"
            tip={HELP["talkgroups.edit.shared"]}
          />
        </div>

        {/* The three export checkboxes. They do nothing on their own — the exporters read
            them only in `allow`/`deny` mode — and the Calls one is fixed off where Calls
            could never publish, rather than offered and then refused. */}
        <div className="flex flex-wrap items-center gap-4 sm:col-span-2 lg:col-span-3">
          <Switch
            checked={draft.export_broadcastify}
            onCheckedChange={(next) => set("export_broadcastify", next)}
            label="Carry on the Broadcastify Feed"
            tip={HELP["talkgroups.table.exportBroadcastify"]}
          />
          <Switch
            checked={trunked ? draft.export_bcfy_calls : false}
            disabled={!trunked}
            onCheckedChange={(next) => set("export_bcfy_calls", next)}
            label="Publish to Broadcastify Calls"
            tip={trunked ? HELP["talkgroups.table.exportBcfyCalls"] : NOT_TRUNKED_TIP}
          />
          <Switch
            checked={draft.export_openmhz}
            onCheckedChange={(next) => set("export_openmhz", next)}
            label="Publish to OpenMHz"
            tip={HELP["talkgroups.table.exportOpenmhz"]}
          />
        </div>

        <Field label="Notes" help="talkgroups.edit.notes" className="sm:col-span-2 lg:col-span-3">
          {(id) => (
            <TextArea
              id={id}
              rows={2}
              value={draft.notes ?? ""}
              onChange={(event) => set("notes", event.target.value)}
            />
          )}
        </Field>
      </div>
    </Dialog>
  );
}
