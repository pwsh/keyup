import { ChevronDown } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { useSaveTunerDesired, useSiteChannelAudit } from "../../api/hooks";
import {
  type Recorder,
  TUNER_ROLES,
  type Tuner,
  type TunerDesiredInput,
  type TunerGainElement,
  type TunerRole,
  tunerGain,
} from "../../api/types";
import { HELP } from "../../help/help";
import { bandList, hzFromMhz, mhz } from "../../lib/bands";
import { cn } from "../../lib/cn";
import { siteLabel } from "../../lib/format";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, FieldGrid, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { Switch } from "../ui/Switch";
import { Tip } from "../ui/Tooltip";
import { BandsEditor } from "./BandsEditor";
import { antennaHint, HardwareSection, notesFirstLine } from "./ReportedFacts";

/**
 * Everything an operator may tell one tuner, in one dialog.
 *
 * It replaces the old drawer rather than sitting beside it, because the settings it adds are
 * not separable from the ones that were already there: a sample rate and a band list are the
 * same decision seen twice, and a gain typed on one screen while the span is read off
 * another is how a tuner ends up deaf.
 *
 * Three rules run through the whole form and are worth stating once rather than at each
 * control:
 *
 * * **Only what was touched is sent.** The desired state is a merge, and a field keyup has
 *   never written is a field the recorder is running on *its own* configuration. Sending
 *   every input on every save would quietly take all of them over the first time anyone
 *   opened the dialog, and a field keyup manages is a field the recorder can no longer be
 *   fixed on locally. So each control marks itself changed, and nothing else goes in the body.
 * * **The tuner's reported state is the authority.** Which sections appear, which gain rows
 *   exist and what they are bounded by all come from the last status push. A tuner that has
 *   reported nothing gets the plain form and the server's own checks, not a guess.
 * * **Refusals are shown where the value is.** The recorder acknowledges a desired version
 *   whether or not it could use every field in it, so a refused setting leaves no "pending"
 *   badge behind — only the `rejected` map, rendered under the input it belongs to.
 */
export function TunerEditDialog({
  recorder,
  tuner,
  sites,
  onClose,
}: {
  recorder: Recorder;
  tuner: Tuner;
  sites: { id: number; name: string; system_name?: string | null }[];
  onClose: () => void;
}) {
  const save = useSaveTunerDesired();
  const desired = tuner.desired ?? {};
  const reported = tuner.reported ?? {};
  const gain = tunerGain(reported);

  /**
   * Which backend this is, as the tuner itself says.
   *
   * `null` when it has not said — and that is a third case, not a default. A HackRF gets the
   * step controls, a Soapy device gets its named elements, and a tuner that has reported
   * nothing gets both of the generic controls and no invented ones.
   */
  const backend = typeof reported.backend === "string" ? reported.backend.toLowerCase() : null;
  const isHackrf = backend === "hackrf";
  const elements: TunerGainElement[] = Array.isArray(reported.gain_elements)
    ? (reported.gain_elements as TunerGainElement[])
    : [];
  const restartOn = new Set(reported.capabilities?.restart_on ?? []);

  // What the operator wrote about this tuner's hardware. It is shown twice on purpose: in
  // full at the top, and as a line under the two controls it constrains — the role and the
  // bands — because that is where the constraint is either honoured or broken.
  const description = reported.description ?? null;
  const antenna = antennaHint(description);
  const hardwareNote = notesFirstLine(description);

  // ---- what the operator has actually changed --------------------------------------
  //
  // A `Set` of field names rather than a dirty flag per input: the body is built from it
  // directly, so "touched" and "sent" cannot drift apart.
  const [changed, setChanged] = useState<Set<string>>(new Set());
  const touch = (field: string) => setChanged((previous) => new Set(previous).add(field));

  const [role, setRole] = useState<TunerRole>(
    (desired.role ?? (reported.role as TunerRole) ?? "auto") as TunerRole,
  );
  const [siteValue, setSiteValue] = useState<string>(desired.site_id != null ? String(desired.site_id) : "");
  const [center, setCenter] = useState<string>(
    desired.center_frequency != null
      ? String(desired.center_frequency)
      : reported.center_hz != null
        ? String(reported.center_hz)
        : "",
  );
  const [enabled, setEnabled] = useState<boolean>(desired.enabled ?? true);
  const [spectrum, setSpectrum] = useState<boolean>(desired.spectrum ?? false);

  // Seeded from what keyup wants, or failing that from what the tuner reports, so opening
  // the dialog never proposes a change to a setting nobody touched.
  const seed = <T,>(wanted: T | null | undefined, running: T | null | undefined): string =>
    wanted != null ? String(wanted) : running != null ? String(running) : "";

  const [sampleRate, setSampleRate] = useState(seed(desired.sample_rate, reported.sample_rate));
  const [bandwidth, setBandwidth] = useState(seed(desired.bandwidth_hz, reported.bandwidth_hz));
  const [ppm, setPpm] = useState(seed(desired.ppm, reported.ppm));
  const [autoPpm, setAutoPpm] = useState<boolean>(desired.auto_ppm ?? reported.auto_ppm ?? false);
  const [autoPpmLimit, setAutoPpmLimit] = useState(seed(desired.auto_ppm_limit, null));
  const [overallGain, setOverallGain] = useState(seed(desired.gain, gain.gain));
  const [agc, setAgc] = useState<boolean>(desired.agc ?? reported.agc ?? false);
  const [rxPort, setRxPort] = useState(seed(desired.rx_port, reported.rx_port));
  const [lna, setLna] = useState(seed(desired.lna_db, gain.lna_db));
  const [vga, setVga] = useState(seed(desired.vga_db, gain.vga_db));
  const [amp, setAmp] = useState<boolean>(desired.amp ?? gain.amp ?? false);
  const [dataFollow, setDataFollow] = useState<boolean>(desired.data_follow ?? reported.data_follow ?? false);
  const [remoteGrants, setRemoteGrants] = useState<boolean>(
    desired.remote_grants ?? (reported.remote_grants as boolean | undefined) ?? false,
  );
  const [gains, setGains] = useState<Record<string, string>>(() => {
    const running = reported.gains ?? {};
    const wanted = desired.gains ?? {};
    const out: Record<string, string> = {};
    for (const element of elements) {
      const value = wanted[element.name] ?? running[element.name];
      out[element.name] = value == null ? "" : String(value);
    }
    return out;
  });
  const [candidates, setCandidates] = useState<number[]>(
    () => desired.control_frequencies ?? reported.control_frequencies ?? [],
  );

  const appliedBands = bandList(reported.bands) ?? [];
  const managedBands = bandList(desired.bands);
  const [bands, setBands] = useState<string[]>(managedBands ?? appliedBands);
  const [bandsTouched, setBandsTouched] = useState(false);
  const [bandsInherit, setBandsInherit] = useState(false);

  // What the recorder refused: whatever it last reported, replaced by the answer to this
  // dialog's own save as soon as there is one.
  const [saved, setSaved] = useState<Tuner | null>(null);
  const rejected = saved?.rejected ?? tuner.rejected ?? null;

  const siteId = siteValue === "" ? null : Number(siteValue);
  const audit = useSiteChannelAudit(siteId);

  const centerHz = num(reported.center_hz);
  const spanHz = num(reported.span_hz);
  const spanLo = centerHz !== null && spanHz !== null && spanHz > 0 ? centerHz - spanHz / 2 : null;
  const spanHi = centerHz !== null && spanHz !== null && spanHz > 0 ? centerHz + spanHz / 2 : null;

  /**
   * The control channels this site is known to use, live one first.
   *
   * The audit endpoint already answers exactly this question — it is the read over
   * `site_frequencies` that separates the control channels from the traffic ones — so the
   * button borrows its answer rather than re-deriving one from raw frequencies here.
   */
  const known = useMemo<number[]>(() => {
    const control = audit.data?.control;
    if (!control) return audit.data?.recorder_config.suggested_control_frequencies ?? [];
    const ordered = [
      ...(control.observed !== null && control.observed !== undefined ? [control.observed] : []),
      ...control.observed_alternate,
      ...control.rr_primary,
      ...control.rr_alternate,
    ];
    return [...new Set(ordered)];
  }, [audit.data]);

  /**
   * Whether `Advanced` starts open.
   *
   * Collapsed it is two settings almost nobody changes; but a tuner that already carries a
   * hand-written candidate list, acts on remote grants, or has had something refused has
   * *state* in there, and state hidden behind a disclosure is state nobody reads.
   */
  const advancedOpen =
    candidates.length > 0 || remoteGrants || (rejected !== null && Object.keys(rejected).length > 0);

  const roleOptions = TUNER_ROLES.map((value) => ({ value, label: value }));
  const siteOptions = [
    { value: "", label: "None" },
    ...sites.map((s) => ({ value: String(s.id), label: siteLabel(s) })),
  ];

  const onSave = () => {
    const centerValue = center.trim() === "" ? null : Number(center);
    if (centerValue !== null && !Number.isFinite(centerValue)) {
      toast("Center frequency must be a number in hertz", { tone: "error" });
      return;
    }
    // The five fields the drawer has always written unconditionally stay that way: they are
    // what "keyup manages this tuner" has meant since the first version, and quietly
    // narrowing that now would leave existing tuners half-managed.
    const body: TunerDesiredInput = {
      role,
      site_id: siteId,
      center_frequency: centerValue,
      enabled,
      spectrum,
      ...(bandsInherit ? { bands: null } : bandsTouched ? { bands } : {}),
    };

    const numeric = (field: string, raw: string, whole: boolean): boolean => {
      if (!changed.has(field)) return true;
      if (raw.trim() === "") {
        // An emptied box is "stop saying anything about this", which is the explicit null.
        (body as Record<string, unknown>)[field] = null;
        return true;
      }
      const value = Number(raw);
      if (!Number.isFinite(value) || (whole && !Number.isInteger(value))) {
        toast(`${field.replace(/_/g, " ")} must be a number`, { tone: "error" });
        return false;
      }
      (body as Record<string, unknown>)[field] = value;
      return true;
    };

    for (const [field, raw, whole] of [
      ["sample_rate", sampleRate, true],
      ["bandwidth_hz", bandwidth, true],
      ["ppm", ppm, false],
      ["auto_ppm_limit", autoPpmLimit, false],
      ["gain", overallGain, false],
      ["lna_db", lna, true],
      ["vga_db", vga, true],
    ] as [string, string, boolean][]) {
      if (!numeric(field, raw, whole)) return;
    }
    if (changed.has("auto_ppm")) body.auto_ppm = autoPpm;
    if (changed.has("agc")) body.agc = agc;
    if (changed.has("amp")) body.amp = amp;
    if (changed.has("data_follow")) body.data_follow = dataFollow;
    if (changed.has("remote_grants")) body.remote_grants = remoteGrants;
    if (changed.has("rx_port")) body.rx_port = rxPort.trim() === "" ? null : rxPort.trim();
    if (changed.has("control_frequencies")) body.control_frequencies = candidates;
    if (changed.has("gains")) {
      const out: Record<string, number> = {};
      for (const [name, raw] of Object.entries(gains)) {
        if (raw.trim() === "") continue;
        const value = Number(raw);
        if (!Number.isFinite(value)) {
          toast(`Gain for ${name} must be a number of dB`, { tone: "error" });
          return;
        }
        out[name] = value;
      }
      body.gains = out;
    }

    save.mutate(
      { recorderId: recorder.id, tunerKey: tuner.tuner_key, desired: body },
      {
        onSuccess: (result) => {
          const refused = result?.rejected ?? null;
          if (refused && Object.keys(refused).length > 0) {
            // Saved, but the recorder could not use all of it. Staying open is the point:
            // the reasons sit under the inputs they belong to, and closing would leave the
            // operator with a success toast and a setting that never took.
            setSaved(result);
            toast("Saved — the recorder refused some of it", {
              tone: "error",
              body: "The reasons are under the fields it refused.",
            });
            return;
          }
          toast("Desired state saved", { tone: "success" });
          onClose();
        },
        onError: (error) => mutationErrorToast(error, "Could not save the tuner"),
      },
    );
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={`Tuner ${tuner.tuner_key}`}
      description={`${recorder.label ?? recorder.instance_id} · ${backend ?? "unknown backend"} · span ${spanText(centerHz, spanHz)}`}
      className="w-[min(45rem,calc(100vw-2rem))]"
      footer={
        <>
          <Button variant="ghost" tip="Discard these changes" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            tip={HELP["admin.recorder.save"]}
            disabled={save.isPending}
            onClick={onSave}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <HardwareSection description={description} />

        <Section title="Identity" hint="What this tuner is for, and which site it serves.">
          <FieldGrid>
            <Field
              label="Role"
              help="admin.recorder.role"
              hint={hardwareNote ? <span data-testid="role-hardware-note">{hardwareNote}</span> : undefined}
            >
              {() => (
                <Select
                  value={role}
                  onValueChange={(value) => setRole(value as TunerRole)}
                  options={roleOptions}
                  ariaLabel="Tuner role"
                  tip={HELP["admin.recorder.role"]}
                  className="w-full"
                />
              )}
            </Field>
            <Field label="Focus site" help="admin.recorder.site">
              {() => (
                <Select
                  value={siteValue}
                  onValueChange={setSiteValue}
                  options={siteOptions}
                  ariaLabel="Focus site"
                  tip={HELP["admin.recorder.site"]}
                  className="w-full"
                />
              )}
            </Field>
          </FieldGrid>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <Switch
              checked={enabled}
              onCheckedChange={setEnabled}
              label="Enabled"
              tip={HELP["admin.recorder.enabled"]}
            />
          </div>
        </Section>

        <Section title="Tuning" hint="Where this tuner sits, and how wide it listens.">
          <FieldGrid>
            <Field
              label="Center frequency (Hz)"
              help="admin.recorder.center"
              hint="Must fall within the tuner's span."
            >
              {(id) => (
                <TextInput
                  id={id}
                  inputMode="numeric"
                  className="tabular-nums"
                  value={center}
                  onChange={(event) => setCenter(event.target.value)}
                  placeholder="851025000"
                />
              )}
            </Field>
            <Field
              label="Sample rate (Hz)"
              badge={<Restarts on={restartOn} field="sample_rate" />}
              help="admin.recorder.sampleRate"
              hint={
                <>
                  Divided by 12 500 Hz it has to come out an <em>even</em> whole number of at least four
                  channels — so 50 000 Hz up, in steps of 25 000 Hz.
                  {rejectedFor(rejected, "sample_rate")}
                </>
              }
            >
              {(id) => (
                <TextInput
                  id={id}
                  inputMode="numeric"
                  className="tabular-nums"
                  value={sampleRate}
                  placeholder="2400000"
                  onChange={(event) => {
                    touch("sample_rate");
                    setSampleRate(event.target.value);
                  }}
                />
              )}
            </Field>
            <Field
              label="Filter bandwidth (Hz)"
              badge={<Restarts on={restartOn} field="bandwidth_hz" />}
              help="admin.recorder.bandwidth"
              hint={<>Empty or 0 lets the driver choose.{rejectedFor(rejected, "bandwidth_hz")}</>}
            >
              {(id) => (
                <TextInput
                  id={id}
                  inputMode="numeric"
                  className="tabular-nums"
                  value={bandwidth}
                  onChange={(event) => {
                    touch("bandwidth_hz");
                    setBandwidth(event.target.value);
                  }}
                />
              )}
            </Field>
          </FieldGrid>
          <Field
            label="Bands"
            help="admin.recorder.bands"
            hint={
              <>
                Which downlink ranges this tuner may record, inside the span above.
                {antenna ? (
                  <span className="mt-0.5 block" data-testid="bands-antenna-note">
                    Antenna: {antenna}
                  </span>
                ) : null}
              </>
            }
          >
            {() => (
              <BandsEditor
                bands={bands}
                onChange={(next) => {
                  setBandsTouched(true);
                  setBands(next);
                }}
                applied={appliedBands}
                managed={managedBands !== null}
                inherit={bandsInherit}
                onInherit={(value) => {
                  setBandsTouched(true);
                  setBandsInherit(value);
                }}
                centerHz={centerHz}
                spanHz={spanHz}
              />
            )}
          </Field>
        </Section>

        <Section
          title="Gain"
          hint={
            isHackrf
              ? "The HackRF's two stages, on their own steps. The level meter on the row above is read against these."
              : "The stages this device reports, each bounded by the range it gave."
          }
        >
          {isHackrf ? (
            <>
              <FieldGrid>
                <StepField
                  label="LNA (dB)"
                  help="admin.recorder.lna"
                  value={lna}
                  max={40}
                  step={8}
                  rejected={rejectedFor(rejected, "lna_db")}
                  restarts={<Restarts on={restartOn} field="lna_db" />}
                  onChange={(value) => {
                    touch("lna_db");
                    setLna(value);
                  }}
                />
                <StepField
                  label="VGA (dB)"
                  help="admin.recorder.vga"
                  value={vga}
                  max={62}
                  step={2}
                  rejected={rejectedFor(rejected, "vga_db")}
                  restarts={<Restarts on={restartOn} field="vga_db" />}
                  onChange={(value) => {
                    touch("vga_db");
                    setVga(value);
                  }}
                />
              </FieldGrid>
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                <Switch
                  checked={amp}
                  onCheckedChange={(value) => {
                    touch("amp");
                    setAmp(value);
                  }}
                  label="Front-end amplifier"
                  tip={HELP["admin.recorder.amp"]}
                />
                {rejectedFor(rejected, "amp")}
              </div>
            </>
          ) : (
            <>
              {elements.length > 0 ? (
                <div className="space-y-2">
                  {elements.map((element) => (
                    <GainElementRow
                      key={element.name}
                      element={element}
                      value={gains[element.name] ?? ""}
                      onChange={(next) => {
                        touch("gains");
                        setGains((previous) => ({ ...previous, [element.name]: next }));
                      }}
                    />
                  ))}
                </div>
              ) : (
                <p className="text-[11px] text-ink-2">
                  This tuner has not reported which gain stages it has, so there is nothing to bound a
                  per-stage slider with. Set the overall gain below instead.
                </p>
              )}
              {rejectedFor(rejected, "gains")}
              <FieldGrid>
                <Field
                  label="Overall gain (dB)"
                  help="admin.recorder.gain"
                  hint={rejectedFor(rejected, "gain")}
                >
                  {(id) => (
                    <TextInput
                      id={id}
                      inputMode="decimal"
                      className="tabular-nums"
                      value={overallGain}
                      onChange={(event) => {
                        touch("gain");
                        setOverallGain(event.target.value);
                      }}
                    />
                  )}
                </Field>
                <Field
                  label="Antenna port"
                  badge={<Restarts on={restartOn} field="rx_port" />}
                  help="admin.recorder.rxPort"
                  hint={<>Empty leaves the driver's own choice.{rejectedFor(rejected, "rx_port")}</>}
                >
                  {(id) => (
                    <TextInput
                      id={id}
                      value={rxPort}
                      placeholder="LNAW"
                      onChange={(event) => {
                        touch("rx_port");
                        setRxPort(event.target.value);
                      }}
                    />
                  )}
                </Field>
              </FieldGrid>
              <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                <Switch
                  checked={agc}
                  onCheckedChange={(value) => {
                    touch("agc");
                    setAgc(value);
                  }}
                  label="Hardware AGC"
                  tip={HELP["admin.recorder.agc"]}
                />
                {rejectedFor(rejected, "agc")}
              </div>
            </>
          )}
        </Section>

        <Section
          title="Corrections"
          hint="Crystal correction. A tuner that is a few ppm out drifts off the control channel as it warms up."
        >
          <FieldGrid>
            <Field
              label="Correction (ppm)"
              badge={<Restarts on={restartOn} field="ppm" />}
              help="admin.recorder.ppm"
              hint={<>Between −200 and 200.{rejectedFor(rejected, "ppm")}</>}
            >
              {(id) => (
                <TextInput
                  id={id}
                  inputMode="decimal"
                  className="tabular-nums"
                  value={ppm}
                  onChange={(event) => {
                    touch("ppm");
                    setPpm(event.target.value);
                  }}
                />
              )}
            </Field>
            <Field
              label="Auto correction limit (ppm)"
              help="admin.recorder.autoPpmLimit"
              hint={
                <>
                  How far the automatic correction may wander from zero.
                  {rejectedFor(rejected, "auto_ppm_limit")}
                </>
              }
            >
              {(id) => (
                <TextInput
                  id={id}
                  inputMode="decimal"
                  className="tabular-nums"
                  value={autoPpmLimit}
                  onChange={(event) => {
                    touch("auto_ppm_limit");
                    setAutoPpmLimit(event.target.value);
                  }}
                />
              )}
            </Field>
          </FieldGrid>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <Switch
              checked={autoPpm}
              onCheckedChange={(value) => {
                touch("auto_ppm");
                setAutoPpm(value);
              }}
              label="Track it automatically"
              tip={HELP["admin.recorder.autoPpm"]}
            />
            <p className="text-[11px] text-ink-2" data-testid="auto-ppm-applied">
              Applied now:{" "}
              <span className="tabular-nums text-ink">
                {num(reported.auto_ppm_applied) === null
                  ? "—"
                  : `${(reported.auto_ppm_applied as number).toFixed(2)} ppm`}
              </span>
            </p>
            {rejectedFor(rejected, "auto_ppm")}
          </div>
        </Section>

        <Section
          title="Data & spectrum"
          hint="Follow SNDCP data grants; needed for the unknown-data analysis. Spectrum ships this tuner's sweep to the band view."
        >
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <Switch
              checked={dataFollow}
              onCheckedChange={(value) => {
                touch("data_follow");
                setDataFollow(value);
              }}
              label="Follow data grants"
              tip={HELP["admin.recorder.dataFollow"]}
            />
            <Switch
              checked={spectrum}
              onCheckedChange={setSpectrum}
              label="Spectrum"
              tip={HELP["admin.recorder.spectrum"]}
            />
            {rejectedFor(rejected, "data_follow")}
          </div>
        </Section>

        <Section
          title="Advanced"
          hint="The link's share of the grants, and the frequencies this tuner hunts a control channel on."
          collapsible
          defaultOpen={advancedOpen}
        >
          <SubHeading>Link</SubHeading>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <Switch
              checked={remoteGrants}
              onCheckedChange={(value) => {
                touch("remote_grants");
                setRemoteGrants(value);
              }}
              label={
                <Restarts on={restartOn} field="remote_grants">
                  Act on remote grants
                </Restarts>
              }
              tip={HELP["admin.recorder.remoteGrants"]}
            />
            {rejectedFor(rejected, "remote_grants")}
          </div>
          {/* Said here rather than left to a refusal, because the setting is on the tuner and
              the thing it depends on is on the recorder — two dialogs apart. */}
          <p className="text-[11px] text-ink-2">
            Needs the recorder's link to be up and accepting them, which is set in{" "}
            <span className="text-ink">Recorder settings</span>. A recorder with no link refuses this and says
            so here.
          </p>

          <SubHeading>Control candidates</SubHeading>
          <ControlCandidates
            values={candidates}
            spanLo={spanLo}
            spanHi={spanHi}
            known={known}
            knownLoading={siteId !== null && audit.isLoading}
            hasSite={siteId !== null}
            onChange={(next) => {
              touch("control_frequencies");
              setCandidates(next);
            }}
          />
          {rejectedFor(rejected, "control_frequencies")}
        </Section>

        {rejected && Object.keys(rejected).length > 0 ? (
          <p className="text-[11px] text-warning-ink" data-testid="rejected-summary">
            The recorder refused {Object.keys(rejected).length} field
            {Object.keys(rejected).length === 1 ? "" : "s"} of the last change it applied. The reasons are
            under the fields above.
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}

/**
 * A titled block of the form — the dialog is long and needs landmarks.
 *
 * A section may be `collapsible`, which is how the settings almost nobody
 * touches stop costing everyone else a screenful. It opens itself when it has
 * something to say (`defaultOpen`), because a refusal or a hand-written
 * candidate list hidden behind a disclosure is a refusal nobody reads.
 */
export function Section({
  title,
  hint,
  children,
  collapsible = false,
  defaultOpen = true,
}: {
  title: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
  collapsible?: boolean;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();
  const shown = !collapsible || open;
  return (
    <section className="rounded-lg border border-line px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-xs font-medium text-ink-2">{title}</h3>
          {hint ? <p className="mt-0.5 text-[11px] text-ink-2">{hint}</p> : null}
        </div>
        {collapsible ? (
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={open}
            aria-controls={bodyId}
            tip={
              open ? `Hide the ${title.toLowerCase()} settings` : `Show the ${title.toLowerCase()} settings`
            }
            onClick={() => setOpen((was) => !was)}
          >
            <ChevronDown
              size={14}
              aria-hidden="true"
              className={cn("transition-transform", open && "rotate-180")}
            />
            <span className="ml-1">{open ? "Hide" : "Show"}</span>
          </Button>
        ) : null}
      </div>
      {shown ? (
        <div id={bodyId} className="mt-3 space-y-3">
          {children}
        </div>
      ) : null}
    </section>
  );
}

/** A heading inside a section, for the two or three blocks `Advanced` collects. */
export function SubHeading({ children }: { children: React.ReactNode }) {
  return <h4 className="text-xs font-medium text-ink-2">{children}</h4>;
}

/**
 * "restarts the tuner" beside a field the recorder says it cannot change live.
 *
 * Never a refusal, only a warning: a restart is a gap in the recording, which is a real cost
 * and sometimes the right one. The list comes from the tuner's own `capabilities.restart_on`,
 * so a device that can retune on the fly says nothing here.
 */
export function Restarts({
  on,
  field,
  children,
}: {
  on: Set<string>;
  field: string;
  children?: React.ReactNode;
}) {
  if (!on.has(field)) return <>{children}</>;
  return (
    <span className="inline-flex items-center gap-1.5">
      {children}
      <Tip label="Changing this tears the stream down and builds it again — a gap in the recording">
        <span className="rounded-full border border-warning px-1.5 py-0.5 text-[10px] font-normal text-ink">
          restarts the tuner
        </span>
      </Tip>
    </span>
  );
}

/** The recorder's reason for refusing one field, or nothing at all. */
export function rejectedFor(rejected: Record<string, string> | null, field: string) {
  const reason = rejected?.[field];
  if (!reason) return null;
  return (
    <span className="mt-1 block text-[11px] text-critical-ink" data-testid={`rejected-${field}`}>
      The recorder refused this: {reason}
    </span>
  );
}

/**
 * One gain stage, bounded by the range the tuner reported for it.
 *
 * A slider *and* a number box, because the two are used for different things: dragging is
 * how you find the level, and typing is how you reproduce the one that worked.
 */
function GainElementRow({
  element,
  value,
  onChange,
}: {
  element: TunerGainElement;
  value: string;
  onChange: (next: string) => void;
}) {
  const min = typeof element.min === "number" ? element.min : 0;
  const max = typeof element.max === "number" ? element.max : 50;
  const step = typeof element.step === "number" && element.step > 0 ? element.step : 0.5;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="w-14 shrink-0 text-xs font-medium text-ink-2">{element.name}</span>
      <input
        type="range"
        aria-label={`${element.name} gain, dB`}
        min={min}
        max={max}
        step={step}
        value={value === "" ? min : value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 min-w-40 flex-1 accent-accent"
      />
      <TextInput
        aria-label={`${element.name} gain in dB`}
        inputMode="decimal"
        className="h-8 w-20 tabular-nums"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <span className="text-[11px] tabular-nums text-ink-2">
        {min}–{max} dB
      </span>
    </div>
  );
}

/**
 * A gain that moves in fixed steps — the HackRF's two.
 *
 * A select rather than a number box because the hardware has exactly these values and
 * rounds anything else silently, so offering a free-text field would offer a lie.
 */
function StepField({
  label,
  help,
  value,
  max,
  step,
  rejected,
  restarts,
  onChange,
}: {
  label: string;
  help: Parameters<typeof Field>[0]["help"];
  value: string;
  max: number;
  step: number;
  rejected: React.ReactNode;
  restarts: React.ReactNode;
  onChange: (next: string) => void;
}) {
  const options = [
    { value: "", label: "None (leave it alone)" },
    ...Array.from({ length: Math.floor(max / step) + 1 }, (_, index) => {
      const db = index * step;
      return { value: String(db), label: `${db} dB` };
    }),
  ];
  return (
    <Field
      label={label}
      badge={restarts}
      help={help}
      hint={
        <>
          {step} dB steps to {max} dB.{rejected}
        </>
      }
    >
      {() => (
        <Select
          value={value}
          onValueChange={onChange}
          options={options}
          ariaLabel={label}
          tip={`${label}: ${step} dB steps to ${max} dB`}
          className="w-full"
        />
      )}
    </Field>
  );
}

/**
 * The control-channel candidates, typed and shown in megahertz and stored in whole hertz.
 *
 * The check against the span is a *warning* rather than a block for the same reason the band
 * editor's is: an operator adding a candidate may be about to retune the tuner as well, and
 * the last status push does not know that. The server refuses one that is genuinely outside,
 * which is the check that has the current reported state in front of it.
 */
function ControlCandidates({
  values,
  spanLo,
  spanHi,
  known,
  knownLoading,
  hasSite,
  onChange,
}: {
  values: number[];
  spanLo: number | null;
  spanHi: number | null;
  known: number[];
  knownLoading: boolean;
  hasSite: boolean;
  onChange: (next: number[]) => void;
}) {
  const [entry, setEntry] = useState("");
  const [error, setError] = useState<string | null>(null);

  const add = () => {
    const hz = hzFromMhz(entry);
    if (hz === null) {
      setError("Give the frequency in MHz.");
      return;
    }
    if (hz % 12_500 !== 0) {
      setError("A control channel sits on the 12.5 kHz grid.");
      return;
    }
    setError(null);
    setEntry("");
    if (!values.includes(hz)) onChange([...values, hz]);
  };

  const outside = (hz: number) => spanLo !== null && spanHi !== null && (hz < spanLo || hz > spanHi);

  return (
    <div className="space-y-2">
      {values.length === 0 ? (
        <p className="text-[11px] text-ink-2">None — the tuner hunts whatever its own configuration says.</p>
      ) : (
        <ul className="space-y-1">
          {values.map((hz, index) => (
            <li key={hz} className="flex items-center gap-2 text-xs">
              <span className="w-6 shrink-0 text-ink-2">{index + 1}.</span>
              <span className="tabular-nums text-ink">{mhz(hz)} MHz</span>
              {outside(hz) ? <span className="text-warning-ink">outside the tuner's span</span> : null}
              <button
                type="button"
                aria-label={`Remove ${mhz(hz)} MHz`}
                onClick={() => onChange(values.filter((value) => value !== hz))}
                className="text-ink-2 hover:text-critical-ink"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        <TextInput
          inputMode="decimal"
          aria-label="Control frequency, MHz"
          placeholder="851.025"
          className="h-8 w-28 tabular-nums"
          value={entry}
          onChange={(event) => setEntry(event.target.value)}
        />
        <span className="text-[11px] text-ink-2">MHz</span>
        <Button size="sm" variant="ghost" tip="Add this frequency to the hunt list" onClick={add}>
          Add
        </Button>
        <Button
          size="sm"
          variant="ghost"
          tip={
            hasSite
              ? "Fill the list from the control channels this site is known to use"
              : "Pick a focus site above first — the known channels come from that site"
          }
          disabled={!hasSite || knownLoading || known.length === 0}
          onClick={() => onChange([...new Set([...values, ...known])])}
        >
          Use site's known control channels
        </Button>
        {values.length > 0 ? (
          <Button size="sm" variant="ghost" tip="Stop naming candidates" onClick={() => onChange([])}>
            Clear
          </Button>
        ) : null}
      </div>

      {error ? <p className="text-[11px] text-critical-ink">{error}</p> : null}
      {spanLo !== null && spanHi !== null ? (
        <p className="text-[11px] text-ink-2">
          The tuner's span is {mhz(spanLo)}–{mhz(spanHi)} MHz.
        </p>
      ) : null}
    </div>
  );
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** "845.5–865.5 MHz" from a center and a span — the dialog's subtitle. */
function spanText(centerHz: number | null, spanHz: number | null): string {
  if (centerHz === null || spanHz === null || spanHz <= 0) return "—";
  return `${((centerHz - spanHz / 2) / 1e6).toFixed(1)}–${((centerHz + spanHz / 2) / 1e6).toFixed(1)} MHz`;
}
