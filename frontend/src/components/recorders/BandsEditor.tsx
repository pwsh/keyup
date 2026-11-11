import { useState } from "react";
import {
  BAND_LABELS,
  type BandLabel,
  bandsReachSpan,
  canonicalBand,
  formatBand,
  formatBands,
  hzFromMhz,
  mhz,
  sameBands,
} from "../../lib/bands";
import { Button } from "../ui/Button";
import { TextInput } from "../ui/Field";

/**
 * The band editor: five label chips, any number of explicit MHz ranges, and the three
 * states the desired-state contract actually has.
 *
 * The states are the reason this is not just a chip row. An **empty list** is a real thing
 * to want — "record anything in your span" — and is not the same as keyup **saying nothing
 * about bands**, which leaves the recorder on whatever its own configuration holds. The
 * page has to be able to express both, or the only way back from a restriction an operator
 * regrets is a text editor on the recorder.
 *
 * Frequencies are typed and shown in megahertz and stored in whole hertz: nobody discusses
 * a band edge in hertz, and the contract is in hertz, so the conversion belongs here rather
 * than in the operator's head.
 */
export function BandsEditor({
  bands,
  onChange,
  applied,
  managed,
  inherit,
  onInherit,
  centerHz,
  spanHz,
}: {
  /** The working list, in wire form. */
  bands: string[];
  onChange: (next: string[]) => void;
  /** What the recorder says it has applied; empty means unrestricted. */
  applied: string[];
  /** Whether keyup is currently managing this tuner's bands at all. */
  managed: boolean;
  /** Whether the operator has asked to hand the bands back to the recorder. */
  inherit: boolean;
  onInherit: (value: boolean) => void;
  centerHz: number | null;
  spanHz: number | null;
}) {
  const [lo, setLo] = useState("");
  const [hi, setHi] = useState("");
  const [error, setError] = useState<string | null>(null);
  const selected = new Set(bands.map(canonicalBand));
  const ranges = bands.filter((band) => !(BAND_LABELS as readonly string[]).includes(canonicalBand(band)));

  const set = (next: string[]) => {
    onInherit(false);
    onChange([...new Set(next.map(canonicalBand))]);
  };
  const toggleLabel = (label: BandLabel) =>
    set(selected.has(label) ? bands.filter((band) => canonicalBand(band) !== label) : [...bands, label]);
  const addRange = () => {
    const loHz = hzFromMhz(lo);
    const hiHz = hzFromMhz(hi);
    if (loHz === null || hiHz === null) {
      setError("Give both edges of the range in MHz.");
      return;
    }
    if (loHz > hiHz) {
      setError("The low edge has to come first.");
      return;
    }
    setError(null);
    set([...bands, `${loHz}-${hiHz}`]);
    setLo("");
    setHi("");
  };

  const pending = !sameBands(bands, applied);
  const spanLo = centerHz !== null && spanHz !== null && spanHz > 0 ? centerHz - spanHz / 2 : null;
  const spanHi = centerHz !== null && spanHz !== null && spanHz > 0 ? centerHz + spanHz / 2 : null;
  // A warning, never a block: trunkd is the one that decides, and an operator who is about
  // to retune the tuner too knows something the last status push does not.
  const misses = spanLo !== null && spanHi !== null && !inherit && !bandsReachSpan(bands, spanLo, spanHi);

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {BAND_LABELS.map((label) => {
          const on = selected.has(label);
          return (
            <button
              key={label}
              type="button"
              aria-pressed={on}
              onClick={() => toggleLabel(label)}
              className={`rounded-full border px-2.5 py-0.5 text-[11px] ${
                on ? "border-accent bg-accent-soft text-accent-ink" : "border-line text-ink-2"
              }`}
            >
              {formatBand(label)}
            </button>
          );
        })}
      </div>

      {ranges.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {ranges.map((band) => (
            <span
              key={band}
              className="flex items-center gap-1 rounded-full border border-accent bg-accent-soft px-2.5 py-0.5 text-[11px] text-accent-ink"
            >
              <span className="tabular-nums">{formatBand(band)}</span>
              <button
                type="button"
                aria-label={`Remove ${formatBand(band)}`}
                onClick={() => set(bands.filter((entry) => entry !== band))}
                className="text-accent-ink hover:text-critical-ink"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-1.5">
        <TextInput
          inputMode="decimal"
          aria-label="Range low edge, MHz"
          placeholder="856"
          className="h-8 w-24 tabular-nums"
          value={lo}
          onChange={(event) => setLo(event.target.value)}
        />
        <span className="text-[11px] text-ink-2">–</span>
        <TextInput
          inputMode="decimal"
          aria-label="Range high edge, MHz"
          placeholder="866"
          className="h-8 w-24 tabular-nums"
          value={hi}
          onChange={(event) => setHi(event.target.value)}
        />
        <span className="text-[11px] text-ink-2">MHz</span>
        <Button size="sm" variant="ghost" tip="Add this range to the list" onClick={addRange}>
          Add range
        </Button>
        {bands.length > 0 ? (
          <Button
            size="sm"
            variant="ghost"
            tip="Let this tuner record its whole span"
            onClick={() => set([])}
          >
            Clear
          </Button>
        ) : null}
      </div>

      {error ? <p className="text-[11px] text-critical-ink">{error}</p> : null}

      <p className="text-[11px] text-ink-2">
        {inherit
          ? "Keyup will stop saying anything about this tuner's bands; the recorder keeps its own."
          : bands.length === 0
            ? "Unrestricted — this tuner may record anything inside its capture span."
            : `Wanted: ${formatBands(bands)}.`}
      </p>
      <p className="text-[11px] text-ink-2" data-testid="bands-applied">
        Applied: <span className="tabular-nums">{formatBands(applied)}</span>
        {pending && !inherit ? (
          <span className="ml-1.5 rounded-full border border-warning px-1.5 py-0.5 text-[10px] text-ink">
            pending
          </span>
        ) : null}
      </p>
      {misses ? (
        <p className="text-[11px] text-warning-ink">
          None of these reach the tuner's span ({mhz(spanLo ?? 0)}–{mhz(spanHi ?? 0)} MHz). The recorder will
          refuse the list and stay on what it has.
        </p>
      ) : null}
      {managed && !inherit ? (
        <button
          type="button"
          onClick={() => onInherit(true)}
          className="text-[11px] text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink"
        >
          Stop managing this tuner's bands
        </button>
      ) : null}
      {inherit ? (
        <button
          type="button"
          onClick={() => onInherit(false)}
          className="text-[11px] text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink"
        >
          Keep managing them after all
        </button>
      ) : null}
    </div>
  );
}
