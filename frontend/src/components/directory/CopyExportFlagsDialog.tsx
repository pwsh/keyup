import { useState } from "react";
import { useCopyTalkgroupExportFlags } from "../../api/hooks";
import {
  TALKGROUP_EXPORT_FLAG_LABELS,
  TALKGROUP_EXPORT_FLAGS,
  type TalkgroupExportFlag,
} from "../../api/types";
import { HELP } from "../../help/help";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { Select } from "../ui/Select";

/**
 * Copy one per-talkgroup export checkbox onto another.
 *
 * This exists for a single day in a deployment's life and then for the occasional repeat
 * of it: Broadcastify's one checkbox became two — the live **Feed** kept
 * `export_broadcastify`, the per-call **Calls** archive got `export_bcfy_calls` — and the
 * honest first move is "make Calls what Broadcastify already was". Doing that by hand
 * across several hundred channels is how a curated selection acquires mistakes.
 *
 * The scope is deliberately a choice rather than an inference. Copying over the whole
 * directory and copying over the rows that happen to be ticked are very different acts,
 * and the second one is the one somebody meant when they had a selection open.
 */
export function CopyExportFlagsDialog({
  open,
  onOpenChange,
  ids,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  ids: number[];
}) {
  const copy = useCopyTalkgroupExportFlags();
  const [source, setSource] = useState<TalkgroupExportFlag>("export_broadcastify");
  const [destination, setDestination] = useState<TalkgroupExportFlag>("export_bcfy_calls");
  const [scope, setScope] = useState<"selection" | "all">(ids.length > 0 ? "selection" : "all");

  const options = TALKGROUP_EXPORT_FLAGS.map((flag) => ({
    value: flag,
    label: TALKGROUP_EXPORT_FLAG_LABELS[flag],
  }));
  const same = source === destination;
  const usingSelection = scope === "selection" && ids.length > 0;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Copy export flags"
      description="Write one channel's export checkbox into another, across many channels at once."
      footer={
        <>
          <Button tip="Close without copying" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={
              same
                ? "Choose two different checkboxes"
                : "Copy the source checkbox onto the destination for every channel in scope"
            }
            size="sm"
            variant="primary"
            disabled={same || copy.isPending || (scope === "selection" && ids.length === 0)}
            onClick={() =>
              copy.mutate(
                usingSelection ? { source, destination, ids } : { source, destination, all: true },
                {
                  onSuccess: (result) => {
                    const held = result.skipped_not_trunked
                      ? `, ${result.skipped_not_trunked} left off (system not trunked)`
                      : "";
                    toast(
                      `Copied ${TALKGROUP_EXPORT_FLAG_LABELS[source]} to ${TALKGROUP_EXPORT_FLAG_LABELS[destination]} — ${result.updated} changed${held}`,
                      { tone: "success" },
                    );
                    onOpenChange(false);
                  },
                  onError: (error) => mutationErrorToast(error, "Could not copy the export flags"),
                },
              )
            }
          >
            Copy flags
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Copy from" help="talkgroups.copyFlags">
          {() => (
            <Select
              value={source}
              onValueChange={(next) => setSource(next as TalkgroupExportFlag)}
              options={options}
              ariaLabel="Copy from"
              tip={HELP["talkgroups.copyFlags"]}
              className="w-full"
            />
          )}
        </Field>
        <Field label="Copy to" help="talkgroups.copyFlags">
          {() => (
            <Select
              value={destination}
              onValueChange={(next) => setDestination(next as TalkgroupExportFlag)}
              options={options}
              ariaLabel="Copy to"
              tip={HELP["talkgroups.copyFlags"]}
              className="w-full"
            />
          )}
        </Field>
        <Field label="Apply to" help="talkgroups.copyFlags" className="sm:col-span-2">
          {() => (
            <Select
              value={scope}
              onValueChange={(next) => setScope(next as "selection" | "all")}
              options={[
                {
                  value: "selection",
                  label:
                    ids.length > 0
                      ? `the ${ids.length} selected channel${ids.length === 1 ? "" : "s"}`
                      : "the selected channels (none selected)",
                },
                { value: "all", label: "every channel in the directory" },
              ]}
              ariaLabel="Apply to"
              tip={HELP["talkgroups.copyFlags"]}
              className="w-full"
            />
          )}
        </Field>
      </div>

      <p className="mt-3 text-[11px] leading-relaxed text-muted">
        {same
          ? "Choose two different checkboxes: copying one onto itself would do nothing."
          : `Every channel in scope will have its ${TALKGROUP_EXPORT_FLAG_LABELS[destination]} checkbox overwritten with its ${TALKGROUP_EXPORT_FLAG_LABELS[source]} one.`}{" "}
        {destination === "export_bcfy_calls"
          ? "Channels on a system that is not trunked are left off — Broadcastify Calls publishes trunked systems only — and counted in the result."
          : null}{" "}
        The checkboxes are read only while that service's talkgroup selection is
        <em> allow</em> or <em> deny</em>, so this changes nothing on its own.
      </p>
    </Dialog>
  );
}
