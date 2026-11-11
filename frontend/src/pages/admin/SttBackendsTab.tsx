import { Activity, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../../api/client";
import {
  useCurrentUser,
  useDeleteBackend,
  useProbeBackend,
  useSaveBackend,
  useSetBackendEnabled,
  useSttBackends,
} from "../../api/hooks";
import type { SttBackendProbe, SttBackendStat, SttBackendsView, SttQueue } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { Card, CardHeader } from "../../components/ui/Card";
import { StatusChip } from "../../components/ui/Chip";
import { Dialog } from "../../components/ui/Dialog";
import { SkeletonText } from "../../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../../components/ui/States";
import { Switch } from "../../components/ui/Switch";
import { Tip } from "../../components/ui/Tooltip";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import { formatLag, formatNumber, formatRelative } from "../../lib/format";
import { toast } from "../../state/toasts";
import { type BackendDraft, SttBackendDialog } from "./SttBackendDialog";

/** How many consecutive errors before a backend that is still answering is called degraded. */
const DEGRADED_AFTER = 3;

/** The last-error cell is one line; the whole message is in the tooltip. */
const ERROR_CHARS = 60;

export type BackendHealthState = "healthy" | "degraded" | "down" | "disabled" | "stale";

/**
 * What the dot means, in the order the operator needs to hear it.
 *
 * "Stale" comes first on purpose: if nobody is publishing, every other fact in the row is
 * old, and a green dot on a dead worker is the exact lie this page was rebuilt to stop
 * telling.  A backend that is switched off is not unhealthy — it is off — so it gets its
 * own state rather than a red dot nobody can act on.
 */
export function healthState(backend: SttBackendStat): BackendHealthState {
  if (backend.stale) return "stale";
  if (!backend.enabled) return "disabled";
  if (backend.healthy === false) return "down";
  if (backend.consecutive_errors >= DEGRADED_AFTER) return "degraded";
  return "healthy";
}

function healthTip(backend: SttBackendStat): string {
  switch (healthState(backend)) {
    case "stale":
      return `The worker has not reported this backend since ${formatRelative(backend.updated_at)}`;
    case "disabled":
      return backend.config_enabled
        ? "Switched off by an operator; the configuration has it enabled"
        : "Disabled in STT_BACKENDS";
    case "down":
      return backend.health_detail || "The last health check failed";
    case "degraded":
      return `${backend.consecutive_errors} attempts in a row have failed`;
    default:
      return backend.health_detail || "Answering its health check";
  }
}

function truncate(value: string | null, chars = ERROR_CHARS): string {
  if (!value) return "—";
  return value.length > chars ? `${value.slice(0, chars)}…` : value;
}

/** A reader hitting the admin-only endpoint gets an explanation, not a red error box. */
function needsAdmin(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

/** The header's one-line answer to "is anything publishing these numbers, and since when". */
function workerLine(view: SttBackendsView): string {
  if (view.reporting) {
    return `worker ${view.worker_instance ?? "?"}, up ${formatRelative(view.worker_started_at)}`;
  }
  return view.last_report_at
    ? `worker not reporting since ${formatRelative(view.last_report_at)}`
    : "worker has never reported";
}

/* ------------------------------------------------------------------- queue summary */

/** The queue as SQL sees it — the one part of this page that survives a worker restart. */
export function QueueStrip({ queue, reporting }: { queue: SttQueue; reporting: boolean }) {
  const cells: { label: string; value: string; tip: string }[] = [
    {
      label: "queued (live)",
      value: formatNumber(queue.queued_live),
      tip: HELP["admin.backends.queue.live"],
    },
    {
      label: "queued (backlog)",
      value: formatNumber(queue.queued_backlog),
      tip: HELP["admin.backends.queue.backlog"],
    },
    { label: "running", value: formatNumber(queue.running), tip: HELP["admin.backends.queue.running"] },
    {
      label: "done (1h)",
      value: formatNumber(queue.completed_1h),
      tip: HELP["admin.backends.queue.completed"],
    },
    {
      label: "failed (1h)",
      value: formatNumber(queue.failed_1h),
      tip: HELP["admin.backends.queue.failed"],
    },
    {
      label: "mean job",
      value: queue.mean_processing_s === null ? "—" : formatLag(queue.mean_processing_s),
      tip: HELP["admin.backends.queue.mean"],
    },
    {
      label: "per minute",
      value: queue.throughput_per_min.toFixed(1),
      tip: HELP["admin.backends.queue.throughput"],
    },
    {
      label: "ETA",
      value: queue.eta_seconds === null ? "—" : formatLag(queue.eta_seconds),
      tip: HELP["admin.backends.queue.eta"],
    },
  ];
  return (
    <div className="border-b border-line px-4 py-3">
      <dl className="flex flex-wrap gap-x-6 gap-y-3">
        {cells.map((cell) => (
          <Tip key={cell.label} label={cell.tip}>
            <div tabIndex={0} className="min-w-[5.5rem]">
              <dt className="text-xs text-muted">{cell.label}</dt>
              <dd className="text-sm font-medium tabular-nums text-ink">{cell.value}</dd>
            </div>
          </Tip>
        ))}
      </dl>
      {!reporting ? (
        <p className="mt-3 text-xs text-warning-ink">
          The queue figures above are counted from the database and are current. The per-backend counters
          below come from the transcription worker, which is not reporting.
        </p>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------- backend rows */

function BackendRow({
  backend,
  canSwitch,
  onToggle,
  onEdit,
  onDelete,
  onProbe,
  probing,
}: {
  backend: SttBackendStat;
  canSwitch: boolean;
  onToggle: (enabled: boolean | null) => void;
  onEdit: () => void;
  onDelete: () => void;
  onProbe: () => void;
  probing: boolean;
}) {
  const state = healthState(backend);
  const overridden = backend.admin_enabled !== null;
  return (
    <tr className="border-t border-line align-top">
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          <StatusChip status={state} tip={healthTip(backend)} />
          <span className="font-medium text-ink">{backend.name}</span>
        </div>
        <div className="mt-0.5 text-[11px] text-ink-2">
          {backend.kind ?? "—"} · {backend.model ?? "—"}
        </div>
        {backend.disqualified ? (
          <div className="mt-0.5 text-[11px] text-critical-ink">
            disqualified: {truncate(backend.disqualified)}
          </div>
        ) : null}
      </td>
      <td className="px-3 py-2">
        <div className="flex flex-col items-start gap-1">
          <Switch
            checked={backend.enabled}
            disabled={!canSwitch}
            onCheckedChange={(next) => onToggle(next)}
            label=""
            tip={
              canSwitch
                ? HELP["admin.backends.toggle"]
                : "Enabling or disabling a backend needs an admin session"
            }
          />
          <span className="text-[11px] text-ink-2">
            {overridden
              ? `by operator (config: ${backend.config_enabled ? "on" : "off"})`
              : backend.config_enabled
                ? "from config"
                : "disabled by config"}
          </span>
          {overridden && canSwitch ? (
            <Button
              tip={HELP["admin.backends.reset"]}
              size="sm"
              variant="ghost"
              className="whitespace-nowrap"
              onClick={() => onToggle(null)}
            >
              Reset to config
            </Button>
          ) : null}
        </div>
      </td>
      <td className="px-3 py-2 tabular-nums">
        {backend.in_flight} / {backend.concurrency}
      </td>
      <td className="px-3 py-2 tabular-nums">{formatNumber(backend.successes)}</td>
      <td className="px-3 py-2 tabular-nums">{formatNumber(backend.errors)}</td>
      <td className="px-3 py-2 tabular-nums">
        {backend.mean_processing_s === null ? "—" : formatLag(backend.mean_processing_s)}
      </td>
      <td className="px-3 py-2 tabular-nums">
        {backend.mean_rtf === null ? "—" : `${backend.mean_rtf.toFixed(2)}×`}
      </td>
      <td className="px-3 py-2 tabular-nums">
        {backend.recent_completed_1m} / {backend.recent_failed_1m}
      </td>
      <td className="px-3 py-2">{formatRelative(backend.last_success_at)}</td>
      <td className="px-3 py-2 max-w-[16rem]">
        {backend.last_error ? (
          <Tip label={backend.last_error}>
            <span tabIndex={0} className="block truncate text-[11px] text-critical-ink">
              {truncate(backend.last_error)}
            </span>
          </Tip>
        ) : (
          <span className="text-[11px] text-muted">—</span>
        )}
        {backend.last_error_at ? (
          <div className="text-[11px] text-muted">{formatRelative(backend.last_error_at)}</div>
        ) : null}
      </td>
      <td className="px-3 py-2 text-[11px] text-ink-2">
        {backend.stale ? "not reporting" : formatRelative(backend.started_at)}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-1">
          <Button
            tip={HELP["admin.backends.probe"]}
            size="iconSm"
            variant="ghost"
            disabled={!canSwitch || probing}
            aria-label={`Probe ${backend.name}`}
            onClick={onProbe}
          >
            <Activity size={13} aria-hidden="true" />
          </Button>
          <Button
            tip={
              backend.editable
                ? HELP["admin.backends.edit"]
                : "This backend comes from STT_BACKENDS. Adding or editing any backend copies them into the database first, after which it can be edited here."
            }
            size="iconSm"
            variant="ghost"
            disabled={!canSwitch || !backend.editable}
            aria-label={`Edit ${backend.name}`}
            onClick={onEdit}
          >
            <Pencil size={13} aria-hidden="true" />
          </Button>
          <Button
            tip={HELP["admin.backends.delete"]}
            size="iconSm"
            variant="ghost"
            disabled={!canSwitch || !backend.editable}
            aria-label={`Delete ${backend.name}`}
            onClick={onDelete}
          >
            <Trash2 size={13} aria-hidden="true" />
          </Button>
        </div>
      </td>
    </tr>
  );
}

/** What a probe came back with, in the words the operator needs and no more. */
function ProbeResult({ probe }: { probe: SttBackendProbe }) {
  return (
    <div
      className={cn("border-b border-line px-4 py-3 text-xs", probe.ok ? "bg-surface-2" : "bg-critical-bg")}
      role="status"
    >
      <p className={probe.ok ? "text-ink" : "text-critical-ink"}>
        <span className="font-medium">{probe.name}</span> {probe.ok ? "answered" : "did not answer"}
        {probe.detail ? ` — ${probe.detail}` : null}
      </p>
      {probe.rtf !== null ? (
        <p className="mt-1 text-ink-2">
          Transcribed {probe.audio_s}s of call {probe.clip_call_id} in {probe.latency_s}s —{" "}
          <span className="font-medium text-ink">{probe.rtf}× real time</span>
          {probe.model ? `, model ${probe.model}` : null}
        </p>
      ) : probe.benchmark_skipped ? (
        <p className="mt-1 text-ink-2">No clip was timed: {probe.benchmark_skipped}.</p>
      ) : null}
      {probe.text ? <p className="mt-1 italic text-ink-2">“{probe.text}”</p> : null}
    </div>
  );
}

/** "Saved, and the worker has it" — or "saved, the worker is a few seconds behind". */
function VersionLine({ view }: { view: SttBackendsView }) {
  if (view.from_environment) {
    return (
      <span className="text-[11px] text-ink-2">
        from STT_BACKENDS — adding or editing a backend copies them into the database
      </span>
    );
  }
  const applied = view.applied_config_version;
  const caught = applied !== null && applied >= view.config_version;
  return (
    <Tip label={HELP["admin.backends.version"]}>
      <span tabIndex={0} className={cn("text-[11px]", caught ? "text-ink-2" : "text-warning-ink")}>
        {caught
          ? `config v${view.config_version}, applied`
          : `config v${view.config_version} — the worker is on v${applied ?? "?"}`}
      </span>
    </Tip>
  );
}

/** `tip: null` is a column with no visible heading — read out, not shown, and no tab stop. */
const COLUMNS: { label: string; tip: string | null }[] = [
  { label: "Backend", tip: HELP["admin.backends.health"] },
  { label: "Enabled", tip: HELP["admin.backends.toggle"] },
  { label: "In flight", tip: HELP["admin.backends.inflight"] },
  { label: "Completed", tip: HELP["admin.backends.completed"] },
  { label: "Failed", tip: HELP["admin.backends.failed"] },
  { label: "Mean job", tip: HELP["admin.backends.mean"] },
  { label: "RTF", tip: HELP["admin.backends.rtf"] },
  { label: "1m ok/err", tip: HELP["admin.backends.recent"] },
  { label: "Last success", tip: HELP["admin.backends.lastsuccess"] },
  { label: "Last error", tip: HELP["admin.backends.lasterror"] },
  { label: "Worker up", tip: HELP["admin.backends.uptime"] },
  // The row's buttons carry their own tooltips (admin.backends.edit among them).
  { label: "Actions", tip: null },
];

/**
 * Speech-to-text backends: what the worker is doing, and the switches for it.
 *
 * Everything on this page is read from the database rather than from this process, because
 * the transcription counters live in the *worker's* memory — the page used to build its own
 * router and report its zeros, which made a busy deployment look idle.
 */
export function BackendsTab() {
  const view = useSttBackends();
  const { isAdmin } = useCurrentUser();
  const setEnabled = useSetBackendEnabled();
  const save = useSaveBackend();
  const remove = useDeleteBackend();
  const probe = useProbeBackend();
  const [confirming, setConfirming] = useState<SttBackendStat | null>(null);
  const [editing, setEditing] = useState<{ backend: SttBackendStat | null } | null>(null);
  const [deleting, setDeleting] = useState<SttBackendStat | null>(null);
  const [probeResult, setProbeResult] = useState<SttBackendProbe | null>(null);

  const backends = view.data?.backends ?? [];
  const enabledCount = backends.filter((backend) => backend.enabled).length;

  const apply = (backend: SttBackendStat, enabled: boolean | null) => {
    setEnabled.mutate(
      { name: backend.name, enabled },
      {
        onSuccess: () =>
          toast(
            enabled === null
              ? `${backend.name} follows the configuration again`
              : `${backend.name} ${enabled ? "enabled" : "disabled"}`,
            { tone: "success" },
          ),
        onError: (error) => toast(`Could not change ${backend.name}`, { body: String(error), tone: "error" }),
      },
    );
  };

  const toggle = (backend: SttBackendStat, enabled: boolean | null) => {
    // Turning off the last one stops transcription outright, which is a thing to be told
    // before it happens rather than discovered from a queue that stopped moving.
    const wouldStop = enabled === false || (enabled === null && !backend.config_enabled);
    if (wouldStop && backend.enabled && enabledCount === 1) {
      setConfirming(backend);
      return;
    }
    apply(backend, enabled);
  };

  const saveBackend = (draft: BackendDraft) => {
    const name = editing?.backend?.name;
    save.mutate(
      { name, input: draft },
      {
        onSuccess: () => {
          toast(name ? `${name} saved` : `${draft.name} added`, {
            body: "The transcription worker picks this up within a few seconds.",
            tone: "success",
          });
          setEditing(null);
        },
        onError: (error) => toast("Could not save the backend", { body: String(error), tone: "error" }),
      },
    );
  };

  const deleteBackend = (backend: SttBackendStat) => {
    remove.mutate(backend.name, {
      onSuccess: (result) =>
        toast(result.retained ? `${backend.name} disabled and kept` : `${backend.name} removed`, {
          body: result.retained
            ? `${result.transcripts} transcript(s) name it, so the row stays and the history is still answerable.`
            : undefined,
          tone: "success",
        }),
      onError: (error) => toast(`Could not remove ${backend.name}`, { body: String(error), tone: "error" }),
    });
    setDeleting(null);
  };

  const runProbe = (backend: SttBackendStat) => {
    setProbeResult(null);
    probe.mutate(backend.name, {
      onSuccess: (result) => setProbeResult(result),
      onError: (error) => toast(`Could not probe ${backend.name}`, { body: String(error), tone: "error" }),
    });
  };

  return (
    <Card>
      <CardHeader
        title="Speech-to-text backends"
        hint={HELP["admin.tab.backends"]}
        actions={
          view.data ? (
            <div className="flex items-center gap-3">
              <span className="text-[11px] text-ink-2">{workerLine(view.data)}</span>
              <VersionLine view={view.data} />
              <Button
                tip={isAdmin ? HELP["admin.backends.add"] : "Adding a backend needs an admin session"}
                size="sm"
                disabled={!isAdmin}
                onClick={() => setEditing({ backend: null })}
              >
                <Plus size={14} aria-hidden="true" /> New backend
              </Button>
            </div>
          ) : null
        }
      />
      {view.isLoading ? (
        <SkeletonText lines={5} className="p-4" />
      ) : needsAdmin(view.error) ? (
        // The same rows carry the operator's switches, so the API asks for `admin` — say so
        // rather than showing an error a reader can do nothing about.
        <EmptyState
          title="Backend health needs an admin session"
          hint="The per-backend counters and the switches that go with them are administrator territory. Sign in to see them."
        />
      ) : view.isError || !view.data ? (
        <ErrorState error={view.error} onRetry={() => view.refetch()} />
      ) : (
        <>
          <QueueStrip queue={view.data.queue} reporting={view.data.reporting} />
          {probeResult ? <ProbeResult probe={probeResult} /> : null}
          {backends.length === 0 ? (
            <EmptyState
              title="No speech-to-text backend is configured"
              hint="STT_BACKENDS is empty, so nothing will be transcribed."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-xs text-muted">
                  <tr>
                    {COLUMNS.map((column) => (
                      <th key={column.label} scope="col" className="px-3 py-2 font-medium">
                        {column.tip === null ? (
                          <span className="sr-only">{column.label}</span>
                        ) : (
                          <Tip label={column.tip}>
                            <span tabIndex={0}>{column.label}</span>
                          </Tip>
                        )}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {backends.map((backend) => (
                    <BackendRow
                      key={backend.name}
                      backend={backend}
                      canSwitch={isAdmin}
                      onToggle={(enabled) => toggle(backend, enabled)}
                      onEdit={() => setEditing({ backend })}
                      onDelete={() => setDeleting(backend)}
                      onProbe={() => runProbe(backend)}
                      probing={probe.isPending}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      <Dialog
        open={confirming !== null}
        onOpenChange={(open) => !open && setConfirming(null)}
        title="Disable the last enabled backend?"
        description="Transcription will pause until a backend is re-enabled. Jobs wait in the queue — nothing is lost or failed."
        footer={
          <>
            <Button tip="Leave it enabled" variant="ghost" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button
              tip="Disable it and pause transcription"
              variant="danger"
              onClick={() => {
                if (confirming) apply(confirming, false);
                setConfirming(null);
              }}
            >
              Disable anyway
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {confirming?.name} is the only backend still enabled. Queued calls will keep piling up until you
          switch one back on.
        </p>
      </Dialog>

      <SttBackendDialog
        open={editing !== null}
        backend={editing?.backend ?? null}
        existingNames={backends.map((item) => item.name)}
        saving={save.isPending}
        onSave={saveBackend}
        onClose={() => setEditing(null)}
      />

      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Remove ${deleting?.name}?`}
        description="It stops being routed to. Nothing already transcribed is affected."
        footer={
          <>
            <Button tip="Keep it" variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              tip={HELP["admin.backends.delete"]}
              variant="danger"
              onClick={() => deleting && deleteBackend(deleting)}
            >
              Remove
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          If any transcript in the archive names {deleting?.name}, the row is kept and disabled rather than
          deleted, so “which engine produced this transcript?” stays answerable. Its counters are left alone
          either way.
        </p>
      </Dialog>
    </Card>
  );
}
