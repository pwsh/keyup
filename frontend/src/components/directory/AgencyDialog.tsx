/**
 * Add or edit one agency.
 *
 * One dialog for both, and for both callers: the Admin → Agencies table, and
 * the "New agency…" entry in the picker inside the talkgroup dialog. That second
 * caller is the reason it exists — an operator mapping a channel to a railroad
 * the county's dispatch system has never heard of should not have to abandon the
 * edit, go to Admin, come back and find the channel again.
 */

import { useEffect, useState } from "react";
import { useCreateAgency, useUpdateAgency } from "../../api/hooks";
import { type AdminAgency, AGENCY_CODE_RE, AGENCY_KINDS, type AgencyKind } from "../../api/types";
import { HELP } from "../../help/help";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { Switch } from "../ui/Switch";

interface Draft {
  code: string;
  name: string;
  short_name: string;
  kind: AgencyKind;
  active: boolean;
}

const BLANK: Draft = { code: "", name: "", short_name: "", kind: "police", active: true };

/** The message under the code box, or null when the code is fine. */
export function codeProblem(code: string, taken: string[]): string | null {
  const value = code.trim().toUpperCase();
  if (value === "") return "A code is required — it is what talkgroups and radios point at.";
  if (!AGENCY_CODE_RE.test(value)) {
    return "2 to 16 characters: capitals, digits, hyphen and underscore only.";
  }
  if (taken.includes(value)) return `${value} is already an agency.`;
  return null;
}

export function AgencyDialog({
  open,
  onOpenChange,
  agency = null,
  existingCodes = [],
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The row being edited, or null to add a new one. */
  agency?: AdminAgency | null;
  /** Codes already in use, so a duplicate is caught before the round trip. */
  existingCodes?: string[];
  /** Called with the new code after a successful add — the picker selects it. */
  onCreated?: (code: string) => void;
}) {
  const create = useCreateAgency();
  const update = useUpdateAgency();
  const editing = agency !== null;
  const [draft, setDraft] = useState<Draft>(BLANK);

  useEffect(() => {
    if (!open) return;
    setDraft(
      agency
        ? {
            code: agency.code,
            name: agency.name,
            short_name: agency.short_name,
            kind: agency.kind as AgencyKind,
            active: agency.active,
          }
        : BLANK,
    );
  }, [open, agency]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((previous) => ({ ...previous, [key]: value }));

  const problem = editing ? null : codeProblem(draft.code, existingCodes);
  const nameMissing = draft.name.trim() === "";
  const pending = create.isPending || update.isPending;

  const save = () => {
    if (editing) {
      update.mutate(
        {
          code: agency.code,
          patch: {
            name: draft.name.trim(),
            short_name: draft.short_name.trim() || null,
            kind: draft.kind,
            active: draft.active,
          },
        },
        {
          onSuccess: () => {
            toast("Agency saved", { tone: "success" });
            onOpenChange(false);
          },
          onError: (error) => mutationErrorToast(error, "Could not save the agency"),
        },
      );
      return;
    }
    const code = draft.code.trim().toUpperCase();
    create.mutate(
      {
        code,
        name: draft.name.trim(),
        short_name: draft.short_name.trim() || null,
        kind: draft.kind,
        active: draft.active,
      },
      {
        onSuccess: () => {
          toast(`Agency ${code} added`, { tone: "success" });
          onCreated?.(code);
          onOpenChange(false);
        },
        onError: (error) => mutationErrorToast(error, "Could not add the agency"),
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={editing ? `Edit ${agency.code}` : "New agency"}
      description={
        editing
          ? "The code cannot change — talkgroups, radios, units and people point at it."
          : "For an agency the county's dispatch system has no code for. It is marked local, and re-seeding the shared list never touches it."
      }
      footer={
        <>
          <Button tip="Close without saving" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={editing ? HELP["admin.agencies.save"] : HELP["admin.agencies.add.save"]}
            size="sm"
            variant="primary"
            disabled={pending || problem !== null || nameMissing}
            onClick={save}
          >
            {editing ? "Save" : "Add agency"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="Code"
          help="admin.agencies.code"
          hint={
            editing
              ? "Fixed: everything mapped to this agency points at it."
              : (problem ?? "Capitals, digits, hyphen and underscore, 2–16 characters.")
          }
        >
          {(id) => (
            <TextInput
              id={id}
              value={draft.code}
              disabled={editing}
              placeholder="NORTH_SO"
              // Upper-cased as it is typed, because that is what is stored and
              // seeing it change on save reads as the form correcting you.
              onChange={(event) => set("code", event.target.value.toUpperCase())}
            />
          )}
        </Field>

        <Field label="Kind" help="admin.agencies.kind">
          {() => (
            <Select
              value={draft.kind}
              onValueChange={(value) => set("kind", value as AgencyKind)}
              options={AGENCY_KINDS.map((kind) => ({ value: kind, label: kind }))}
              tip={HELP["admin.agencies.kind"]}
              ariaLabel="Kind"
              className="w-full"
            />
          )}
        </Field>

        <Field label="Name" help="admin.agencies.name" className="sm:col-span-2">
          {(id) => (
            <TextInput
              id={id}
              value={draft.name}
              placeholder="North County Sheriff"
              onChange={(event) => set("name", event.target.value)}
            />
          )}
        </Field>

        <Field
          label="Short name"
          help="admin.agencies.shortName"
          hint="Left empty, the code is used."
          className="sm:col-span-2"
        >
          {(id) => (
            <TextInput
              id={id}
              value={draft.short_name}
              placeholder={draft.code || "LCSO"}
              onChange={(event) => set("short_name", event.target.value)}
            />
          )}
        </Field>

        <div className="sm:col-span-2">
          <Switch
            checked={draft.active}
            onCheckedChange={(next) => set("active", next)}
            label="Active"
            tip={HELP["admin.agencies.active"]}
          />
        </div>
      </div>
    </Dialog>
  );
}
