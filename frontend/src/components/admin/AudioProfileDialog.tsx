import { ArrowDown, ArrowUp, AudioLines, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiUrl, errorDetail } from "../../api/client";
import {
  useAudioProfilePreview,
  useAudioStepCatalog,
  useCreateAudioProfile,
  useUpdateAudioProfile,
} from "../../api/hooks";
import type { AudioProfile, AudioProfileStep, AudioProfileStepSpec } from "../../api/types";
import { HELP } from "../../help/help";
import { mutationErrorToast, toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, inputClass, TextArea, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";

/**
 * A step while it is being edited.
 *
 * The chain is an ordered list an operator reorders by hand, and position is therefore the
 * one thing about a step that is *not* stable — two `equalizer` steps are genuinely the
 * same object in two places, so neither the kind nor the index identifies a row. `uid` is
 * the identity React needs to keep an input's cursor where it was across a move; it is
 * assigned here and stripped before anything is sent.
 */
interface EditorStep extends AudioProfileStep {
  uid: number;
}

let nextStepUid = 0;

const toEditorStep = (step: AudioProfileStep): EditorStep => {
  nextStepUid += 1;
  return { uid: nextStepUid, kind: step.kind, params: { ...step.params } };
};

/**
 * The profile editor: a chain of filters, built from the server's own whitelist.
 *
 * Every field below — which kinds exist, what each one takes, the range each number is
 * confined to, the sentence explaining it — comes from `GET /admin/audio/profiles/steps`
 * and none of it is written twice in TypeScript. A new step kind therefore appears in this
 * form by being added to the backend's `STEP_KINDS` table and nowhere else, which is the
 * whole reason that endpoint exists.
 *
 * The other half of the dialog is the preview, and it is the half that makes the numbers
 * mean anything: a 40 dB hole at 1.9 kHz is an abstraction until you have heard the same
 * transmission with and without it, which is what the A/B toggle is for.
 */
export function AudioProfileDialog({
  open,
  onOpenChange,
  profile,
  callId = "",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The profile being edited, or `null` to create one. */
  profile: AudioProfile | null;
  /** Prefill for the preview's call id — the `call` query param a Tune… link carries. */
  callId?: string;
}) {
  const catalog = useAudioStepCatalog(open);
  const create = useCreateAudioProfile();
  const update = useUpdateAudioProfile();
  const preview = useAudioProfilePreview();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [steps, setSteps] = useState<EditorStep[]>([]);
  const [adding, setAdding] = useState("");
  /**
   * What the server said when it refused the chain. Shown next to the steps rather than in
   * a toast: the sentence names the step and the parameter it objects to, so it belongs
   * beside the field it is about and has to stay on screen while that field is corrected.
   */
  const [refusal, setRefusal] = useState<string | null>(null);

  const [previewCall, setPreviewCall] = useState(callId);
  const [rendered, setRendered] = useState<{ url: string; filter: string } | null>(null);
  const [hearing, setHearing] = useState<"original" | "processed">("processed");

  // Each opening starts from the profile as stored, so an abandoned edit does not come
  // back the next time the dialog is opened on a different row.
  useEffect(() => {
    if (!open) return;
    setName(profile?.name ?? "");
    setDescription(profile?.description ?? "");
    setSteps((profile?.chain ?? []).map(toEditorStep));
    setRefusal(null);
    setRendered(null);
    setPreviewCall(callId);
  }, [open, profile, callId]);

  // An object URL holds the blob alive until it is revoked, and the browser will not
  // collect it on its own. Revoked when it is replaced by the next render and again when
  // the dialog goes away, which is what this cleanup covers on both counts.
  useEffect(() => {
    const url = rendered?.url;
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [rendered?.url]);

  const specs = catalog.data?.steps ?? [];
  const maxSteps = catalog.data?.max_steps ?? 12;
  const specByKind = useMemo(
    () => new Map(specs.map((spec: AudioProfileStepSpec) => [spec.kind, spec])),
    [specs],
  );

  const setStep = (index: number, next: AudioProfileStep) =>
    setSteps((current) =>
      current.map((step, at) => (at === index ? { ...step, params: next.params } : step)),
    );

  const move = (index: number, by: -1 | 1) =>
    setSteps((current) => {
      const next = [...current];
      const target = index + by;
      if (target < 0 || target >= next.length) return current;
      [next[index], next[target]] = [next[target] as EditorStep, next[index] as EditorStep];
      return next;
    });

  const addStep = () => {
    const spec = specByKind.get(adding || (specs[0]?.kind ?? ""));
    if (!spec) return;
    // A new step arrives with the whitelist's defaults already in it, so the chain it
    // makes is one that renders — the operator adjusts numbers rather than inventing them.
    const params: Record<string, number> = {};
    for (const param of spec.params) {
      if (param.default !== null) params[param.name] = param.default;
    }
    setSteps((current) => [...current, toEditorStep({ kind: spec.kind, params })]);
    setRefusal(null);
  };

  const save = () => {
    setRefusal(null);
    const chain = steps.map((step) => ({ kind: step.kind, params: step.params ?? {} }));
    const onError = (error: unknown) => {
      // 422 is the chain being refused and 409 the name being taken; both name the thing
      // to fix, so they are shown in the form. Anything else is a toast.
      const status = (error as { status?: number }).status;
      if (status === 422 || status === 409) setRefusal(errorDetail(error) ?? "The server refused this.");
      else mutationErrorToast(error, "Could not save the profile");
    };
    const onSuccess = () => {
      toast(profile ? "Profile saved" : "Profile created", { tone: "success" });
      onOpenChange(false);
    };
    if (profile) {
      update.mutate(
        { id: profile.id, patch: { name: name.trim(), description: description || null, chain } },
        { onSuccess, onError },
      );
    } else {
      create.mutate({ name: name.trim(), description: description || null, chain }, { onSuccess, onError });
    }
  };

  const render = () => {
    setRefusal(null);
    preview.mutate(
      {
        call_id: Number(previewCall),
        chain: steps.map((step) => ({ kind: step.kind, params: step.params ?? {} })),
        // Long enough to judge a chain on, short enough that a mis-typed call id costs a
        // second rather than a minute of ffmpeg. The server caps it at 60 either way.
        seconds: 30,
      },
      {
        onSuccess: (result) => {
          setRendered({ url: URL.createObjectURL(result.blob), filter: result.filter });
          setHearing("processed");
        },
        onError: (error) => {
          const status = (error as { status?: number }).status;
          if (status === 422) setRefusal(errorDetail(error) ?? "The server refused this chain.");
          else mutationErrorToast(error, "Could not render that call");
        },
      },
    );
  };

  const pending = create.isPending || update.isPending;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={profile ? `Edit ${profile.name}` : "New audio profile"}
      description="An ordered chain of filters. It replaces the denoiser and gate of standard processing for the channels assigned to it; the trim and the loudness tail are measured per call and stay as they are."
      className="w-[min(46rem,calc(100vw-2rem))]"
      footer={
        <>
          <Button tip={HELP["admin.audio.profiles.cancel"]} size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["admin.audio.profiles.save"]}
            size="sm"
            variant="primary"
            disabled={pending || name.trim() === ""}
            onClick={save}
          >
            {profile ? "Save" : "Create"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name" help="admin.audio.profiles.name">
            {(id) => (
              <TextInput
                id={id}
                value={name}
                placeholder="Aircraft cabin"
                onChange={(event) => setName(event.target.value)}
              />
            )}
          </Field>
          <Field label="Description" help="admin.audio.profiles.description">
            {(id) => (
              <TextArea
                id={id}
                rows={2}
                value={description}
                placeholder="What this chain is for, and on which channels."
                onChange={(event) => setDescription(event.target.value)}
              />
            )}
          </Field>
        </div>

        {/* ------------------------------------------------------------- the chain */}

        <section className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-xs font-medium text-ink-2">Steps</h3>
            <span className="text-[11px] tabular-nums text-muted">
              {steps.length} of {maxSteps}
            </span>
            <div className="ml-auto flex items-center gap-2">
              <Select
                value={adding || (specs[0]?.kind ?? "")}
                onValueChange={setAdding}
                options={specs.map((spec) => ({ value: spec.kind, label: spec.label }))}
                tip={HELP["admin.audio.profiles.addStep"]}
                ariaLabel="Step to add"
                size="sm"
                className="w-48"
                disabled={specs.length === 0}
              />
              <Button
                tip={
                  steps.length >= maxSteps
                    ? `A chain may hold at most ${maxSteps} steps`
                    : HELP["admin.audio.profiles.addStep"]
                }
                size="sm"
                variant="secondary"
                disabled={specs.length === 0 || steps.length >= maxSteps}
                onClick={addStep}
              >
                <Plus size={13} aria-hidden="true" /> Add step
              </Button>
            </div>
          </div>

          {steps.length === 0 ? (
            <p className="rounded border border-dashed border-line p-3 text-xs text-muted">
              No steps yet. An empty chain is legal — it renders the loudness tail alone — but it is not what
              a profile is for.
            </p>
          ) : null}

          {steps.map((step, index) => {
            const spec = specByKind.get(step.kind);
            return (
              <StepRow
                key={step.uid}
                index={index}
                count={steps.length}
                step={step}
                spec={spec}
                onChange={(next) => setStep(index, next)}
                onMove={(by) => move(index, by)}
                onRemove={() => setSteps((current) => current.filter((_, at) => at !== index))}
              />
            );
          })}

          {refusal ? (
            <p role="alert" className="rounded border border-critical px-3 py-2 text-xs text-critical-ink">
              {refusal}
            </p>
          ) : null}

          <div>
            <p className="mb-1 text-xs text-muted">{rendered ? "Rendered chain (as played)" : "Chain"}</p>
            <pre className="whitespace-pre-wrap break-all rounded bg-surface-2 p-2 font-mono text-[10px] leading-relaxed text-ink-2">
              {/* The server's own string once anything has been rendered or saved through
                  it; before that, the steps as they stand, which is a summary rather than a
                  filter graph and is labelled as one. */}
              {rendered?.filter || profile?.rendered || describeChain(steps)}
            </pre>
          </div>
        </section>

        {/* ----------------------------------------------------------- the preview */}

        <section className="space-y-3 border-t border-line pt-4">
          <div className="flex flex-wrap items-end gap-2">
            <div>
              <label htmlFor="profile-preview-call" className="mb-1 block text-xs font-medium text-ink-2">
                Call id
              </label>
              <input
                id="profile-preview-call"
                type="number"
                inputMode="numeric"
                value={previewCall}
                placeholder="123456"
                onChange={(event) => setPreviewCall(event.target.value)}
                className={`${inputClass} w-40`}
              />
            </div>
            <Button
              tip={HELP["admin.audio.profiles.preview"]}
              size="sm"
              variant="secondary"
              disabled={!previewCall || preview.isPending}
              onClick={render}
            >
              <AudioLines size={13} aria-hidden="true" /> Render
            </Button>
            <p className="text-[11px] text-muted">
              Renders the first 30 seconds over the call's original audio. Nothing is stored.
            </p>
          </div>

          {rendered ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-medium text-ink-2">Listening to</span>
                <Button
                  tip={HELP["admin.audio.profiles.ab"]}
                  size="sm"
                  variant="secondary"
                  active={hearing === "original"}
                  onClick={() => setHearing("original")}
                >
                  Original
                </Button>
                <Button
                  tip={HELP["admin.audio.profiles.ab"]}
                  size="sm"
                  variant="secondary"
                  active={hearing === "processed"}
                  onClick={() => setHearing("processed")}
                >
                  With this chain
                </Button>
              </div>
              {/* Both players stay mounted and one is hidden, rather than one player whose
                  `src` is swapped: swapping the source restarts the clip, and the whole
                  point of an A/B is to hear the same second of audio both ways. */}
              <div hidden={hearing !== "original"}>
                {/* biome-ignore lint/a11y/useMediaCaption: scanner audio carries no caption track */}
                <audio
                  controls
                  preload="none"
                  src={apiUrl(`/calls/${Number(previewCall)}/audio?raw=1`)}
                  aria-label="Original"
                  className="h-8 w-full"
                />
              </div>
              <div hidden={hearing !== "processed"}>
                {/* biome-ignore lint/a11y/useMediaCaption: scanner audio carries no caption track */}
                <audio
                  controls
                  preload="none"
                  src={rendered.url}
                  aria-label="With this chain"
                  className="h-8 w-full"
                />
              </div>
            </>
          ) : null}
        </section>
      </div>
    </Dialog>
  );
}

/**
 * The steps as a line of prose, for a chain that has not been near the server yet.
 *
 * Deliberately not an ffmpeg string: rendering one here would mean writing the option
 * names, the fixed suffixes and `agate`'s dB conversion a second time, and a second
 * renderer is a second answer to "what will actually run". The real string arrives with
 * the first preview and replaces this.
 */
function describeChain(steps: AudioProfileStep[]): string {
  if (steps.length === 0) return "(empty — the loudness tail alone)";
  return steps
    .map((step) => {
      const params = Object.entries(step.params ?? {})
        .map(([key, value]) => `${key}=${value}`)
        .join(" ");
      return params ? `${step.kind} ${params}` : step.kind;
    })
    .join("  →  ");
}

/** One filter in the chain: its explanation, its numbers, and where it sits in the order. */
function StepRow({
  index,
  count,
  step,
  spec,
  onChange,
  onMove,
  onRemove,
}: {
  index: number;
  count: number;
  step: AudioProfileStep;
  spec: AudioProfileStepSpec | undefined;
  onChange: (step: AudioProfileStep) => void;
  onMove: (by: -1 | 1) => void;
  onRemove: () => void;
}) {
  const label = spec?.label ?? step.kind;
  const setParam = (paramName: string, raw: string) => {
    const params = { ...(step.params ?? {}) };
    // A cleared box is not zero. For a parameter with a default the server fills it back
    // in; for one without, the filter simply does not get the option — which is the state
    // `afftdn` with no `nr` is in, and there is no other way to express it.
    if (raw.trim() === "") delete params[paramName];
    else params[paramName] = Number(raw);
    onChange({ kind: step.kind, params });
  };

  return (
    <div className="border-t border-line pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] tabular-nums text-muted">{index + 1}</span>
        <span className="text-sm font-medium text-ink">{label}</span>
        <code className="font-mono text-[10px] text-muted">{step.kind}</code>
        <div className="ml-auto flex items-center gap-1">
          <Button
            tip={HELP["admin.audio.profiles.stepUp"]}
            size="iconSm"
            variant="ghost"
            disabled={index === 0}
            aria-label={`Move ${label} earlier`}
            onClick={() => onMove(-1)}
          >
            <ArrowUp size={13} aria-hidden="true" />
          </Button>
          <Button
            tip={HELP["admin.audio.profiles.stepDown"]}
            size="iconSm"
            variant="ghost"
            disabled={index === count - 1}
            aria-label={`Move ${label} later`}
            onClick={() => onMove(1)}
          >
            <ArrowDown size={13} aria-hidden="true" />
          </Button>
          <Button
            tip={HELP["admin.audio.profiles.removeStep"]}
            size="iconSm"
            variant="ghost"
            aria-label={`Remove ${label}`}
            onClick={onRemove}
          >
            <Trash2 size={13} aria-hidden="true" />
          </Button>
        </div>
      </div>

      {spec ? <p className="mt-1 text-[11px] leading-relaxed text-ink-2">{spec.help}</p> : null}

      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        {(spec?.params ?? []).map((param) => (
          <div key={param.name} className="min-w-0">
            <label
              htmlFor={`step-${index}-${param.name}`}
              className="mb-1 block text-[11px] font-medium text-ink-2"
            >
              {param.name}
              {param.unit ? <span className="ml-1 text-muted">({param.unit})</span> : null}
            </label>
            <input
              id={`step-${index}-${param.name}`}
              type="number"
              inputMode="decimal"
              // The bounds are the server's, read off the whitelist — the browser refuses
              // what the API would refuse, and the two cannot drift apart.
              min={param.low}
              max={param.high}
              aria-label={`${label} ${param.name}`}
              value={step.params?.[param.name] ?? ""}
              // A parameter with no default is left off the filter when it is blank, so
              // "off" is the honest placeholder rather than a number nobody chose.
              placeholder={param.default === null ? "off" : String(param.default)}
              onChange={(event) => setParam(param.name, event.target.value)}
              className={`${inputClass} h-8`}
            />
            <p className="mt-0.5 text-[10px] leading-snug text-muted">
              {param.help}
              {param.optional ? " — optional" : null}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
