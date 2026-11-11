/**
 * Pick an agency code from the agencies that exist.
 *
 * Every directory dialog used to take the code as free text, and the API refuses
 * a code it does not know with a 422 — so a typo (`XX000O100`, letter O for zero)
 * was only discovered on Save, by which point the reader had no list to check
 * against. `useAgencies` has been in `hooks.ts` since the agencies endpoint
 * landed and nothing called it; this is that call.
 *
 * The label is the agency's name with its code beside it, because the code is
 * what the archive stores and what an outside system's export will be matched on, but the
 * name is what a human recognizes.
 */

import { useState } from "react";
import { useAgencies, useCurrentUser } from "../../api/hooks";
import { Select } from "../ui/Select";
import { AgencyDialog } from "./AgencyDialog";

/**
 * "No agency" as a value a reader can pick *back*. Not the empty string, which a
 * Radix select reserves for "nothing is selected" — an item using it cannot be
 * chosen. No agency code is spelled this way, so the sentinel cannot collide.
 */
export const NO_AGENCY = "none";

/**
 * The entry that opens the add dialog instead of choosing anything. Lower case
 * with a space, so `AGENCY_CODE_RE` could never produce a real code that
 * collides with it.
 */
export const NEW_AGENCY = "new agency";

export function AgencySelect({
  value,
  onValueChange,
  tip,
  ariaLabel = "Agency",
  className = "w-full",
  size = "md",
  disabled = false,
}: {
  value: string | null;
  onValueChange: (code: string | null) => void;
  tip: string;
  ariaLabel?: string;
  className?: string;
  size?: "sm" | "md";
  disabled?: boolean;
}) {
  const agencies = useAgencies();
  const { isAdmin } = useCurrentUser();
  const [adding, setAdding] = useState(false);
  const known = agencies.data ?? [];

  // A deactivated agency is not offered — that is what deactivating it was for —
  // but a channel already mapped to one still has to show what it is mapped to,
  // or saving the dialog would quietly move the channel somewhere else.
  const offered = known.filter((agency) => agency.active || agency.code === value);

  // A talkgroup can already carry a code this deployment's agency table has since
  // dropped. Silently showing "— none —" for it would erase the mapping on the
  // next save, so the stale code stays in the list, marked as what it is.
  const orphan = value !== null && !known.some((agency) => agency.code === value);

  return (
    <>
      <Select
        value={value ?? NO_AGENCY}
        onValueChange={(next) => {
          if (next === NEW_AGENCY) {
            setAdding(true);
            return;
          }
          onValueChange(next === NO_AGENCY ? null : next);
        }}
        options={[
          { value: NO_AGENCY, label: "— none —" },
          ...offered.map((agency) => ({
            value: agency.code,
            label: `${agency.name} (${agency.code})${agency.active ? "" : " — inactive"}`,
          })),
          ...(orphan ? [{ value, label: `${value} (not in the agency table)` }] : []),
          // Last, and only for an administrator: it is an action in a list of
          // values, and putting it anywhere else would make it a mis-click.
          ...(isAdmin ? [{ value: NEW_AGENCY, label: "New agency…" }] : []),
        ]}
        tip={tip}
        ariaLabel={ariaLabel}
        className={className}
        size={size}
        disabled={disabled}
      />
      {isAdmin ? (
        <AgencyDialog
          open={adding}
          onOpenChange={setAdding}
          existingCodes={known.map((agency) => agency.code)}
          // Selecting it straight away is the whole point of adding it here.
          onCreated={onValueChange}
        />
      ) : null}
    </>
  );
}
