import { Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useDeleteIngestSource, useIngestSources, useSaveIngestSource, useTokens } from "../../api/hooks";
import type { IngestSourceInput, IngestSourceKind, IngestSourceRow } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { Card, CardHeader } from "../../components/ui/Card";
import { StatusChip } from "../../components/ui/Chip";
import { Dialog } from "../../components/ui/Dialog";
import { Field, inputClass, TextArea, TextInput } from "../../components/ui/Field";
import { Select } from "../../components/ui/Select";
import { SkeletonText } from "../../components/ui/Skeleton";
import { EmptyState } from "../../components/ui/States";
import { Switch } from "../../components/ui/Switch";
import { Tip } from "../../components/ui/Tooltip";
import { HELP } from "../../help/help";
import { formatRelative } from "../../lib/format";
import { toast } from "../../state/toasts";

const KINDS: { value: IngestSourceKind; label: string }[] = [
  { value: "watch_dir", label: "Watched directory" },
  { value: "push_token", label: "Recorder push (upload token)" },
  { value: "migration", label: "Bulk import" },
];

const KIND_LABEL: Record<IngestSourceKind, string> = {
  watch_dir: "directory",
  push_token: "push",
  migration: "import",
};

/**
 * The dot beside a source.
 *
 * A disabled source is not broken, it is off, so it gets its own state rather than the red
 * one an operator would go looking for a fault behind — the same distinction the backends
 * table draws.
 */
export function sourceState(source: IngestSourceRow): string {
  if (!source.enabled) return "disabled";
  if (source.kind === "watch_dir") return source.path_state === "ok" ? "healthy" : "down";
  if (source.kind === "push_token") return source.token_active === false ? "down" : "healthy";
  return source.meta?.finished ? "healthy" : "running";
}

function sourceTip(source: IngestSourceRow): string {
  if (!source.enabled) return "Switched off; the watcher ignores it";
  if (source.kind === "watch_dir") {
    return source.path_state === "ok"
      ? "Readable by the ingest process"
      : (source.path_detail ?? `The path is ${source.path_state}`);
  }
  if (source.kind === "push_token") {
    return source.token_active === false
      ? "The upload token has been revoked or has expired — this recorder's pushes are being refused"
      : "The upload token is valid";
  }
  return "A bulk import reporting its own progress";
}

/** The one line under a source's name that says what it actually is. */
function detailLine(source: IngestSourceRow): string {
  if (source.kind === "watch_dir") return source.path ?? "—";
  if (source.kind === "push_token") return `token: ${source.token_label ?? source.token_id ?? "—"}`;
  const meta = source.meta ?? {};
  const done = meta.files_done as number | undefined;
  const total = meta.files_total as number | undefined;
  if (done === undefined) return "no progress reported yet";
  return total ? `${done} / ${total} files` : `${done} files`;
}

const EMPTY: IngestSourceInput = {
  kind: "watch_dir",
  label: "",
  enabled: true,
  path: null,
  recursive: true,
  tr_prefer_audio: null,
  tr_sidecar_wait_seconds: null,
  token_id: null,
  notes: null,
};

function draftFrom(source: IngestSourceRow): IngestSourceInput {
  return {
    kind: source.kind,
    label: source.label,
    enabled: source.enabled,
    path: source.path,
    recursive: source.recursive,
    tr_prefer_audio: (source.tr_prefer_audio as "m4a" | "wav" | null) ?? null,
    tr_sidecar_wait_seconds: source.tr_sidecar_wait_seconds,
    token_id: source.token_id,
    notes: source.notes,
  };
}

function SourceDialog({
  open,
  source,
  saving,
  onSave,
  onClose,
}: {
  open: boolean;
  source: IngestSourceRow | null;
  saving: boolean;
  onSave: (draft: IngestSourceInput) => void;
  onClose: () => void;
}) {
  const editing = source !== null;
  const [draft, setDraft] = useState<IngestSourceInput>(EMPTY);
  const tokens = useTokens();

  useEffect(() => {
    if (open) setDraft(source ? draftFrom(source) : EMPTY);
  }, [open, source]);

  const set = <K extends keyof IngestSourceInput>(key: K, value: IngestSourceInput[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const kind = draft.kind;
  const problem = !draft.label.trim()
    ? "A name is required."
    : kind === "watch_dir" && !(draft.path ?? "").trim()
      ? "A directory is required."
      : kind === "push_token" && draft.token_id === null
        ? "Pick the upload token this recorder uses."
        : null;

  const tokenOptions = [
    { value: "", label: "— pick a token —" },
    ...(tokens.data ?? [])
      .filter((token) => token.revoked_at === null)
      .map((token) => ({ value: String(token.id), label: token.label })),
  ];

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !next && onClose()}
      title={editing ? `Edit ${source?.label}` : "New ingest source"}
      description={
        kind === "watch_dir"
          ? "The watcher re-reads its directories on a timer, so this takes effect within about thirty seconds — no restart."
          : "Naming a source does not change how anything is authenticated; it gives the recorder an identity on this page."
      }
      footer={
        <>
          <Button tip="Close without saving" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            tip={problem ?? HELP["admin.ingest.sources.add"]}
            disabled={problem !== null || saving}
            onClick={() => onSave(draft)}
          >
            {saving ? "Saving…" : editing ? "Save changes" : "Add source"}
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name" help="admin.ingest.sources.label">
          {(id) => (
            <TextInput
              id={id}
              value={draft.label}
              placeholder="attic recorder spool"
              onChange={(event) => set("label", event.target.value)}
            />
          )}
        </Field>
        <Field label="Kind" help="admin.ingest.sources.kind">
          {() => (
            <Select
              value={kind}
              onValueChange={(value) => set("kind", value as IngestSourceKind)}
              options={KINDS}
              tip={HELP["admin.ingest.sources.kind"]}
              ariaLabel="Source kind"
              // The kind is the row's identity on the server, so an existing source cannot
              // be turned into a different sort of thing — delete it and add the other.
              disabled={editing}
            />
          )}
        </Field>

        {kind === "watch_dir" ? (
          <>
            <Field
              label="Directory"
              help="admin.ingest.sources.path"
              className="sm:col-span-2"
              hint="Saved even if it does not exist yet; it is shown red until it does."
            >
              {(id) => (
                <TextInput
                  id={id}
                  value={draft.path ?? ""}
                  placeholder="/srv/recorder-2/audio"
                  onChange={(event) => set("path", event.target.value)}
                />
              )}
            </Field>
            <div className="flex items-end pb-1">
              <Switch
                checked={draft.recursive ?? true}
                onCheckedChange={(next) => set("recursive", next)}
                label="Include subdirectories"
                tip={HELP["admin.ingest.sources.recursive"]}
              />
            </div>
            <Field label="trunk-recorder audio" help="admin.ingest.sources.prefer">
              {() => (
                <Select
                  value={draft.tr_prefer_audio ?? ""}
                  onValueChange={(value) => set("tr_prefer_audio", (value || null) as "m4a" | "wav" | null)}
                  options={[
                    { value: "", label: "Follow TR_PREFER_AUDIO" },
                    { value: "m4a", label: "Prefer the M4A" },
                    { value: "wav", label: "Prefer the WAV" },
                  ]}
                  tip={HELP["admin.ingest.sources.prefer"]}
                  ariaLabel="Which trunk-recorder file to keep"
                />
              )}
            </Field>
            <Field
              label="Sidecar wait (s)"
              help="admin.ingest.sources.sidecar"
              hint="Blank follows TR_SIDECAR_WAIT_SECONDS."
            >
              {(id) => (
                <input
                  id={id}
                  type="number"
                  min={0}
                  className={inputClass}
                  value={draft.tr_sidecar_wait_seconds ?? ""}
                  onChange={(event) =>
                    set(
                      "tr_sidecar_wait_seconds",
                      event.target.value === "" ? null : Number(event.target.value),
                    )
                  }
                />
              )}
            </Field>
          </>
        ) : null}

        {kind === "push_token" ? (
          <Field label="Upload token" help="admin.ingest.sources.token" className="sm:col-span-2">
            {() => (
              <Select
                value={draft.token_id === null ? "" : String(draft.token_id)}
                onValueChange={(value) => set("token_id", value === "" ? null : Number(value))}
                options={tokenOptions}
                tip={HELP["admin.ingest.sources.token"]}
                ariaLabel="Upload token"
              />
            )}
          </Field>
        ) : null}

        <div className="flex items-end pb-1">
          <Switch
            checked={draft.enabled ?? true}
            onCheckedChange={(next) => set("enabled", next)}
            label="Enabled"
            tip={HELP["admin.ingest.sources.enabled"]}
          />
        </div>

        <Field label="Notes" help="admin.ingest.sources.notes" className="sm:col-span-2">
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

/**
 * Where recordings come from, as an editable list.
 *
 * `AUDIO_ROOT` used to be the whole answer and could only be changed by editing a file on
 * the host and restarting the watcher. The card exists to make the answer plural and
 * editable — and, just as importantly, to show which of those places is currently broken,
 * because a spool whose mount went away at three in the morning is otherwise silent.
 */
export function IngestSourcesCard({ canWrite }: { canWrite: boolean }) {
  const sources = useIngestSources();
  const save = useSaveIngestSource();
  const remove = useDeleteIngestSource();
  const [editing, setEditing] = useState<{ source: IngestSourceRow | null } | null>(null);
  const [deleting, setDeleting] = useState<IngestSourceRow | null>(null);

  const rows = sources.data?.sources ?? [];

  const saveSource = (draft: IngestSourceInput) => {
    const id = editing?.source?.id;
    save.mutate(
      { id, input: draft },
      {
        onSuccess: () => {
          toast(id ? `${draft.label} saved` : `${draft.label} added`, {
            body:
              draft.kind === "watch_dir"
                ? "The watcher picks this up within about thirty seconds."
                : undefined,
            tone: "success",
          });
          setEditing(null);
        },
        onError: (error) => toast("Could not save the source", { body: String(error), tone: "error" }),
      },
    );
  };

  return (
    <Card>
      <CardHeader
        title="Sources"
        hint={HELP["admin.ingest.sources"]}
        actions={
          <Button
            tip={canWrite ? HELP["admin.ingest.sources.add"] : HELP["app.readOnly"]}
            size="sm"
            disabled={!canWrite}
            onClick={() => setEditing({ source: null })}
          >
            <Plus size={14} aria-hidden="true" /> New source
          </Button>
        }
      />
      {sources.isLoading ? (
        <SkeletonText lines={3} className="p-4" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="No source is configured"
          hint="Ingest is still watching AUDIO_ROOT. Add a source to watch somewhere else as well, or to give a pushing recorder a name."
        />
      ) : (
        <ul className="divide-y divide-line">
          {rows.map((source) => (
            <li key={source.id} className="flex flex-wrap items-center gap-2 px-4 py-2 text-xs">
              <StatusChip status={sourceState(source)} tip={sourceTip(source)} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-ink">{source.label}</span>
                  <span className="text-xs text-muted">{KIND_LABEL[source.kind]}</span>
                </div>
                <div className="truncate font-mono text-[11px] text-ink-2">{detailLine(source)}</div>
                {source.last_error ? (
                  <Tip label={source.last_error}>
                    <span tabIndex={0} className="block truncate text-[11px] text-critical-ink">
                      {source.last_error}
                    </span>
                  </Tip>
                ) : null}
              </div>
              <Tip label="When this source last produced a call">
                <span tabIndex={0} className="text-[11px] text-muted">
                  {formatRelative(source.last_activity_at)}
                </span>
              </Tip>
              <Button
                tip={canWrite ? HELP["admin.ingest.sources.edit"] : HELP["app.readOnly"]}
                size="iconSm"
                variant="ghost"
                disabled={!canWrite}
                aria-label={`Edit ${source.label}`}
                onClick={() => setEditing({ source })}
              >
                <Pencil size={13} aria-hidden="true" />
              </Button>
              <Button
                tip={canWrite ? HELP["admin.ingest.sources.delete"] : HELP["app.readOnly"]}
                size="iconSm"
                variant="ghost"
                disabled={!canWrite}
                aria-label={`Remove ${source.label}`}
                onClick={() => setDeleting(source)}
              >
                <Trash2 size={13} aria-hidden="true" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      <SourceDialog
        open={editing !== null}
        source={editing?.source ?? null}
        saving={save.isPending}
        onSave={saveSource}
        onClose={() => setEditing(null)}
      />

      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Remove ${deleting?.label}?`}
        description="The watcher stops using it on its next reload."
        footer={
          <>
            <Button tip="Keep it" variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              tip={HELP["admin.ingest.sources.delete"]}
              variant="danger"
              onClick={() => {
                if (deleting) {
                  remove.mutate(deleting.id, {
                    onSuccess: () => toast(`${deleting.label} removed`, { tone: "success" }),
                    onError: (error) =>
                      toast("Could not remove the source", { body: String(error), tone: "error" }),
                  });
                }
                setDeleting(null);
              }}
            >
              Remove
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          Nothing already ingested is affected — the calls and the ledger rows belong to the archive, not to
          the source that found them.
        </p>
      </Dialog>
    </Card>
  );
}
