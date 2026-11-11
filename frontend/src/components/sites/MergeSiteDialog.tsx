import { useState } from "react";
import { errorDetail } from "../../api/client";
import { useMergeSite } from "../../api/hooks";
import type { SiteInfo } from "../../api/types";
import { HELP } from "../../help/help";
import { formatNumber, siteLabel } from "../../lib/format";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { Select } from "../ui/Select";

const PICK_NONE = "none";

/**
 * Fold two rows for one tower into one — the fix for a rename or a respelling
 * that landed as a second site.
 *
 * The picker offers only sites on the **same system**, because that is all the
 * API will do: a site's calls carry that system's talkgroups and radios, and
 * moving them under another system would leave every one of them pointing at a
 * channel the call no longer belongs to. That operation exists — it is
 * `keyup refile-calls`, which splits the talkgroups and radios onto twins first —
 * and it is not something a two-click merge should do by accident.
 */
export function MergeSiteDialog({
  site,
  sites,
  open,
  onOpenChange,
}: {
  site: SiteInfo | null;
  sites: SiteInfo[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const merge = useMergeSite();
  // Reset-on-prop-change during render rather than in an effect: opening the dialog on a
  // different site must never show the target picked for the previous one, and doing it
  // here means there is no frame in which it does.
  const [pick, setPick] = useState<{ site: number | null; target: string }>({
    site: site?.id ?? null,
    target: PICK_NONE,
  });
  if (pick.site !== (site?.id ?? null)) {
    setPick({ site: site?.id ?? null, target: PICK_NONE });
  }
  const target = pick.target;
  const setTarget = (value: string) => setPick({ site: site?.id ?? null, target: value });

  if (!site) return null;

  const candidates = sites.filter(
    (candidate) => candidate.system_id === site.system_id && candidate.id !== site.id,
  );
  const chosen = candidates.find((candidate) => String(candidate.id) === target) ?? null;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Merge ${siteLabel(site)}`}
      description="Everything filed under this site moves to the one you pick, blank fields on it are filled in from this one, and this site is then deleted."
      footer={
        <>
          <Button tip={HELP["sites.merge.cancel"]} size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["sites.merge.confirm"]}
            size="sm"
            variant="danger"
            disabled={chosen === null || merge.isPending}
            onClick={() => {
              if (!chosen) return;
              merge.mutate(
                { id: site.id, into: chosen.id },
                {
                  onSuccess: (result) => {
                    toast(`Merged into ${chosen.name}`, {
                      tone: "success",
                      body: `${formatNumber(result.moved?.calls ?? 0)} calls moved.`,
                    });
                    onOpenChange(false);
                  },
                  onError: (error) =>
                    toast("Could not merge the site", {
                      tone: "error",
                      body: errorDetail(error),
                    }),
                },
              );
            }}
          >
            Merge and delete
          </Button>
        </>
      }
    >
      {candidates.length === 0 ? (
        <p className="text-sm text-ink-2">
          {siteLabel(site)} is the only site on its system, so there is nothing to merge it into.
        </p>
      ) : (
        <div className="space-y-3">
          <Field label="Merge into" help="sites.merge.target">
            {() => (
              <Select
                value={target}
                onValueChange={setTarget}
                ariaLabel="Merge into"
                tip={HELP["sites.merge.target"]}
                options={[
                  { value: PICK_NONE, label: "— pick a site —" },
                  ...candidates.map((candidate) => ({
                    value: String(candidate.id),
                    label: `${candidate.name} (${formatNumber(candidate.call_count)} calls)`,
                  })),
                ]}
              />
            )}
          </Field>
          <p className="text-xs text-ink-2">
            {chosen
              ? `${formatNumber(site.call_count)} calls, and every frequency, neighbor and observation of ${site.name}, will move to ${chosen.name}. This cannot be undone.`
              : "Only sites on the same system are offered: moving calls between systems is a re-filing, not a merge."}
          </p>
        </div>
      )}
    </Dialog>
  );
}
