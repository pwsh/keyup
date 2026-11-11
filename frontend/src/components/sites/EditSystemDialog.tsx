import { useEffect, useState } from "react";
import { errorDetail } from "../../api/client";
import { useSetSystemRr, useUpdateSystem } from "../../api/hooks";
import type { SystemInfo } from "../../api/types";
import { HELP } from "../../help/help";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, TextArea, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";

/**
 * The two fields on a system that are ours to choose, and the RadioReference mapping.
 *
 * `wacn`, `system_id`, `nac` and `protocol` are missing on purpose, for the same reason a
 * site's `nac`/`rfss`/`lra` are: they are what the control channel announced about itself,
 * filled from what the recorder heard, and editing them by hand would be editing the
 * archive's record of what the air said.
 *
 * `trunked` is the exception to that rule and belongs with the name: it is a *judgement*
 * about what the protocol string means rather than something the control channel
 * announced, and Broadcastify Calls will not publish a system that is not trunked. Left on
 * "from the protocol" it is derived (`is_trunked`); set either way it is the last word.
 *
 * The RadioReference id is saved through its own endpoint rather than the patch, because
 * it is an admin escape hatch rather than an edit — it overrides the `RR_SYSTEMS`
 * environment mapping and decides what the nightly sync pulls — and until now nothing in
 * the app called it at all: the channel audit told operators to map a system and offered
 * no way to do it.
 */
export function EditSystemDialog({
  system,
  open,
  onOpenChange,
  canMapRr,
}: {
  system: SystemInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Mapping is admin-only; a writer may still rename. */
  canMapRr: boolean;
}) {
  const update = useUpdateSystem();
  const setRr = useSetSystemRr();
  const [draft, setDraft] = useState<SystemInfo | null>(system);

  useEffect(() => {
    setDraft(system);
  }, [system]);

  if (!draft || !system) return null;

  const set = <K extends keyof SystemInfo>(key: K, value: SystemInfo[K]) =>
    setDraft((previous) => (previous ? { ...previous, [key]: value } : previous));

  const rrChanged = draft.rr_system_id !== system.rr_system_id;

  const save = async () => {
    try {
      await update.mutateAsync({
        id: draft.id,
        patch: {
          name: draft.name.trim(),
          description: draft.description,
          trunked: draft.trunked,
        },
      });
      // Second, and only when it moved: the mapping is a different endpoint with a
      // different permission, and a writer who cannot map must still be able to rename.
      if (rrChanged) {
        await setRr.mutateAsync({ id: draft.id, rrId: draft.rr_system_id });
      }
      toast("System saved", { tone: "success" });
      onOpenChange(false);
    } catch (error) {
      toast("Could not save the system", {
        tone: "error",
        body: errorDetail(error),
      });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit ${system.name}`}
      description="The name, the description and the RadioReference mapping are yours to set; what the control channel announces about itself is filled from what the recorder heard."
      footer={
        <>
          <Button tip={HELP["systems.edit.cancel"]} size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["systems.edit.save"]}
            size="sm"
            variant="primary"
            disabled={update.isPending || setRr.isPending || draft.name.trim() === ""}
            onClick={() => void save()}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name" help="systems.edit.name">
          {(id) => (
            <TextInput
              id={id}
              value={draft.name}
              onChange={(event) => set("name", event.target.value)}
              placeholder="Example Network"
            />
          )}
        </Field>
        <Field
          label="RR system id"
          help="systems.edit.rrSystemId"
          hint={canMapRr ? undefined : "Mapping a system to RadioReference needs an admin account."}
        >
          {(id) => (
            <TextInput
              id={id}
              type="number"
              min={1}
              disabled={!canMapRr}
              value={draft.rr_system_id ?? ""}
              onChange={(event) =>
                set("rr_system_id", event.target.value === "" ? null : Number(event.target.value))
              }
              placeholder="not mapped"
            />
          )}
        </Field>
        <Field label="Trunked system" help="systems.edit.trunked">
          {() => (
            <Select
              value={draft.trunked === null ? "auto" : draft.trunked ? "yes" : "no"}
              onValueChange={(next) => set("trunked", next === "auto" ? null : next === "yes")}
              options={[
                {
                  value: "auto",
                  label: `from the protocol (${draft.is_trunked ? "trunked" : "not trunked"})`,
                },
                { value: "yes", label: "yes — trunked" },
                { value: "no", label: "no — conventional" },
              ]}
              ariaLabel="Trunked system"
              tip={HELP["systems.edit.trunked"]}
              className="w-full"
            />
          )}
        </Field>
        <Field label="Description" help="systems.edit.description" className="sm:col-span-2">
          {(id) => (
            <TextArea
              id={id}
              rows={3}
              value={draft.description ?? ""}
              onChange={(event) => set("description", event.target.value || null)}
              placeholder="Who runs this system, and what it covers."
            />
          )}
        </Field>
      </div>
      <p className="mt-3 text-[11px] text-ink-2">
        {system.wacn === null && system.system_id === null
          ? "Nothing has announced a WACN or system id for this system yet."
          : `WACN ${system.wacn ?? "—"} · system ${system.system_id ?? "—"} · ${system.protocol ?? "protocol unknown"}`}
      </p>
    </Dialog>
  );
}
