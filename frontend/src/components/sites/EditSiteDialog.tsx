import { useEffect, useState } from "react";
import { errorDetail } from "../../api/client";
import { useUpdateSite } from "../../api/hooks";
import type { SiteInfo } from "../../api/types";
import { HELP } from "../../help/help";
import { siteLabel } from "../../lib/format";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, TextArea, TextInput } from "../ui/Field";

/**
 * The three fields on a site that are ours to choose.
 *
 * `site_number`, `nac`, `rfss` and `lra` are missing on purpose: they are what
 * the control channel announced about itself, filled from recorder observations,
 * and editing them by hand would be editing the archive's record of what the air
 * said. `description` is missing for the same reason — the RadioReference sync
 * writes it — which is why `notes` exists beside it.
 */
export function EditSiteDialog({
  site,
  open,
  onOpenChange,
}: {
  site: SiteInfo | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const update = useUpdateSite();
  const [draft, setDraft] = useState<SiteInfo | null>(site);

  useEffect(() => {
    setDraft(site);
  }, [site]);

  if (!draft) return null;

  const set = <K extends keyof SiteInfo>(key: K, value: SiteInfo[K]) =>
    setDraft((previous) => (previous ? { ...previous, [key]: value } : previous));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Edit ${siteLabel(draft)}`}
      description="The name and the RadioReference mapping are yours to set; what the control channel announces about itself is filled from observations and is not editable here."
      footer={
        <>
          <Button tip={HELP["sites.edit.cancel"]} size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["sites.edit.save"]}
            size="sm"
            variant="primary"
            disabled={update.isPending || draft.name.trim() === ""}
            onClick={() =>
              update.mutate(
                {
                  id: draft.id,
                  patch: {
                    name: draft.name.trim(),
                    notes: draft.notes,
                    rr_site_id: draft.rr_site_id,
                  },
                },
                {
                  onSuccess: () => {
                    toast("Site saved", { tone: "success" });
                    onOpenChange(false);
                  },
                  onError: (error) =>
                    toast("Could not save the site", {
                      tone: "error",
                      body: errorDetail(error),
                    }),
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
        <Field label="System" help="sites.edit.system">
          {(id) => (
            <TextInput id={id} value={draft.system_name ?? "—"} readOnly disabled className="opacity-70" />
          )}
        </Field>
        <Field label="Name" help="sites.edit.name">
          {(id) => (
            <TextInput
              id={id}
              value={draft.name}
              onChange={(event) => set("name", event.target.value)}
              placeholder="Central"
            />
          )}
        </Field>
        <Field label="RR site id" help="sites.edit.rrSiteId">
          {(id) => (
            <TextInput
              id={id}
              type="number"
              min={1}
              value={draft.rr_site_id ?? ""}
              onChange={(event) =>
                set("rr_site_id", event.target.value === "" ? null : Number(event.target.value))
              }
              placeholder="not mapped"
            />
          )}
        </Field>
        <Field label="Notes" help="sites.edit.notes" className="sm:col-span-2">
          {(id) => (
            <TextArea
              id={id}
              rows={3}
              value={draft.notes ?? ""}
              onChange={(event) => set("notes", event.target.value || null)}
              placeholder="Anything you want to remember about this site."
            />
          )}
        </Field>
      </div>
    </Dialog>
  );
}
