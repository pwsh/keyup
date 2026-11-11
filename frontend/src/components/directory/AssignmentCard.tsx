import { useState } from "react";
import { Link } from "react-router";
import {
  useAssignRadio,
  useCurrentUser,
  useEndAssignment,
  usePersonnel,
  useRadioAssignments,
  useUnits,
} from "../../api/hooks";
import type { RadioDetail } from "../../api/types";
import { HELP } from "../../help/help";
import { formatDate } from "../../lib/format";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Card, CardHeader } from "../ui/Card";
import { Select } from "../ui/Select";
import { SkeletonText } from "../ui/Skeleton";
import { EmptyState } from "../ui/States";

const NONE = "none";

/**
 * Who a radio belongs to: the unit it lives on, the person carrying it, and how
 * that has changed.
 *
 * The two are separate rows of controls because they are separate facts —
 * reassigning the apparatus does not take the handset out of anybody's hands, and
 * ending a shift does not move the radio off the engine. Ending an assignment
 * closes it rather than deleting it, which is why the history below can be long
 * and is worth reading.
 */
export function AssignmentCard({ radio }: { radio: RadioDetail }) {
  const { canWrite } = useCurrentUser();
  // Both lists exist only to fill the two pickers below, which a read-only viewer never
  // sees — and the roster endpoints answer a guest with a 401, so asking would put an
  // error toast on a page that is working perfectly well.
  const units = useUnits({ page_size: 500 }, canWrite);
  const personnel = usePersonnel({ active: true, page_size: 500 }, canWrite);
  const assignments = useRadioAssignments(radio.id);
  const assign = useAssignRadio(radio.id);
  const end = useEndAssignment(radio.id);

  const [unitChoice, setUnitChoice] = useState(NONE);
  const [personChoice, setPersonChoice] = useState(NONE);

  const unitOptions = [
    { value: NONE, label: "Choose a unit…" },
    ...(units.data?.items ?? []).map((unit) => ({
      value: String(unit.id),
      label: unit.name ? `${unit.callsign} — ${unit.name}` : unit.callsign,
    })),
  ];
  const personOptions = [
    { value: NONE, label: "Choose a person…" },
    ...(personnel.data?.items ?? []).map((person) => ({
      value: String(person.id),
      label: person.rank ? `${person.name} (${person.rank})` : person.name,
    })),
  ];

  const apply = (kind: "unit" | "person") => {
    const choice = kind === "unit" ? unitChoice : personChoice;
    if (choice === NONE) return;
    assign.mutate(kind === "unit" ? { unit_id: Number(choice) } : { person_id: Number(choice) }, {
      onSuccess: () => {
        toast(kind === "unit" ? "Radio assigned to the unit" : "Radio handed over", {
          tone: "success",
        });
        setUnitChoice(NONE);
        setPersonChoice(NONE);
      },
      onError: () => toast("Could not change the assignment", { tone: "error" }),
    });
  };

  const stop = (kind: "unit" | "person") =>
    end.mutate(kind, {
      onSuccess: () => toast("Assignment ended", { tone: "success" }),
      onError: () => toast("Could not end the assignment", { tone: "error" }),
    });

  const history = assignments.data ?? [];

  return (
    <Card>
      <CardHeader title="Assignment" hint={HELP["roster.assign.card"]} />

      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="w-16 shrink-0 text-xs font-medium text-ink-2">Unit</span>
          {radio.current_unit ? (
            // The unit's own page is signed-in only, so a guest gets the name without a
            // link that would bounce them to the sign-in form.
            canWrite ? (
              <Link to={`/units/${radio.current_unit.id}`} className="text-sm text-ink hover:underline">
                {radio.current_unit.callsign}
                {radio.current_unit.name ? ` — ${radio.current_unit.name}` : ""}
              </Link>
            ) : (
              <span className="text-sm text-ink">
                {radio.current_unit.callsign}
                {radio.current_unit.name ? ` — ${radio.current_unit.name}` : ""}
              </span>
            )
          ) : (
            <span className="text-sm text-muted">Unassigned</span>
          )}
          {canWrite ? (
            <div className="ml-auto flex items-center gap-1.5">
              <Select
                value={unitChoice}
                onValueChange={setUnitChoice}
                options={unitOptions}
                tip={HELP["roster.assign.unit"]}
                ariaLabel="Unit to assign"
                size="sm"
                className="w-56"
              />
              <Button
                tip={HELP["roster.assign.apply"]}
                size="sm"
                variant="primary"
                disabled={unitChoice === NONE || assign.isPending}
                onClick={() => apply("unit")}
              >
                Assign
              </Button>
              <Button
                tip={HELP["roster.assign.end"]}
                size="sm"
                disabled={!radio.current_unit || end.isPending}
                onClick={() => stop("unit")}
              >
                End
              </Button>
            </div>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span className="w-16 shrink-0 text-xs font-medium text-ink-2">Person</span>
          {radio.current_person ? (
            canWrite ? (
              <Link to={`/personnel/${radio.current_person.id}`} className="text-sm text-ink hover:underline">
                {radio.current_person.name}
              </Link>
            ) : (
              <span className="text-sm text-ink">{radio.current_person.name}</span>
            )
          ) : (
            <span className="text-sm text-muted">Nobody signed for it</span>
          )}
          {canWrite ? (
            <div className="ml-auto flex items-center gap-1.5">
              <Select
                value={personChoice}
                onValueChange={setPersonChoice}
                options={personOptions}
                tip={HELP["roster.assign.person"]}
                ariaLabel="Person to assign"
                size="sm"
                className="w-56"
              />
              <Button
                tip={HELP["roster.assign.apply"]}
                size="sm"
                variant="primary"
                disabled={personChoice === NONE || assign.isPending}
                onClick={() => apply("person")}
              >
                Assign
              </Button>
              <Button
                tip={HELP["roster.assign.end"]}
                size="sm"
                disabled={!radio.current_person || end.isPending}
                onClick={() => stop("person")}
              >
                End
              </Button>
            </div>
          ) : null}
        </div>
      </div>

      <div className="border-t border-line">
        <CardHeader title="History" hint={HELP["roster.assign.history"]} />
        {assignments.isLoading ? (
          <SkeletonText lines={3} className="p-4" />
        ) : history.length === 0 ? (
          <EmptyState title="Never assigned" hint="Assign this radio to a unit or a person above." />
        ) : (
          <ul className="divide-y divide-line">
            {history.map((row) => (
              <li key={row.id} className="flex items-center gap-3 px-4 py-2 text-xs">
                <span className="w-14 shrink-0 text-muted">{row.kind}</span>
                <span className="min-w-0 flex-1 truncate text-ink">
                  {row.unit ? (row.unit.name ?? row.unit.callsign) : (row.person?.name ?? "—")}
                </span>
                <span className="shrink-0 tabular-nums text-ink-2">
                  {formatDate(row.effective_from)} – {row.effective_to ? formatDate(row.effective_to) : "now"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
