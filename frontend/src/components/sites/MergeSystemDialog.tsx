import { useState } from "react";
import { errorDetail } from "../../api/client";
import { useMergeSystem } from "../../api/hooks";
import type { SystemInfo } from "../../api/types";
import { HELP } from "../../help/help";
import { formatNumber } from "../../lib/format";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { Select } from "../ui/Select";

const PICK_NONE = "none";

/**
 * Fold a whole system into another — the fix for a `systemLabel` typo, or for a recorder
 * that sent none at all and left the archive with a second system holding duplicate sites,
 * talkgroups and radios.
 *
 * Unlike the site merge beside it this one is *allowed* to move calls between systems,
 * because it is not moving them: it splits every talkgroup and radio onto the target's
 * twin first, so a call arrives already pointing at the right channel. That is the whole
 * difference between a merge and a re-filing, and it is why this dialog offers every other
 * system while the site one offers only siblings.
 */
export function MergeSystemDialog({
  system,
  systems,
  open,
  onOpenChange,
}: {
  system: SystemInfo | null;
  systems: SystemInfo[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const merge = useMergeSystem();
  // Reset on prop change during render rather than in an effect, exactly as the site merge
  // does: opening on a different system must never show the target picked for the last one.
  const [pick, setPick] = useState<{ system: number | null; target: string }>({
    system: system?.id ?? null,
    target: PICK_NONE,
  });
  if (pick.system !== (system?.id ?? null)) {
    setPick({ system: system?.id ?? null, target: PICK_NONE });
  }
  const target = pick.target;
  const setTarget = (value: string) => setPick({ system: system?.id ?? null, target: value });

  if (!system) return null;

  const candidates = systems.filter((candidate) => candidate.id !== system.id);
  const chosen = candidates.find((candidate) => String(candidate.id) === target) ?? null;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Merge ${system.name}`}
      description="Every site, talkgroup, radio and call of this system moves to the one you pick — folded into its twin where it has one, moved across as it is where it does not — and this system is then deleted."
      footer={
        <>
          <Button tip={HELP["systems.merge.cancel"]} size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["systems.merge.confirm"]}
            size="sm"
            variant="danger"
            disabled={chosen === null || merge.isPending}
            onClick={() => {
              if (!chosen) return;
              merge.mutate(
                { id: system.id, into: chosen.id },
                {
                  onSuccess: (result) => {
                    const moved = Object.values(result.rows_repointed ?? {}).reduce(
                      (total, rows) => total + rows,
                      0,
                    );
                    toast(`Merged into ${chosen.name}`, {
                      tone: "success",
                      body: `${formatNumber(moved)} rows repointed; no call was deleted.`,
                    });
                    onOpenChange(false);
                  },
                  onError: (error) =>
                    toast("Could not merge the system", {
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
          {system.name} is the only system in the archive, so there is nothing to merge it into.
        </p>
      ) : (
        <div className="space-y-3">
          <Field label="Merge into" help="systems.merge.target">
            {() => (
              <Select
                value={target}
                onValueChange={setTarget}
                ariaLabel="Merge into"
                tip={HELP["systems.merge.target"]}
                options={[
                  { value: PICK_NONE, label: "— pick a system —" },
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
              ? `${formatNumber(system.call_count)} calls and ${formatNumber(system.site_count)} sites move to ${chosen.name}. A site, talkgroup or radio the target already has absorbs its twin — counters summed, blanks filled — and no call is ever deleted. This cannot be undone.`
              : "A large merge runs in committed batches, so recording carries on while it works."}
          </p>
        </div>
      )}
    </Dialog>
  );
}
