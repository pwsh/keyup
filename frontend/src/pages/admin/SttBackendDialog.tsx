import { useEffect, useState } from "react";
import type { SttBackendConfigInput, SttBackendStat } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field, inputClass, TextArea, TextInput } from "../../components/ui/Field";
import { Select } from "../../components/ui/Select";
import { Switch } from "../../components/ui/Switch";
import { HELP } from "../../help/help";

/** The four adapters `app/stt/` provides, in the order an operator is likely to want them. */
const KINDS = [
  { value: "openai_compatible", label: "OpenAI-compatible server" },
  { value: "ovms", label: "OpenVINO Model Server (OVMS)" },
  { value: "faster_whisper_local", label: "faster-whisper (in process)" },
  { value: "openvino_genai", label: "OpenVINO GenAI (in process)" },
];

/** Which backends talk to a server over HTTP, and therefore need a URL and may need a key. */
const REMOTE = new Set(["openai_compatible", "ovms"]);

/** Which run the model inside the process, and therefore have a device and thread count. */
const LOCAL = new Set(["faster_whisper_local", "openvino_genai"]);

/**
 * The dialog's working copy.
 *
 * `kind` is narrowed to non-optional: the schema makes it optional because it has a
 * server-side default, but a form always has one selected, and leaving it optional would
 * mean writing `draft.kind ?? "openai_compatible"` at every branch that reads it.
 */
export type BackendDraft = SttBackendConfigInput & {
  kind: NonNullable<SttBackendConfigInput["kind"]>;
};

const EMPTY: BackendDraft = {
  name: "",
  kind: "openai_compatible",
  base_url: "",
  api_key: null,
  model: "",
  device: "",
  compute_type: "int8",
  cpu_threads: 4,
  cache_dir: "",
  use_prompt: true,
  use_hotwords: false,
  concurrency: 2,
  priority: 100,
  timeout: 300,
  enabled: true,
  notes: null,
};

/**
 * What an existing row edits from.
 *
 * The key is the one field that cannot be pre-filled — it is write-only, and the server has
 * never sent it — so the draft starts it as `null`, which the API reads as "leave the stored
 * one alone". That is why the field's placeholder says so rather than sitting there empty
 * and implying the backend has no key.
 */
function draftFrom(backend: SttBackendStat): BackendDraft {
  return {
    ...EMPTY,
    name: backend.name,
    kind: (backend.kind ?? "openai_compatible") as BackendDraft["kind"],
    base_url: backend.base_url ?? "",
    api_key: null,
    model: backend.model ?? "",
    device: backend.device ?? "",
    compute_type: backend.compute_type ?? "int8",
    cpu_threads: backend.cpu_threads ?? 4,
    cache_dir: backend.cache_dir ?? "",
    use_prompt: backend.use_prompt ?? true,
    use_hotwords: backend.use_hotwords ?? false,
    concurrency: backend.concurrency,
    priority: backend.priority,
    timeout: backend.timeout ?? 300,
    enabled: backend.config_enabled,
    notes: backend.notes ?? null,
  };
}

/**
 * Add or edit one speech-to-text backend.
 *
 * The fields shown follow the `kind`, because a form that offered a base URL for an
 * in-process model and a compute type for a remote server would be asking the operator to
 * know which of its own inputs to ignore. What is shared stays put so switching kind does
 * not feel like a different dialog.
 */
export function SttBackendDialog({
  open,
  backend,
  existingNames,
  saving,
  onSave,
  onClose,
}: {
  open: boolean;
  /** `null` for "new"; a row to edit otherwise. */
  backend: SttBackendStat | null;
  existingNames: string[];
  saving: boolean;
  onSave: (draft: BackendDraft) => void;
  onClose: () => void;
}) {
  const editing = backend !== null;
  const [draft, setDraft] = useState<BackendDraft>(EMPTY);

  useEffect(() => {
    if (open) setDraft(backend ? draftFrom(backend) : EMPTY);
  }, [open, backend]);

  const set = <K extends keyof BackendDraft>(key: K, value: BackendDraft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const nameTaken = !editing && existingNames.includes(draft.name.trim());
  const problem = !draft.name.trim()
    ? "A name is required."
    : nameTaken
      ? `There is already a backend called ${draft.name.trim()}.`
      : !(draft.model ?? "").trim()
        ? "A model is required."
        : REMOTE.has(draft.kind) && !(draft.base_url ?? "").trim()
          ? "A base URL is required for this kind."
          : null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title={editing ? `Edit ${backend?.name}` : "New speech-to-text backend"}
      description={
        editing
          ? "Saving bumps the configuration version; the transcription worker rebuilds its router within a few seconds. Attempts already running finish on the old one."
          : "The worker picks a new backend up within a few seconds. Nothing needs restarting."
      }
      footer={
        <>
          <Button tip="Close without saving" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            tip={problem ?? HELP["admin.backends.add"]}
            disabled={problem !== null || saving}
            onClick={() => onSave(draft)}
          >
            {saving ? "Saving…" : editing ? "Save changes" : "Add backend"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name" help="admin.backends.health">
          {(id) => (
            <TextInput
              id={id}
              value={draft.name}
              disabled={editing}
              placeholder="whisper"
              onChange={(event) => set("name", event.target.value)}
            />
          )}
        </Field>
        <Field label="Kind" help="admin.backends.kind">
          {/* The trigger is a Radix button, not an input, so it carries its own aria-label
              rather than the Field's id — the same pairing the recorder dialogs use. */}
          {() => (
            <Select
              value={draft.kind}
              onValueChange={(value) => set("kind", value as BackendDraft["kind"])}
              options={KINDS}
              tip={HELP["admin.backends.kind"]}
              ariaLabel="Backend kind"
            />
          )}
        </Field>

        {REMOTE.has(draft.kind) ? (
          <>
            <Field label="Base URL" help="admin.backends.baseurl">
              {(id) => (
                <TextInput
                  id={id}
                  value={draft.base_url ?? ""}
                  placeholder="http://whisper:8000"
                  onChange={(event) => set("base_url", event.target.value)}
                />
              )}
            </Field>
            <Field
              label="API key"
              help="admin.backends.apikey"
              hint={
                editing && backend?.has_api_key
                  ? "A key is stored. Leave blank to keep it."
                  : "Stored, never shown again."
              }
            >
              {(id) => (
                <TextInput
                  id={id}
                  type="password"
                  autoComplete="new-password"
                  value={draft.api_key ?? ""}
                  placeholder={editing && backend?.has_api_key ? "unchanged" : ""}
                  onChange={(event) => set("api_key", event.target.value)}
                />
              )}
            </Field>
          </>
        ) : null}

        <Field label="Model" help="admin.backends.model" className="sm:col-span-2">
          {(id) => (
            <TextInput
              id={id}
              value={draft.model ?? ""}
              placeholder="Systran/faster-whisper-large-v3"
              onChange={(event) => set("model", event.target.value)}
            />
          )}
        </Field>

        {LOCAL.has(draft.kind) ? (
          <>
            <Field label="Device" help="admin.backends.device">
              {(id) => (
                <TextInput
                  id={id}
                  value={draft.device ?? ""}
                  placeholder={draft.kind === "openvino_genai" ? "NPU" : "cpu"}
                  onChange={(event) => set("device", event.target.value)}
                />
              )}
            </Field>
            <Field label="CPU threads" help="admin.backends.cputhreads">
              {(id) => (
                <input
                  id={id}
                  type="number"
                  min={1}
                  className={inputClass}
                  value={draft.cpu_threads ?? 4}
                  onChange={(event) => set("cpu_threads", Number(event.target.value))}
                />
              )}
            </Field>
          </>
        ) : null}

        {draft.kind === "faster_whisper_local" ? (
          <Field label="Compute type" help="admin.backends.computetype">
            {(id) => (
              <TextInput
                id={id}
                value={draft.compute_type ?? ""}
                placeholder="int8"
                onChange={(event) => set("compute_type", event.target.value)}
              />
            )}
          </Field>
        ) : null}

        {draft.kind === "openvino_genai" ? (
          <>
            <Field label="Cache directory" help="admin.backends.cachedir">
              {(id) => (
                <TextInput
                  id={id}
                  value={draft.cache_dir ?? ""}
                  placeholder="~/.cache/keyup/openvino"
                  onChange={(event) => set("cache_dir", event.target.value)}
                />
              )}
            </Field>
            <div className="flex flex-col justify-end gap-2 pb-1">
              <Switch
                checked={draft.use_prompt ?? true}
                onCheckedChange={(next) => set("use_prompt", next)}
                label="Send the domain prompt"
                tip={HELP["admin.backends.useprompt"]}
              />
              <Switch
                checked={draft.use_hotwords ?? false}
                onCheckedChange={(next) => set("use_hotwords", next)}
                label="Send hotwords"
                tip={HELP["admin.backends.usehotwords"]}
              />
            </div>
          </>
        ) : null}

        <Field label="Concurrency" help="admin.backends.concurrency">
          {(id) => (
            <input
              id={id}
              type="number"
              min={1}
              className={inputClass}
              value={draft.concurrency ?? 2}
              onChange={(event) => set("concurrency", Number(event.target.value))}
            />
          )}
        </Field>
        <Field label="Priority" help="admin.backends.priority" hint="Lower is tried first.">
          {(id) => (
            <input
              id={id}
              type="number"
              min={0}
              className={inputClass}
              value={draft.priority ?? 100}
              onChange={(event) => set("priority", Number(event.target.value))}
            />
          )}
        </Field>
        <Field label="Timeout (s)" help="admin.backends.timeout">
          {(id) => (
            <input
              id={id}
              type="number"
              min={1}
              className={inputClass}
              value={draft.timeout ?? 300}
              onChange={(event) => set("timeout", Number(event.target.value))}
            />
          )}
        </Field>
        <div className="flex items-end pb-1">
          <Switch
            checked={draft.enabled ?? true}
            onCheckedChange={(next) => set("enabled", next)}
            label="Enabled"
            tip={HELP["admin.backends.enabled"]}
          />
        </div>

        <Field label="Notes" help="admin.backends.notes" className="sm:col-span-2">
          {(id) => (
            <TextArea
              id={id}
              rows={2}
              value={draft.notes ?? ""}
              onChange={(event) => set("notes", event.target.value)}
            />
          )}
        </Field>
      </div>
      {problem ? <p className="mt-3 text-xs text-critical-ink">{problem}</p> : null}
    </Dialog>
  );
}
