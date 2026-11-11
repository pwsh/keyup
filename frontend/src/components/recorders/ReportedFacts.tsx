import type { ReactNode } from "react";
import type { RecorderStation, TunerDescription } from "../../api/types";
import { formatNumber } from "../../lib/format";
import { InfoDot } from "../help/InfoDot";
import { Section } from "./TunerEditDialog";

/**
 * What the *recorder* says about its own hardware, rendered inside the dialogs where the
 * operator makes the decisions that hardware constrains.
 *
 * None of it is editable here and none of it ever will be: the description lives in trunkd's
 * config, and keyup only stores what the status push carries. It is on these forms anyway
 * because "Antenna B is 152-470 MHz only - never tune it to 700/800/900" is exactly the fact
 * an operator needs *while* picking a band, and a tooltip two screens away on a table cell is
 * a fact nobody reads until after the mistake.
 *
 * Every field may be missing, so nothing here renders a placeholder: an absent field is
 * dropped, and a description with nothing readable in it drops the whole section rather than
 * leaving an empty box implying the recorder said something.
 */

/** A string worth rendering: present, and not just whitespace. */
function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * One reported label/value pair, laid out to the metrics of a {@link Field} without being one.
 *
 * `Field` labels a *control*: it owns an id, wires a `<label htmlFor>` and takes a help dot of
 * its own. None of that is true of a fact, and borrowing it would give the dialog a row of
 * labels pointing at nothing — so this matches its spacing and stops there.
 */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <span className="mb-1 flex min-h-6 items-center gap-1 text-xs font-medium text-ink-2">{label}</span>
      <span className="block break-words text-sm text-ink">{children}</span>
    </div>
  );
}

/** The facts in the same two columns the dialogs' fields use, so the block reads as one page. */
function FactGrid({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">{children}</div>;
}

/**
 * The operator's notes, in full.
 *
 * Never truncated and never only a tooltip: the notes are where the constraints are written —
 * which USB port the device is on, which antenna must never see 800 MHz — and a constraint
 * clipped at one line is a constraint that can read as its opposite. Line breaks are kept
 * because operators write these as lists.
 */
function Notes({ notes, testId }: { notes: string; testId: string }) {
  return (
    <p className="whitespace-pre-wrap break-words text-xs leading-relaxed text-ink" data-testid={testId}>
      {notes}
    </p>
  );
}

/** Whether a tuner description has anything readable in it at all. */
export function describedHardware(description: TunerDescription | null | undefined): boolean {
  if (!description) return false;
  return (
    text(description.receiver) !== null ||
    text(description.antenna) !== null ||
    text(description.cable) !== null ||
    text(description.amplifier) !== null ||
    text(description.notes) !== null ||
    num(description.height_m) !== null
  );
}

/**
 * The tuner's hardware as its operator described it, above the settings it constrains.
 *
 * Absent entirely on a tuner that has reported no description — which is every tuner on an
 * older recorder binary, and plenty on a current one.
 */
export function HardwareSection({ description }: { description: TunerDescription | null | undefined }) {
  if (!describedHardware(description)) return null;
  const receiver = text(description?.receiver);
  const antenna = text(description?.antenna);
  const cable = text(description?.cable);
  const amplifier = text(description?.amplifier);
  const height = num(description?.height_m);
  const notes = text(description?.notes);

  return (
    <Section
      title="Hardware"
      hint={
        <>
          Reported by the recorder; edit it in trunkd's panel or its <code>[[device]]</code> block.
          <InfoDot id="admin.recorder.hardware" />
        </>
      }
    >
      <FactGrid>
        {receiver ? <Fact label="Receiver">{receiver}</Fact> : null}
        {antenna ? <Fact label="Antenna">{antenna}</Fact> : null}
        {cable ? <Fact label="Cable">{cable}</Fact> : null}
        {amplifier ? <Fact label="Amplifier">{amplifier}</Fact> : null}
        {height !== null ? (
          <Fact label="Height (m)">
            <span className="tabular-nums">{formatNumber(height)}</span>
          </Fact>
        ) : null}
      </FactGrid>
      {notes ? <Notes notes={notes} testId="hardware-notes" /> : null}
    </Section>
  );
}

/** Whether a station has anything readable in it at all. */
export function describedStation(station: RecorderStation | null | undefined): boolean {
  if (!station) return false;
  return (
    text(station.name) !== null ||
    text(station.location) !== null ||
    text(station.contact) !== null ||
    text(station.notes) !== null ||
    num(station.latitude) !== null ||
    num(station.longitude) !== null ||
    num(station.elevation_m) !== null
  );
}

/**
 * The station as its operator described it, above the recorder's own settings — the
 * instance-level twin of {@link HardwareSection}, and read for the same reason: "kept off
 * unless the roof box goes quiet" changes what an operator does with an upload hold.
 */
export function StationSection({ station }: { station: RecorderStation | null | undefined }) {
  if (!describedStation(station)) return null;
  const name = text(station?.name);
  const location = text(station?.location);
  const contact = text(station?.contact);
  const elevation = num(station?.elevation_m);
  const notes = text(station?.notes);

  return (
    <Section
      title="Station"
      hint={
        <>
          Reported by the recorder; edit it in trunkd's panel or its <code>[station]</code> block.
          <InfoDot id="admin.recorder.settings.station" />
        </>
      }
    >
      <FactGrid>
        {name ? <Fact label="Name">{name}</Fact> : null}
        {location ? <Fact label="Location">{location}</Fact> : null}
        {contact ? <Fact label="Contact">{contact}</Fact> : null}
        {elevation !== null ? (
          <Fact label="Elevation (m)">
            <span className="tabular-nums">{formatNumber(elevation)}</span>
          </Fact>
        ) : null}
      </FactGrid>
      {notes ? <Notes notes={notes} testId="station-notes" /> : null}
    </Section>
  );
}

/**
 * The one-line constraint that belongs beside the Role and Bands controls.
 *
 * The Hardware section above already carries the full text; these are the same facts *where
 * the choice is made*, because scrolling back up to re-read the antenna is the step an
 * operator skips. Both return `null` when there is nothing to say, so no control grows an
 * empty hint line.
 */
export function antennaHint(description: TunerDescription | null | undefined): string | null {
  return text(description?.antenna);
}

/** The first line of the operator's notes, for the same inline use as {@link antennaHint}. */
export function notesFirstLine(description: TunerDescription | null | undefined): string | null {
  const notes = text(description?.notes);
  if (notes === null) return null;
  return text(notes.split("\n")[0]);
}
