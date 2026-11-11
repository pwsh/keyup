import { useEffect, useState } from "react";
import { useCreatePerson, useUpdatePerson } from "../../api/hooks";
import type { Person } from "../../api/types";
import { HELP } from "../../help/help";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, TextArea, TextInput } from "../ui/Field";
import { Switch } from "../ui/Switch";
import { AgencySelect } from "./AgencySelect";

const EMPTY: Person = {
  id: 0,
  name: "",
  rank: null,
  badge: null,
  agency_code: null,
  active: true,
  notes: null,
  created_at: null,
  updated_at: null,
  radio_count: 0,
};

/**
 * Create or edit one person.
 *
 * There is no delete: the archive is historical, so somebody who has left the
 * roster is turned inactive instead — their name is still the right answer about
 * last year's traffic.
 */
export function EditPersonnelDialog({
  person,
  open,
  onOpenChange,
}: {
  person: Person | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const create = useCreatePerson();
  const update = useUpdatePerson();
  const [draft, setDraft] = useState<Person>(person ?? EMPTY);
  useEffect(() => {
    setDraft(person ?? EMPTY);
  }, [person]);

  const isNew = person === null;
  const pending = create.isPending || update.isPending;
  const set = <K extends keyof Person>(key: K, value: Person[K]) =>
    setDraft((previous) => ({ ...previous, [key]: value }));

  const save = () => {
    const fields = {
      name: draft.name.trim(),
      rank: draft.rank,
      badge: draft.badge,
      agency_code: draft.agency_code,
      active: draft.active,
      notes: draft.notes,
    };
    const handlers = {
      onSuccess: () => {
        toast(isNew ? "Person added" : "Person saved", { tone: "success" });
        onOpenChange(false);
      },
      onError: (error: unknown) => mutationErrorToast(error, "Could not save the person"),
    };
    if (isNew) create.mutate(fields, handlers);
    else update.mutate({ id: draft.id, patch: fields }, handlers);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={isNew ? "New person" : `Edit ${person?.name ?? "person"}`}
      description="Somebody who carries a radio. Only what the archive needs to credit a transmission — nothing else about them belongs here."
      footer={
        <>
          <Button tip="Close without saving" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["roster.personnel.edit"]}
            size="sm"
            variant="primary"
            disabled={pending || draft.name.trim() === ""}
            onClick={save}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name" help="roster.personnel.edit.name">
          {(id) => (
            <TextInput
              id={id}
              value={draft.name}
              placeholder="J. Smith"
              onChange={(event) => set("name", event.target.value)}
            />
          )}
        </Field>
        <Field label="Rank" help="roster.personnel.edit.rank">
          {(id) => (
            <TextInput
              id={id}
              value={draft.rank ?? ""}
              placeholder="Captain"
              onChange={(event) => set("rank", event.target.value || null)}
            />
          )}
        </Field>
        <Field label="Badge" help="roster.personnel.edit.badge">
          {(id) => (
            <TextInput
              id={id}
              value={draft.badge ?? ""}
              onChange={(event) => set("badge", event.target.value || null)}
            />
          )}
        </Field>
        <Field label="Agency" help="roster.personnel.edit.agency">
          {() => (
            <AgencySelect
              value={draft.agency_code}
              onValueChange={(code) => set("agency_code", code)}
              tip={HELP["roster.personnel.edit.agency"]}
              ariaLabel="Agency"
            />
          )}
        </Field>
        <Field label="On the roster" help="roster.personnel.edit.active">
          {() => (
            <Switch
              checked={draft.active}
              onCheckedChange={(value) => set("active", value)}
              label="On the roster"
              tip={HELP["roster.personnel.edit.active"]}
            />
          )}
        </Field>
        <Field label="Notes" help="roster.personnel.edit.notes" className="sm:col-span-2">
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
