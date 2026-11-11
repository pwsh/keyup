import { Copy, Headphones, KeyRound, Play, Plus, RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import {
  useAdminIngest,
  useCreateToken,
  useCurrentUser,
  useDeleteToken,
  useIngestFiles,
  useJobs,
  useRequeueJobs,
  useRescan,
  useRetentionPolicies,
  useRetryIngestFile,
  useRevokeToken,
  useRunRetention,
  useSaveRetentionPolicy,
  useSaveUser,
  useSystemStatus,
  useTokens,
  useUsers,
} from "../api/hooks";
import { type ApiTokenInfo, TOKEN_SCOPES, type TokenScope, type User } from "../api/types";
import { PageHeader } from "../components/layout/PageHeader";
import { RecorderTab } from "../components/recorders/RecorderTab";
import { LiveActivity } from "../components/status/LiveActivity";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Chip, StatusChip } from "../components/ui/Chip";
import { Dialog } from "../components/ui/Dialog";
import { Field, TextInput } from "../components/ui/Field";
import { Select } from "../components/ui/Select";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { Tip } from "../components/ui/Tooltip";
import { HELP, useHelpText } from "../help/help";
import { copyAndToast } from "../lib/clipboard";
import { cn } from "../lib/cn";
import { formatBytes, formatDateTime, formatLag, formatNumber, formatRelative } from "../lib/format";
import { mutationErrorToast, toast } from "../state/toasts";
import { AgenciesTab } from "./admin/AgenciesTab";
import { AudioTab } from "./admin/AudioTab";
import { ExportsTab } from "./admin/ExportsTab";
import { HttpsTab } from "./admin/HttpsTab";
import { IngestSourcesCard } from "./admin/IngestSourcesCard";
import { SettingsTab } from "./admin/SettingsTab";
import { BackendsTab } from "./admin/SttBackendsTab";

const TABS = [
  { id: "ingest", label: "Ingest", tip: HELP["admin.tab.ingest"] },
  { id: "transcription", label: "Transcription", tip: HELP["admin.tab.transcription"] },
  { id: "backends", label: "Backends", tip: HELP["admin.tab.backends"] },
  { id: "recorder", label: "Recorder", tip: HELP["admin.tab.recorder"] },
  { id: "audio", label: "Audio", tip: HELP["admin.tab.audio"] },
  { id: "agencies", label: "Agencies", tip: HELP["admin.tab.agencies"] },
  { id: "retention", label: "Retention", tip: HELP["admin.tab.retention"] },
  { id: "settings", label: "Settings", tip: HELP["admin.tab.settings"] },
  { id: "https", label: "HTTPS", tip: HELP["admin.tab.https"] },
  { id: "exports", label: "Exports", tip: HELP["admin.tab.exports"] },
  { id: "tokens", label: "Tokens", tip: HELP["admin.tab.tokens"] },
  { id: "users", label: "Users", tip: HELP["admin.tab.users"] },
];

/** Operating the system. Every tab is one area of the ARCHITECTURE §4.7 surface. */
export function Admin() {
  // Read into a title, outside a tooltip: resolved so it is never the loading placeholder.
  const feedStatsHelp = useHelpText("admin.feedstats.link");
  const diskFreeHelp = useHelpText("admin.diskFree");
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  // The Agencies tab was called "Directory" until it became a tab of its own, and
  // `?tab=directory` is in saved links, older docs and help text; it resolves here
  // rather than silently dumping the reader on Ingest.
  const asked = requested === "directory" ? "agencies" : requested;
  const tab = TABS.some((item) => item.id === asked) ? (asked as string) : "ingest";
  const { canWrite, isAdmin } = useCurrentUser();
  const status = useSystemStatus();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Admin"
        description="Ingest health, the transcription queue and its backends, retention, runtime settings, API tokens and accounts."
        actions={
          <div className="flex flex-wrap items-center gap-3">
            {/* The Broadcastify **feed** is a page of its own rather than a tenth tab:
                it is a dashboard with charts, not a form, and it sits next to Exports
                because the two are the same operator's two Broadcastify products. */}
            <Link
              to="/feed-stats"
              className="inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:underline"
              title={feedStatsHelp}
            >
              <Headphones size={14} aria-hidden="true" />
              Feed stats
            </Link>
            {status.data ? (
              <div className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
                <StatusChip status={status.data.status} tip="Overall health as the API reports it" />
                <span className="tabular-nums">v{status.data.version}</span>
                <span
                  className={cn(
                    "tabular-nums",
                    status.data.disk?.level === "critical"
                      ? "text-critical-ink"
                      : status.data.disk?.level === "warn"
                        ? "text-warning-ink"
                        : undefined,
                  )}
                  title={diskFreeHelp}
                >
                  {formatBytes(status.data.disk_free_bytes)} free of{" "}
                  {formatBytes(status.data.disk_total_bytes)}
                  {typeof status.data.disk?.days_to_full === "number"
                    ? ` · ~${Math.round(status.data.disk.days_to_full)} days to full`
                    : null}
                </span>
              </div>
            ) : null}
          </div>
        }
      />

      <div className="px-4 md:px-6">
        {/* Nine tabs do not fit a phone. They used to scroll sideways, which hides the
            three on the right behind a gesture nothing on the page suggests; below `sm`
            they become the control a phone already knows how to open. */}
        <div className="border-b border-line pb-2 sm:hidden">
          <Select
            value={tab}
            onValueChange={(next) => setSearchParams({ tab: next }, { replace: true })}
            options={TABS.map((item) => ({ value: item.id, label: item.label }))}
            ariaLabel="Admin sections"
            tip="Pick the part of the system to work on"
            className="w-full"
          />
        </div>
        <div className="hidden sm:block">
          <Tabs
            idPrefix="admin"
            items={TABS}
            value={tab}
            onChange={(next) => setSearchParams({ tab: next }, { replace: true })}
            label="Admin sections"
            className="overflow-x-auto"
            listClassName="flex gap-1 border-b border-line pb-2"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6">
        {tab === "ingest" ? (
          <TabPanel idPrefix="admin" value="ingest" className="space-y-4">
            <IngestTab canWrite={canWrite} />
          </TabPanel>
        ) : null}
        {tab === "transcription" ? (
          <TabPanel idPrefix="admin" value="transcription" className="space-y-4">
            <JobsTab canWrite={canWrite} />
          </TabPanel>
        ) : null}
        {tab === "backends" ? (
          <TabPanel idPrefix="admin" value="backends">
            <BackendsTab />
          </TabPanel>
        ) : null}
        {tab === "recorder" ? (
          <TabPanel idPrefix="admin" value="recorder" className="space-y-4">
            <RecorderTab />
          </TabPanel>
        ) : null}
        {tab === "audio" ? (
          <TabPanel idPrefix="admin" value="audio" className="space-y-4">
            <AudioTab canWrite={canWrite} />
          </TabPanel>
        ) : null}
        {tab === "agencies" ? (
          <TabPanel idPrefix="admin" value="agencies" className="space-y-4">
            <AgenciesTab canWrite={isAdmin} />
          </TabPanel>
        ) : null}
        {tab === "retention" ? (
          <TabPanel idPrefix="admin" value="retention" className="space-y-4">
            <RetentionTab canWrite={canWrite} />
          </TabPanel>
        ) : null}
        {tab === "settings" ? (
          <TabPanel idPrefix="admin" value="settings">
            <SettingsTab canWrite={canWrite} />
          </TabPanel>
        ) : null}
        {tab === "https" ? (
          <TabPanel idPrefix="admin" value="https">
            <HttpsTab />
          </TabPanel>
        ) : null}
        {tab === "exports" ? (
          <TabPanel idPrefix="admin" value="exports">
            <ExportsTab />
          </TabPanel>
        ) : null}
        {tab === "tokens" ? (
          <TabPanel idPrefix="admin" value="tokens">
            <TokensTab canWrite={canWrite} canAdmin={isAdmin} />
          </TabPanel>
        ) : null}
        {tab === "users" ? (
          <TabPanel idPrefix="admin" value="users">
            <UsersTab canWrite={canWrite} />
          </TabPanel>
        ) : null}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- ingest */

function IngestTab({ canWrite }: { canWrite: boolean }) {
  const ingest = useAdminIngest();
  const [status, setStatus] = useState("failed");
  const files = useIngestFiles(status);
  const rescan = useRescan();
  const retry = useRetryIngestFile();
  const info = ingest.data?.status;

  return (
    <>
      {/* The first question an operator has when they open this page is not "is the
          watcher healthy" but "is anything moving" — so the live panel comes first and
          the Watcher card, which is about one ingest path, sits under it. */}
      <LiveActivity detail />
      <Card>
        <CardHeader
          title="Watcher"
          hint={HELP["admin.ingest.status"]}
          actions={
            <Button
              tip={canWrite ? HELP["admin.ingest.rescan"] : HELP["app.readOnly"]}
              size="sm"
              disabled={!canWrite || rescan.isPending}
              onClick={() =>
                rescan.mutate(undefined, {
                  onSuccess: () => toast("Full rescan queued", { tone: "success" }),
                  onError: (error) => mutationErrorToast(error, "Could not queue the rescan"),
                })
              }
            >
              <RefreshCw size={14} aria-hidden="true" /> Rescan now
            </Button>
          }
        />
        {ingest.isLoading ? (
          <SkeletonText lines={3} className="p-4" />
        ) : (
          <dl className="grid gap-x-6 gap-y-2 p-4 text-sm sm:grid-cols-3 lg:grid-cols-5">
            <Stat label="Watching" value={info?.watching ? "yes" : "no"} />
            <Stat label="Audio root" value={info?.audio_root ?? "—"} />
            <Stat label="Lag" value={formatLag(info?.lag_s ?? null)} />
            <Stat label="New (24h)" value={formatNumber(info?.files_new_24h)} />
            <Stat label="Failed (24h)" value={formatNumber(info?.files_failed_24h)} />
          </dl>
        )}
      </Card>

      <IngestSourcesCard canWrite={canWrite} />

      <Card>
        <CardHeader title="Recent runs" hint={HELP["admin.ingest.runs"]} />
        <table className="w-full text-left text-xs">
          <caption className="sr-only">The last few ingest batches</caption>
          <thead>
            <tr className="text-xs text-ink-2">
              <th scope="col" className="px-3 py-1.5">
                Started
              </th>
              <th scope="col" className="px-3 py-1.5 text-right">
                Seen
              </th>
              <th scope="col" className="px-3 py-1.5 text-right">
                New
              </th>
              <th scope="col" className="px-3 py-1.5 text-right">
                Duplicate
              </th>
              <th scope="col" className="px-3 py-1.5 text-right">
                Failed
              </th>
            </tr>
          </thead>
          <tbody>
            {(ingest.data?.runs ?? []).map((run) => (
              <tr key={run.id} className="border-t border-line">
                <td className="px-3 py-1.5 tabular-nums text-ink-2">{formatDateTime(run.started_at)}</td>
                <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">{run.files_seen}</td>
                <td className="px-3 py-1.5 text-right tabular-nums text-ink">{run.files_new}</td>
                <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">{run.files_duplicate}</td>
                <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">{run.files_failed}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card>
        <CardHeader
          title="Files"
          hint={HELP["admin.ingest.files"]}
          actions={
            <Select
              value={status}
              onValueChange={setStatus}
              options={[
                { value: "failed", label: "Failed" },
                { value: "duplicate", label: "Duplicate" },
                { value: "ingested", label: "Ingested" },
                { value: "", label: "All" },
              ]}
              tip={HELP["admin.ingest.filter"]}
              ariaLabel="Which files to list"
              size="sm"
            />
          }
        />
        {files.isLoading ? (
          <SkeletonText lines={4} className="p-4" />
        ) : (files.data?.items ?? []).length === 0 ? (
          <EmptyState title="Nothing here" hint="No files in this state." />
        ) : (
          <ul className="divide-y divide-line">
            {(files.data?.items ?? []).map((file) => (
              <li key={file.id} className="flex flex-wrap items-center gap-2 px-4 py-2 text-xs">
                <StatusChip status={file.status} tip={HELP["admin.ingest.files"]} />
                <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-2">{file.path}</span>
                {file.error ? <span className="text-critical-ink">{file.error}</span> : null}
                <span className="tabular-nums text-muted">{file.attempts} tries</span>
                <Button
                  tip={canWrite ? HELP["admin.ingest.retry"] : HELP["app.readOnly"]}
                  size="iconSm"
                  variant="ghost"
                  disabled={!canWrite || file.status === "ingested"}
                  aria-label={`Retry ${file.path}`}
                  onClick={() =>
                    retry.mutate(file.id, {
                      onSuccess: () => toast("Retried", { tone: "success" }),
                      onError: (error) => mutationErrorToast(error, "Retry failed"),
                    })
                  }
                >
                  <RotateCcw size={13} aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

/* ------------------------------------------------------------------ jobs */

function JobsTab({ canWrite }: { canWrite: boolean }) {
  const [statuses, setStatuses] = useState<string[]>(["queued", "running", "failed"]);
  const jobs = useJobs(statuses);
  const requeue = useRequeueJobs();
  const failed = useMemo(
    () => (jobs.data?.items ?? []).filter((job) => job.status === "failed"),
    [jobs.data],
  );

  return (
    <Card>
      <CardHeader
        title="Transcription queue"
        hint={HELP["admin.jobs.table"]}
        actions={
          <>
            {["queued", "running", "done", "failed"].map((status) => (
              <Chip
                key={status}
                label={status}
                tip={HELP["admin.jobs.status"]}
                active={statuses.includes(status)}
                onClick={() =>
                  setStatuses(
                    statuses.includes(status)
                      ? statuses.filter((value) => value !== status)
                      : [...statuses, status],
                  )
                }
              />
            ))}
            <Button
              tip={canWrite ? HELP["admin.jobs.requeue"] : HELP["app.readOnly"]}
              size="sm"
              variant="primary"
              disabled={!canWrite || failed.length === 0}
              onClick={() =>
                requeue.mutate(
                  failed.map((job) => job.id),
                  {
                    onSuccess: (result) => toast(`${result.requeued} jobs requeued`, { tone: "success" }),
                    onError: (error) => mutationErrorToast(error, "Could not requeue"),
                  },
                )
              }
            >
              <Play size={14} aria-hidden="true" /> Requeue {failed.length} failed
            </Button>
          </>
        }
      />
      {jobs.isLoading ? (
        <SkeletonText lines={6} className="p-4" />
      ) : (jobs.data?.items ?? []).length === 0 ? (
        <EmptyState title="The queue is empty" hint="Nothing is waiting to be transcribed." />
      ) : (
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Transcription jobs with their status, backend and errors</caption>
          <thead>
            <tr className="text-xs text-ink-2">
              <th scope="col" className="px-3 py-1.5">
                Status
              </th>
              <th scope="col" className="px-3 py-1.5">
                Call
              </th>
              <th scope="col" className="px-3 py-1.5">
                Talkgroup
              </th>
              <th scope="col" className="px-3 py-1.5">
                Backend
              </th>
              <th scope="col" className="px-3 py-1.5 text-right">
                Priority
              </th>
              <th scope="col" className="px-3 py-1.5">
                Last error
              </th>
            </tr>
          </thead>
          <tbody>
            {(jobs.data?.items ?? []).map((job) => (
              <tr key={job.id} className="border-t border-line">
                <td className="px-3 py-1.5">
                  <StatusChip status={job.status} tip={HELP["admin.jobs.table"]} />
                </td>
                <td className="px-3 py-1.5 tabular-nums text-ink-2">{job.call_id}</td>
                <td className="px-3 py-1.5 text-ink-2">{job.talkgroup_alias ?? "—"}</td>
                <td className="px-3 py-1.5 text-ink-2">{job.backend_name ?? "—"}</td>
                <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">{job.priority}</td>
                <td className="max-w-64 truncate px-3 py-1.5 text-critical-ink">
                  <Tip label={job.last_error ?? HELP["admin.jobs.error"]}>
                    <span tabIndex={0}>{job.last_error ?? ""}</span>
                  </Tip>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

/* ------------------------------------------------------------- retention */

function RetentionTab({ canWrite }: { canWrite: boolean }) {
  const policies = useRetentionPolicies();
  const save = useSaveRetentionPolicy();
  const run = useRunRetention();

  return (
    <Card>
      <CardHeader
        title="Retention policies"
        hint={HELP["admin.retention.table"]}
        actions={
          <Button
            tip={canWrite ? HELP["admin.retention.run"] : HELP["app.readOnly"]}
            size="sm"
            variant="primary"
            disabled={!canWrite || run.isPending}
            onClick={() =>
              run.mutate(undefined, {
                onSuccess: (result) =>
                  toast("Retention run finished", {
                    tone: "success",
                    body: `${result.audio_deleted} recordings deleted, ${result.audio_archived} archived, ${formatBytes(result.bytes_freed)} freed.`,
                  }),
                onError: (error) => mutationErrorToast(error, "The retention run failed"),
              })
            }
          >
            <Play size={14} aria-hidden="true" /> Run now
          </Button>
        }
      />
      {policies.isLoading ? (
        <SkeletonText lines={4} className="p-4" />
      ) : (
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Retention rules by scope</caption>
          <thead>
            <tr className="text-xs text-ink-2">
              <th scope="col" className="px-3 py-1.5">
                Scope
              </th>
              <ThTip help="admin.retention.keepDays">Keep records</ThTip>
              <ThTip help="admin.retention.keepAudioDays">Keep audio</ThTip>
              <ThTip help="admin.retention.keepTranscripts">Transcripts</ThTip>
              <ThTip help="admin.retention.archivePath">Archive to</ThTip>
            </tr>
          </thead>
          <tbody>
            {(policies.data ?? []).map((policy) => (
              <tr key={policy.id} className="border-t border-line">
                <td className="px-3 py-1.5 text-ink">{policy.scope_label}</td>
                <td className="px-3 py-1.5">
                  <TextInput
                    type="number"
                    disabled={!canWrite}
                    aria-label={`Keep records for ${policy.scope_label}, days`}
                    defaultValue={policy.keep_days || ""}
                    placeholder="forever"
                    className="h-7 w-24"
                    onBlur={(event) =>
                      save.mutate({
                        ...policy,
                        // 0 is how the API spells "keep forever"; the field shows it empty.
                        keep_days: event.target.value === "" ? 0 : Number(event.target.value),
                      })
                    }
                  />
                </td>
                <td className="px-3 py-1.5">
                  <TextInput
                    type="number"
                    disabled={!canWrite}
                    aria-label={`Keep audio for ${policy.scope_label}, days`}
                    defaultValue={policy.keep_audio_days || ""}
                    placeholder="forever"
                    className="h-7 w-24"
                    onBlur={(event) =>
                      save.mutate({
                        ...policy,
                        keep_audio_days: event.target.value === "" ? 0 : Number(event.target.value),
                      })
                    }
                  />
                </td>
                <td className="px-3 py-1.5">
                  <Switch
                    checked={policy.keep_transcripts}
                    disabled={!canWrite}
                    onCheckedChange={(next) => save.mutate({ ...policy, keep_transcripts: next })}
                    label={null}
                    tip={HELP["admin.retention.keepTranscripts"]}
                  />
                </td>
                <td className="px-3 py-1.5 font-mono text-[11px] text-ink-2">{policy.archive_path ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="border-t border-line p-3">
        <Button
          tip={canWrite ? HELP["admin.retention.add"] : HELP["app.readOnly"]}
          size="sm"
          disabled={!canWrite}
          onClick={() =>
            save.mutate(
              {
                scope: "talkgroup",
                scope_id: null,
                keep_days: 365,
                keep_audio_days: 90,
                keep_transcripts: true,
                archive_path: null,
              },
              { onSuccess: () => toast("Policy added", { tone: "success" }) },
            )
          }
        >
          <Plus size={14} aria-hidden="true" /> Add a policy
        </Button>
      </div>
    </Card>
  );
}

function ThTip({ help, children }: { help: keyof typeof HELP; children: string }) {
  return (
    <th scope="col" className="px-3 py-1.5">
      <Tip label={HELP[help]}>
        <span tabIndex={0}>{children}</span>
      </Tip>
    </th>
  );
}

/* -------------------------------------------------------------- settings */

/* The settings form moved to its own file when it grew an index, sub-sections and a
   filter; `DEDICATED_GROUPS` went with it because it is that form's business alone. */
export { DEDICATED_GROUPS, SettingsTab } from "./admin/SettingsTab";

/* ---------------------------------------------------------------- tokens */

function TokensTab({ canWrite, canAdmin }: { canWrite: boolean; canAdmin: boolean }) {
  const tokens = useTokens();
  const create = useCreateToken();
  const revoke = useRevokeToken();
  const remove = useDeleteToken();
  const [deleting, setDeleting] = useState<ApiTokenInfo | null>(null);
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [scopes, setScopes] = useState<TokenScope[]>(["read"]);
  const [expires, setExpires] = useState("");
  const [secret, setSecret] = useState<string | null>(null);

  return (
    <Card>
      <CardHeader
        title="API tokens"
        hint={HELP["admin.tokens.table"]}
        actions={
          <Button
            tip={canWrite ? HELP["admin.tokens.create"] : HELP["app.readOnly"]}
            size="sm"
            variant="primary"
            disabled={!canWrite}
            onClick={() => {
              setSecret(null);
              setOpen(true);
            }}
          >
            <Plus size={14} aria-hidden="true" /> New token
          </Button>
        }
      />
      {tokens.isLoading ? (
        <SkeletonText lines={4} className="p-4" />
      ) : (
        <ul className="divide-y divide-line">
          {(tokens.data ?? []).map((token) => (
            <li key={token.id} className="flex flex-wrap items-center gap-2 px-4 py-2 text-xs">
              <span className="font-medium text-ink">{token.label}</span>
              <span className="font-mono text-muted">…{token.suffix}</span>
              <span className="flex flex-wrap gap-1">
                {token.scopes.map((scope) => (
                  <StatusChip key={scope} status={scope} tip={HELP["admin.tokens.scopes"]} />
                ))}
              </span>
              {token.used_by_recorders.length > 0 ? (
                <Tip label={HELP["admin.tokens.usedBy"]}>
                  <span tabIndex={0} className="text-ink-2">
                    used by {token.used_by_recorders.join(", ")}
                  </span>
                </Tip>
              ) : null}
              {token.expires_at ? (
                <Tip label={HELP["admin.tokens.expires"]}>
                  <span
                    tabIndex={0}
                    className={
                      new Date(token.expires_at).getTime() <= Date.now() ? "text-critical-ink" : "text-ink-2"
                    }
                  >
                    {new Date(token.expires_at).getTime() <= Date.now() ? "expired " : "expires "}
                    {formatRelative(token.expires_at)}
                  </span>
                </Tip>
              ) : null}
              <Tip label={HELP["admin.tokens.lastUsed"]}>
                <span tabIndex={0} className="ml-auto text-muted">
                  {token.last_used_at ? formatRelative(token.last_used_at) : "never used"}
                </span>
              </Tip>
              {token.revoked_at ? (
                <>
                  <StatusChip status="failed" tip="This token has been revoked and no longer works" />
                  {/* A revoked row is dead weight; only an admin may sweep it away, and only
                      after saying so out loud — the row is the audit trail. */}
                  <Button
                    tip={canAdmin ? HELP["admin.tokens.delete"] : HELP["app.adminOnly"]}
                    size="iconSm"
                    variant="ghost"
                    disabled={!canAdmin}
                    aria-label={`Delete ${token.label}`}
                    onClick={() => setDeleting(token)}
                  >
                    <Trash2 size={13} aria-hidden="true" />
                  </Button>
                </>
              ) : (
                <Button
                  tip={canWrite ? HELP["admin.tokens.revoke"] : HELP["app.readOnly"]}
                  size="iconSm"
                  variant="ghost"
                  disabled={!canWrite}
                  aria-label={`Revoke ${token.label}`}
                  onClick={() =>
                    revoke.mutate(token.id, { onSuccess: () => toast("Token revoked", { tone: "success" }) })
                  }
                >
                  <Trash2 size={13} aria-hidden="true" />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}

      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="New API token"
        description="The token's value is shown once, here, and never again. Copy it before closing this dialog."
        footer={
          secret ? (
            <Button tip="Close" size="sm" variant="primary" onClick={() => setOpen(false)}>
              Done
            </Button>
          ) : (
            <>
              <Button tip="Close without creating" size="sm" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button
                tip="Create the token"
                size="sm"
                variant="primary"
                disabled={!label.trim() || create.isPending}
                onClick={() =>
                  create.mutate(
                    // A `datetime-local` value is wall-clock text with no zone; the browser
                    // read it in the operator's zone, so it is converted here rather than
                    // sent as-is and read by the API as UTC.
                    {
                      label,
                      scopes,
                      expires_at: expires === "" ? null : new Date(expires).toISOString(),
                    },
                    {
                      onSuccess: (created) => setSecret(created.token),
                      onError: (error) => mutationErrorToast(error, "Could not create the token"),
                    },
                  )
                }
              >
                Create
              </Button>
            </>
          )
        }
      >
        {secret ? (
          <div className="space-y-2">
            <p className="text-xs text-ink-2">Copy this now — it will not be shown again.</p>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded-md border border-line bg-surface-2 px-2 py-1.5 font-mono text-xs text-ink">
                {secret}
              </code>
              <Button
                tip={HELP["admin.tokens.copy"]}
                size="iconSm"
                onClick={() => {
                  void copyAndToast(secret, { label: "Token" });
                }}
                aria-label="Copy the token"
              >
                <Copy size={14} aria-hidden="true" />
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <Field label="Label" help="admin.tokens.label">
              {(id) => (
                <TextInput
                  id={id}
                  value={label}
                  placeholder="sdrtrunk recorder"
                  onChange={(event) => setLabel(event.target.value)}
                />
              )}
            </Field>
            <Field label="Expires" help="admin.tokens.expires">
              {(id) => (
                <TextInput
                  id={id}
                  type="datetime-local"
                  value={expires}
                  onChange={(event) => setExpires(event.target.value)}
                />
              )}
            </Field>
            <div>
              <p className="mb-1 text-xs font-medium text-ink-2">Scopes</p>
              <div className="flex flex-wrap gap-1.5">
                {TOKEN_SCOPES.map((scope) => (
                  <Chip
                    key={scope}
                    label={scope}
                    tip={HELP["admin.tokens.scopes"]}
                    active={scopes.includes(scope)}
                    onClick={() =>
                      setScopes(
                        scopes.includes(scope)
                          ? scopes.filter((value) => value !== scope)
                          : [...scopes, scope],
                      )
                    }
                  />
                ))}
              </div>
            </div>
          </div>
        )}
      </Dialog>

      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={deleting ? `Delete ${deleting.label}?` : "Delete token"}
        description="The row goes for good, and with it the record that this key ever existed. Any recorder still bound to it re-binds on its next contact."
        footer={
          <>
            <Button tip={HELP["admin.tokens.delete.cancel"]} size="sm" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              tip={HELP["admin.tokens.delete.confirm"]}
              size="sm"
              variant="danger"
              disabled={remove.isPending}
              onClick={() => {
                if (!deleting) return;
                const label = deleting.label;
                remove.mutate(deleting.id, {
                  onSuccess: () => {
                    toast(`Deleted ${label}`, { tone: "success" });
                    setDeleting(null);
                  },
                  onError: (error) => mutationErrorToast(error, "Could not delete the token"),
                });
              }}
            >
              Delete token
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {deleting ? `Permanently delete token ${deleting.label}? This cannot be undone.` : null}
        </p>
      </Dialog>
    </Card>
  );
}

/* ----------------------------------------------------------------- users */

/**
 * Accounts, and the two things you can only do to one from here.
 *
 * The create form asks for a password because `POST /admin/users` requires one
 * (422 "password is required" without it) — this form used to send only a
 * username, and every account it appeared to create was in fact refused. The mock
 * accepted it, so the tests agreed with the bug rather than with the API.
 *
 * Setting a password afterwards is a separate row action rather than a field on
 * an edit form: it is the one change here that is not reversible by looking at
 * the row, so it should take a deliberate act, and `UserPatch.password` is the
 * only way an operator can rescue an account whose password is lost.
 */
function UsersTab({ canWrite }: { canWrite: boolean }) {
  const users = useUsers();
  const save = useSaveUser();
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [resetting, setResetting] = useState<User | null>(null);
  const [newPassword, setNewPassword] = useState("");

  return (
    <Card>
      <CardHeader
        title="Accounts"
        hint={HELP["admin.users.table"]}
        actions={
          <Button
            tip={canWrite ? HELP["admin.users.create"] : HELP["app.readOnly"]}
            size="sm"
            variant="primary"
            disabled={!canWrite}
            onClick={() => setOpen(true)}
          >
            <Plus size={14} aria-hidden="true" /> New account
          </Button>
        }
      />
      {users.isLoading ? (
        <SkeletonText lines={3} className="p-4" />
      ) : (
        <ul className="divide-y divide-line">
          {(users.data ?? []).map((user) => (
            <li key={user.id} className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
              <span className="font-medium text-ink">{user.username}</span>
              <span className="text-xs text-ink-2">{user.display_name}</span>
              <Switch
                checked={user.is_admin}
                disabled={!canWrite}
                onCheckedChange={(next) => save.mutate({ id: user.id, patch: { is_admin: next } })}
                label="admin"
                tip={HELP["admin.users.admin"]}
              />
              <Switch
                checked={user.is_active}
                disabled={!canWrite}
                onCheckedChange={(next) => save.mutate({ id: user.id, patch: { is_active: next } })}
                label="active"
                tip={HELP["admin.users.active"]}
              />
              <span className="ml-auto text-[11px] text-muted">
                last signed in {user.last_login_at ? formatRelative(user.last_login_at) : "never"}
              </span>
              <Button
                tip={canWrite ? HELP["admin.users.password"] : HELP["app.readOnly"]}
                size="sm"
                variant="ghost"
                disabled={!canWrite}
                aria-label={`Set a password for ${user.username}`}
                onClick={() => {
                  setNewPassword("");
                  setResetting(user);
                }}
              >
                <KeyRound size={14} aria-hidden="true" /> Set password
              </Button>
            </li>
          ))}
        </ul>
      )}

      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="New account"
        description="A new account can read everything straight away; make it an administrator only if it needs to change settings."
        footer={
          <>
            <Button tip="Close without creating" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              tip={HELP["admin.users.save"]}
              size="sm"
              variant="primary"
              // The API refuses a passwordless account, so the button that would
              // send one is not offered.
              disabled={!username.trim() || password === ""}
              onClick={() =>
                save.mutate(
                  { patch: { username, password, is_admin: isAdmin } },
                  {
                    onSuccess: () => {
                      toast("Account created", { tone: "success" });
                      setOpen(false);
                      setUsername("");
                      setPassword("");
                    },
                    onError: (error) => mutationErrorToast(error, "Could not create the account"),
                  },
                )
              }
            >
              Create
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <Field label="Username" help="admin.users.create">
            {(id) => (
              <TextInput id={id} value={username} onChange={(event) => setUsername(event.target.value)} />
            )}
          </Field>
          <Field label="Password" help="admin.users.password">
            {(id) => (
              <TextInput
                id={id}
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            )}
          </Field>
          <Switch
            checked={isAdmin}
            onCheckedChange={setIsAdmin}
            label="Administrator"
            tip={HELP["admin.users.admin"]}
          />
        </div>
      </Dialog>

      <Dialog
        open={resetting !== null}
        onOpenChange={(next) => !next && setResetting(null)}
        title={`Set a password for ${resetting?.username ?? ""}`}
        description="The account's old password stops working straight away, and any session it already has is unaffected. Tell the holder out of band — this is the only time the app shows it."
        footer={
          <>
            <Button tip="Close without changing the password" size="sm" onClick={() => setResetting(null)}>
              Cancel
            </Button>
            <Button
              tip={HELP["admin.users.password"]}
              size="sm"
              variant="primary"
              disabled={newPassword === "" || resetting === null}
              onClick={() => {
                if (resetting === null) return;
                save.mutate(
                  { id: resetting.id, patch: { password: newPassword } },
                  {
                    onSuccess: () => {
                      toast(`Password set for ${resetting.username}`, { tone: "success" });
                      setResetting(null);
                      setNewPassword("");
                    },
                    onError: (error) => mutationErrorToast(error, "Could not set the password"),
                  },
                );
              }}
            >
              Set password
            </Button>
          </>
        }
      >
        <Field label="New password" help="admin.users.password">
          {(id) => (
            <TextInput
              id={id}
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
          )}
        </Field>
      </Dialog>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="truncate text-ink">{value}</dd>
    </div>
  );
}
