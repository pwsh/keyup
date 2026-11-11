import { useEffect, useState } from "react";
import { useUpdateRadio } from "../../api/hooks";
import { RADIO_KINDS, type Radio, type RadioKind } from "../../api/types";
import { HELP } from "../../help/help";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, TextArea, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { AgencySelect } from "./AgencySelect";

/** Inline edit for one radio's unit number, alias, kind and agency. */
export function EditRadioDialog({
  radio,
  open,
  onOpenChange,
}: {
  radio: Radio | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const update = useUpdateRadio();
  const [draft, setDraft] = useState<Radio | null>(radio);
  useEffect(() => {
    setDraft(radio);
  }, [radio]);

  if (!draft) return null;
  const set = <K extends keyof Radio>(key: K, value: Radio[K]) =>
    setDraft((previous) => (previous ? { ...previous, [key]: value } : previous));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit radio ${radio?.radio_id ?? ""}`}
      description="The unit number is what links this radio to apparatus in other systems, so it is worth filling in."
      footer={
        <>
          <Button tip="Close without saving" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip="Save these changes"
            size="sm"
            variant="primary"
            disabled={update.isPending}
            onClick={() =>
              update.mutate(
                {
                  id: draft.id,
                  patch: {
                    alias: draft.alias,
                    unit_id: draft.unit_id,
                    agency_code: draft.agency_code,
                    kind: draft.kind,
                    notes: draft.notes,
                  },
                },
                {
                  onSuccess: () => {
                    toast("Radio saved", { tone: "success" });
                    onOpenChange(false);
                  },
                  onError: (error) => mutationErrorToast(error, "Could not save the radio"),
                },
              )
            }
          >
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Unit number" help="radios.edit.unit">
          {(id) => (
            <TextInput
              id={id}
              value={draft.unit_id ?? ""}
              placeholder="E1"
              onChange={(event) => set("unit_id", event.target.value || null)}
            />
          )}
        </Field>
        <Field label="Alias" help="radios.edit.alias">
          {(id) => (
            <TextInput
              id={id}
              value={draft.alias ?? ""}
              onChange={(event) => set("alias", event.target.value || null)}
            />
          )}
        </Field>
        <Field label="Kind" help="radios.edit.kind">
          {() => (
            <Select
              value={draft.kind}
              onValueChange={(value) => set("kind", value as RadioKind)}
              options={RADIO_KINDS.map((kind) => ({ value: kind, label: kind }))}
              tip={HELP["radios.edit.kind"]}
              ariaLabel="Radio kind"
              className="w-full"
            />
          )}
        </Field>
        <Field label="Agency" help="radios.edit.agency">
          {() => (
            <AgencySelect
              value={draft.agency_code}
              onValueChange={(code) => set("agency_code", code)}
              tip={HELP["radios.edit.agency"]}
              ariaLabel="Agency"
            />
          )}
        </Field>
        <Field label="Notes" help="radios.edit.notes" className="sm:col-span-2">
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
