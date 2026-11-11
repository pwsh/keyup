import { useEffect, useState } from "react";
import { useCreateUnit, useUpdateUnit } from "../../api/hooks";
import { UNIT_KINDS, type Unit, type UnitKind } from "../../api/types";
import { HELP } from "../../help/help";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, TextArea, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { AgencySelect } from "./AgencySelect";

/** A blank unit, for the "New unit" path — the same form does both. */
const EMPTY: Unit = {
  id: 0,
  callsign: "",
  name: null,
  agency_code: null,
  kind: "other",
  notes: null,
  created_at: null,
  updated_at: null,
  radio_count: 0,
};

/**
 * Create or edit one unit.
 *
 * Creating and editing share a form because the fields are identical and the
 * distinction is one the operator does not care about: `unit === null` means new.
 */
export function EditUnitDialog({
  unit,
  open,
  onOpenChange,
}: {
  unit: Unit | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const create = useCreateUnit();
  const update = useUpdateUnit();
  const [draft, setDraft] = useState<Unit>(unit ?? EMPTY);
  useEffect(() => {
    setDraft(unit ?? EMPTY);
  }, [unit]);

  const isNew = unit === null;
  const pending = create.isPending || update.isPending;
  const set = <K extends keyof Unit>(key: K, value: Unit[K]) =>
    setDraft((previous) => ({ ...previous, [key]: value }));

  const save = () => {
    const fields = {
      callsign: draft.callsign.trim(),
      name: draft.name,
      agency_code: draft.agency_code,
      kind: draft.kind,
      notes: draft.notes,
    };
    const handlers = {
      onSuccess: () => {
        toast(isNew ? "Unit added" : "Unit saved", { tone: "success" });
        onOpenChange(false);
      },
      // A callsign is a unique key, so this is where a 409 lands: "callsign 'E1'
      // already exists" is the whole answer, and the old fixed string hid it.
      onError: (error: unknown) => mutationErrorToast(error, "Could not save the unit"),
    };
    if (isNew) create.mutate(fields, handlers);
    else update.mutate({ id: draft.id, patch: fields }, handlers);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={isNew ? "New unit" : `Edit ${unit?.callsign ?? "unit"}`}
      description="A unit is what a callsign names — an engine, an ambulance, a station, a crew. Radios are assigned to it."
      footer={
        <>
          <Button tip="Close without saving" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["roster.units.edit"]}
            size="sm"
            variant="primary"
            disabled={pending || draft.callsign.trim() === ""}
            onClick={save}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Callsign" help="roster.units.edit.callsign">
          {(id) => (
            <TextInput
              id={id}
              value={draft.callsign}
              placeholder="E1"
              onChange={(event) => set("callsign", event.target.value)}
            />
          )}
        </Field>
        <Field label="Name" help="roster.units.edit.name">
          {(id) => (
            <TextInput
              id={id}
              value={draft.name ?? ""}
              placeholder="Engine 1"
              onChange={(event) => set("name", event.target.value || null)}
            />
          )}
        </Field>
        <Field label="Kind" help="roster.units.edit.kind">
          {() => (
            <Select
              value={draft.kind}
              onValueChange={(value) => set("kind", value as UnitKind)}
              options={UNIT_KINDS.map((kind) => ({ value: kind, label: kind }))}
              tip={HELP["roster.units.edit.kind"]}
              ariaLabel="Unit kind"
              className="w-full"
            />
          )}
        </Field>
        <Field label="Agency" help="roster.units.edit.agency">
          {() => (
            <AgencySelect
              value={draft.agency_code}
              onValueChange={(code) => set("agency_code", code)}
              tip={HELP["roster.units.edit.agency"]}
              ariaLabel="Agency"
            />
          )}
        </Field>
        <Field label="Notes" help="roster.units.edit.notes" className="sm:col-span-2">
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
