/**
 * "Change these settings on all of them" — one drawer, four grids.
 *
 * The whole problem of a bulk-edit form is that an empty box is ambiguous: does a
 * blank *Rank* mean "leave everyone's rank alone" or "everyone has no rank now"?
 * Both are things an operator wants, and guessing wrong silently erases a roster.
 * So every field here is explicitly tri-state — **leave unchanged / set to … /
 * clear** — and only the fields deliberately moved off "leave unchanged" are sent.
 * That maps exactly onto the endpoint's semantics: a key absent from `changes` is
 * not written, and a key present with `null` clears the column.
 *
 * One engine driven by a field spec rather than four hand-written forms, because
 * the tri-state and the "only send what was chosen" rule are the entire substance
 * of all four; writing them out four times would be four chances for one page to
 * quietly mean something different. The four `BulkEdit*Drawer` exports below are
 * the specs — which fields, in what order, with which help text.
 *
 * What is *not* here is as deliberate: a talkgroup's alias, a radio's alias and
 * unit id, and a unit's callsign. Each names one particular row, and the server
 * refuses the last three outright (422). Offering a control that always errors is
 * worse than not offering it.
 */

import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import {
  useAudioProfiles,
  useBulkUpdatePersonnel,
  useBulkUpdateRadios,
  useBulkUpdateTalkgroups,
  useBulkUpdateUnits,
  useVocabulary,
} from "../../api/hooks";
import type { PersonPatch, RadioPatch, TalkgroupPatch, UnitPatch } from "../../api/types";
import {
  RADIO_KINDS,
  RR_SERVICE_TAGS,
  TALKGROUP_CATEGORIES,
  TALKGROUP_PRIORITY_MAX,
  TALKGROUP_PRIORITY_MIN,
  UNIT_KINDS,
} from "../../api/types";
import { HELP, type HelpId } from "../../help/help";
import { cn } from "../../lib/cn";
import { CATEGORY_LABELS, formatNumber } from "../../lib/format";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Drawer } from "../ui/Drawer";
import { inputClass } from "../ui/Field";
import { Select, type SelectOption } from "../ui/Select";
import { Switch } from "../ui/Switch";
import { Tip } from "../ui/Tooltip";
import { AgencySelect } from "./AgencySelect";

/* ------------------------------------------------------------------ the spec */

interface FieldBase {
  /** The `changes` key, spelled exactly as the API's patch model spells it. */
  key: string;
  label: string;
  help: HelpId;
  /**
   * Whether "Clear" is offered. False for a column the database will not take a
   * null in — a switch is on or off, and there is no third thing to mean.
   */
  clearable?: boolean;
}

export type BulkField =
  | (FieldBase & { kind: "text"; placeholder?: string })
  | (FieldBase & { kind: "number"; min?: number; max?: number; step?: number })
  | (FieldBase & { kind: "color" })
  | (FieldBase & { kind: "switch" })
  /**
   * `numeric` is for a select whose options are row ids — the audio profile. Without it
   * the chosen value would go up as the string it is in the DOM, and a foreign key sent
   * as `"2"` is a 422 the reader cannot act on.
   */
  | (FieldBase & { kind: "select"; options: SelectOption[]; numeric?: boolean })
  | (FieldBase & { kind: "agency" });

type Mode = "keep" | "set" | "clear";

interface FieldState {
  mode: Mode;
  /** The chosen value, kept even while the mode is "keep" so toggling back is free. */
  value: string;
  /** Switches carry a boolean rather than a string. */
  on: boolean;
}

const MODE_OPTIONS: SelectOption[] = [
  { value: "keep", label: "Leave unchanged" },
  { value: "set", label: "Set to…" },
];
const CLEAR_OPTION: SelectOption = { value: "clear", label: "Clear" };

function initialState(field: BulkField): FieldState {
  const value =
    field.kind === "select" ? (field.options[0]?.value ?? "") : field.kind === "color" ? "#2a78d6" : "";
  return { mode: "keep", value, on: false };
}

/**
 * The request body: every field the reader actually moved off "leave unchanged",
 * and nothing else.
 *
 * Exported because it is the part worth testing on its own — the drawer's whole
 * contract with the API is this function.
 */
export function changesFrom(fields: BulkField[], state: Record<string, FieldState>): Record<string, unknown> {
  const changes: Record<string, unknown> = {};
  for (const field of fields) {
    const current = state[field.key];
    if (!current || current.mode === "keep") continue;
    if (current.mode === "clear") {
      changes[field.key] = null;
      continue;
    }
    switch (field.kind) {
      case "switch":
        changes[field.key] = current.on;
        break;
      case "number": {
        // An unparseable number is treated as "not chosen" rather than sent as NaN,
        // which would come back a 422 about a field the reader thought was blank.
        const parsed = Number(current.value);
        if (current.value.trim() === "" || Number.isNaN(parsed)) continue;
        changes[field.key] = parsed;
        break;
      }
      case "agency":
        // The picker's "— none —" is the same request as Clear.
        changes[field.key] = current.value === "" ? null : current.value;
        break;
      case "text":
        // An empty box under "Set to…" means the empty string, which for these
        // nullable columns is what Clear is for — so it is sent as null.
        changes[field.key] = current.value.trim() === "" ? null : current.value;
        break;
      case "select":
        changes[field.key] = field.numeric ? Number(current.value) : current.value;
        break;
      default:
        changes[field.key] = current.value;
    }
  }
  return changes;
}

/* ---------------------------------------------------------------- the engine */

export function BulkEditDrawer({
  open,
  onOpenChange,
  ids,
  noun,
  title,
  fields,
  submit,
  pending = false,
  notice,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ids: number[];
  /** Plural: "talkgroups", "radios", "units", "people". */
  noun: string;
  title: string;
  fields: BulkField[];
  submit: (changes: Record<string, unknown>) => Promise<unknown>;
  pending?: boolean;
  /**
   * A soft word about the combination chosen, shown above the summary line.
   *
   * The fields are deliberately independent — that is what makes one engine enough
   * for four forms — so anything that has to look at two of them at once goes here,
   * as an observation rather than a rule. It never disables Apply: "this service tag
   * usually belongs to a different category" is sometimes exactly what the operator
   * means, and a form that refused it would be wrong more often than the operator is.
   */
  notice?: (changes: Record<string, unknown>) => ReactNode;
}) {
  const [state, setState] = useState<Record<string, FieldState>>({});

  // Each opening starts from "change nothing". Carrying the last edit's choices
  // forward would make the second bulk edit of a session the dangerous one.
  useEffect(() => {
    if (open) setState(Object.fromEntries(fields.map((field) => [field.key, initialState(field)])));
  }, [open, fields]);

  const changes = useMemo(() => changesFrom(fields, state), [fields, state]);
  const chosen = Object.keys(changes).length;

  const set = (key: string, patch: Partial<FieldState>) =>
    setState((current) => ({ ...current, [key]: { ...(current[key] as FieldState), ...patch } }));

  const apply = () => {
    submit(changes)
      .then(() => {
        toast(`Updated ${formatNumber(ids.length)} ${noun}`);
        onOpenChange(false);
      })
      .catch((error: unknown) => mutationErrorToast(error, `Could not update the ${noun}`));
  };

  return (
    <Drawer
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={`${formatNumber(ids.length)} ${noun} selected. Only the settings you switch off "leave unchanged" are written.`}
      footer={
        <>
          <Button tip={HELP["app.bulk.cancel"]} size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["app.bulk.apply"]}
            size="sm"
            variant="primary"
            disabled={chosen === 0 || pending || ids.length === 0}
            onClick={apply}
          >
            {pending ? "Applying…" : `Apply to ${formatNumber(ids.length)}`}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {fields.map((field) => {
          const current = state[field.key] ?? initialState(field);
          const options = field.clearable === false ? MODE_OPTIONS : [...MODE_OPTIONS, CLEAR_OPTION];
          return (
            <div key={field.key} className="min-w-0">
              <span className="mb-1 block text-xs font-medium text-ink-2">{field.label}</span>
              <div className="flex items-center gap-2">
                <Select
                  value={current.mode}
                  onValueChange={(mode) => set(field.key, { mode: mode as Mode })}
                  options={options}
                  tip={HELP["app.bulk.field"]}
                  ariaLabel={`What to do with ${field.label}`}
                  size="sm"
                  className="w-40 shrink-0"
                />
                {current.mode === "set" ? (
                  <FieldControl field={field} state={current} onChange={(patch) => set(field.key, patch)} />
                ) : (
                  <span className="text-xs text-muted">
                    {current.mode === "clear" ? "will be emptied on all of them" : "not written"}
                  </span>
                )}
              </div>
            </div>
          );
        })}

        {notice?.(changes) ?? null}

        <p className="mt-1 border-t border-line pt-3 text-[11px] text-ink-2">
          {chosen === 0
            ? "Nothing chosen yet — pick a setting above."
            : `${chosen} setting${chosen === 1 ? "" : "s"} will be written to every selected row.`}
        </p>
      </div>
    </Drawer>
  );
}

function FieldControl({
  field,
  state,
  onChange,
}: {
  field: BulkField;
  state: FieldState;
  onChange: (patch: Partial<FieldState>) => void;
}) {
  const label = `New ${field.label.toLowerCase()}`;
  switch (field.kind) {
    case "switch":
      return (
        <Switch
          checked={state.on}
          onCheckedChange={(on) => onChange({ on })}
          label={state.on ? "On" : "Off"}
          tip={HELP[field.help]}
        />
      );
    case "select":
      return (
        <Select
          value={state.value}
          onValueChange={(value) => onChange({ value })}
          options={field.options}
          tip={HELP[field.help]}
          ariaLabel={label}
          size="sm"
          className="w-full min-w-0"
        />
      );
    case "agency":
      return (
        <AgencySelect
          value={state.value === "" ? null : state.value}
          onValueChange={(code) => onChange({ value: code ?? "" })}
          tip={HELP[field.help]}
          ariaLabel={label}
          size="sm"
          className="h-8 w-full min-w-0 text-xs"
        />
      );
    case "color":
      return (
        <Tip label={HELP[field.help]}>
          <input
            type="color"
            aria-label={label}
            value={state.value || "#2a78d6"}
            onChange={(event) => onChange({ value: event.target.value })}
            className="h-8 w-12 cursor-pointer rounded border border-control-border bg-surface p-0.5"
          />
        </Tip>
      );
    case "number":
      return (
        <Tip label={HELP[field.help]}>
          <input
            type="number"
            aria-label={label}
            value={state.value}
            min={field.min}
            max={field.max}
            step={field.step}
            onChange={(event) => onChange({ value: event.target.value })}
            className={cn(inputClass, "h-8 w-28")}
          />
        </Tip>
      );
    default:
      return (
        <Tip label={HELP[field.help]}>
          <input
            type="text"
            aria-label={label}
            value={state.value}
            placeholder={field.placeholder}
            onChange={(event) => onChange({ value: event.target.value })}
            className={cn(inputClass, "h-8 w-full min-w-0")}
          />
        </Tip>
      );
  }
}

/* ------------------------------------------------------------- the four specs */

const TALKGROUP_FIELDS: BulkField[] = [
  {
    key: "category",
    label: "Category",
    help: "talkgroups.edit.category",
    kind: "select",
    options: TALKGROUP_CATEGORIES.map((category) => ({
      value: category,
      label: CATEGORY_LABELS[category] ?? category,
    })),
  },
  {
    key: "service_tag",
    label: "Service tag",
    help: "talkgroups.edit.serviceTag",
    kind: "select",
    options: RR_SERVICE_TAGS.map((tag) => ({ value: tag, label: tag })),
  },
  { key: "agency_code", label: "Agency", help: "talkgroups.edit.agency", kind: "agency" },
  { key: "color", label: "Color", help: "talkgroups.edit.color", kind: "color" },
  {
    key: "priority",
    label: "Priority",
    help: "talkgroups.edit.priority",
    kind: "number",
    min: TALKGROUP_PRIORITY_MIN,
    max: TALKGROUP_PRIORITY_MAX,
  },
  {
    key: "record",
    label: "Record",
    help: "talkgroups.edit.record",
    kind: "switch",
    clearable: false,
  },
  {
    key: "transcribe",
    label: "Transcribe",
    help: "talkgroups.edit.transcribe",
    kind: "switch",
    clearable: false,
  },
  { key: "is_hidden", label: "Hidden", help: "talkgroups.edit.hidden", kind: "switch", clearable: false },
  { key: "shared", label: "Shared", help: "talkgroups.edit.shared", kind: "switch", clearable: false },
  {
    key: "export_broadcastify",
    label: "Carry on the Broadcastify Feed",
    help: "talkgroups.table.exportBroadcastify",
    kind: "switch",
    clearable: false,
  },
  {
    // Broadcastify's other product, and its own decision. Setting it true across a
    // selection that includes a conventional system is not an error: the API holds those
    // rows at false and the rest are written, which is the same rule the gate enforces.
    key: "export_bcfy_calls",
    label: "Publish to Broadcastify Calls",
    help: "talkgroups.table.exportBcfyCalls",
    kind: "switch",
    clearable: false,
  },
  {
    key: "export_openmhz",
    label: "Publish to OpenMHz",
    help: "talkgroups.table.exportOpenmhz",
    kind: "switch",
    clearable: false,
  },
  {
    key: "retention_days",
    label: "Retention (days)",
    help: "talkgroups.edit.retention",
    kind: "number",
    min: 1,
  },
  {
    key: "transcribe_min_seconds",
    label: "Minimum length (s)",
    help: "talkgroups.edit.minSeconds",
    kind: "number",
    min: 0,
    step: 0.5,
  },
  {
    key: "group_name",
    label: "Group",
    help: "talkgroups.edit.group",
    kind: "text",
    placeholder: "Example County Fire",
  },
];

export function BulkEditTalkgroupsDrawer({
  open,
  onOpenChange,
  ids,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ids: number[];
}) {
  const bulk = useBulkUpdateTalkgroups();
  // The fields whose options are not known until the server is asked. They are applied
  // here rather than written into `TALKGROUP_FIELDS` because that list is a module
  // constant, and a spec that changed identity on every render would reset the drawer's
  // state — the effect that starts each opening from "change nothing" keys on `fields`.
  const profiles = useAudioProfiles(open);
  // The same vocabulary the filter row and the edit dialog read, so a category reads
  // "Law enforcement" here and in every other picker rather than as its wire key. Until
  // it arrives the module constants stand in, which is the full set of *values* — only
  // the prose labels are missing, so the drawer is never wrong, just briefly terse.
  const vocabulary = useVocabulary();
  const fields = useMemo(() => {
    const categories = vocabulary.data?.categories ?? [];
    const tags = vocabulary.data?.service_tags ?? [];
    const spec = TALKGROUP_FIELDS.map((field) => {
      if (field.key === "category" && categories.length > 0) {
        return { ...field, options: categories.map((term) => ({ value: term.key, label: term.label })) };
      }
      if (field.key === "service_tag" && tags.length > 0) {
        return { ...field, options: tags.map((term) => ({ value: term.tag, label: term.tag })) };
      }
      return field;
    });
    return [
      ...spec,
      {
        key: "audio_profile_id",
        label: "Audio profile",
        help: "talkgroups.edit.audioProfile" as HelpId,
        kind: "select" as const,
        numeric: true,
        options: (profiles.data ?? []).map((profile) => ({
          value: String(profile.id),
          label: profile.name,
        })),
      },
    ];
  }, [profiles.data, vocabulary.data]);

  // Setting both at once is the one combination worth a second look: RadioReference
  // maps each tag to a category, and choosing "Fire Dispatch" alongside "Law
  // enforcement" is usually a mis-click on one of the two pickers. Usually, not
  // always — a county that runs its fire dispatch on a law channel is a real thing
  // — so this says what it noticed and lets the operator go ahead.
  const tagCategory = useMemo(
    () => new Map((vocabulary.data?.service_tags ?? []).map((term) => [term.tag, term])),
    [vocabulary.data],
  );
  const notice = (changes: Record<string, unknown>): ReactNode => {
    const tag = changes.service_tag;
    const category = changes.category;
    if (typeof tag !== "string" || typeof category !== "string") return null;
    const term = tagCategory.get(tag);
    if (!term || term.category === category) return null;
    return (
      <p role="status" className="rounded-md bg-serious/10 px-2.5 py-2 text-[11px] text-serious-ink">
        RadioReference files <strong>{tag}</strong> under <strong>{term.category_label}</strong>, not the
        category chosen here. Both will be written as picked — this is only worth a look.
      </p>
    );
  };

  return (
    <BulkEditDrawer
      open={open}
      onOpenChange={onOpenChange}
      ids={ids}
      noun="talkgroups"
      title="Edit selected talkgroups"
      fields={fields}
      pending={bulk.isPending}
      notice={notice}
      submit={(changes) => bulk.mutateAsync({ ids, changes: changes as TalkgroupPatch })}
    />
  );
}

const RADIO_FIELDS: BulkField[] = [
  { key: "agency_code", label: "Agency", help: "radios.edit.agency", kind: "agency" },
  {
    key: "kind",
    label: "Kind",
    help: "radios.edit.kind",
    kind: "select",
    clearable: false,
    options: RADIO_KINDS.map((kind) => ({ value: kind, label: kind })),
  },
];

export function BulkEditRadiosDrawer({
  open,
  onOpenChange,
  ids,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ids: number[];
}) {
  const bulk = useBulkUpdateRadios();
  return (
    <BulkEditDrawer
      open={open}
      onOpenChange={onOpenChange}
      ids={ids}
      noun="radios"
      title="Edit selected radios"
      fields={RADIO_FIELDS}
      pending={bulk.isPending}
      submit={(changes) => bulk.mutateAsync({ ids, changes: changes as RadioPatch })}
    />
  );
}

const UNIT_FIELDS: BulkField[] = [
  { key: "agency_code", label: "Agency", help: "roster.units.edit.agency", kind: "agency" },
  {
    key: "kind",
    label: "Kind",
    help: "roster.units.edit.kind",
    kind: "select",
    clearable: false,
    options: UNIT_KINDS.map((kind) => ({ value: kind, label: kind })),
  },
];

export function BulkEditUnitsDrawer({
  open,
  onOpenChange,
  ids,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ids: number[];
}) {
  const bulk = useBulkUpdateUnits();
  return (
    <BulkEditDrawer
      open={open}
      onOpenChange={onOpenChange}
      ids={ids}
      noun="units"
      title="Edit selected units"
      fields={UNIT_FIELDS}
      pending={bulk.isPending}
      submit={(changes) => bulk.mutateAsync({ ids, changes: changes as UnitPatch })}
    />
  );
}

const PERSON_FIELDS: BulkField[] = [
  { key: "agency_code", label: "Agency", help: "roster.personnel.edit.agency", kind: "agency" },
  {
    key: "active",
    label: "On roster",
    help: "roster.personnel.edit.active",
    kind: "switch",
    clearable: false,
  },
  { key: "rank", label: "Rank", help: "roster.personnel.edit.rank", kind: "text", placeholder: "Captain" },
];

export function BulkEditPersonnelDrawer({
  open,
  onOpenChange,
  ids,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ids: number[];
}) {
  const bulk = useBulkUpdatePersonnel();
  return (
    <BulkEditDrawer
      open={open}
      onOpenChange={onOpenChange}
      ids={ids}
      noun="people"
      title="Edit selected people"
      fields={PERSON_FIELDS}
      pending={bulk.isPending}
      submit={(changes) => bulk.mutateAsync({ ids, changes: changes as PersonPatch })}
    />
  );
}
