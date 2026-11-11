import { AudioLines, Bell, Play, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { apiUrl } from "../../api/client";
import {
  useApplyAudioProfile,
  useAudioPreview,
  useAudioProfiles,
  useAudioReprocess,
  useAudioStatus,
  useDeleteAudioProfile,
  useSaveSettings,
  useSettings,
} from "../../api/hooks";
import type { AudioPreview, AudioProfile } from "../../api/types";
import { AudioProfileDialog } from "../../components/admin/AudioProfileDialog";
import { Button } from "../../components/ui/Button";
import { Card, CardHeader } from "../../components/ui/Card";
import { inputClass } from "../../components/ui/Field";
import { SkeletonText } from "../../components/ui/Skeleton";
import { ErrorState } from "../../components/ui/States";
import { Switch } from "../../components/ui/Switch";
import { Tip } from "../../components/ui/Tooltip";
import { HELP } from "../../help/help";
import { formatNumber } from "../../lib/format";
import { mutationErrorToast, toast } from "../../state/toasts";
import { SettingField } from "./SettingField";

/** The settings group this tab owns outright — the Settings tab holds it back
    (`DEDICATED_GROUPS`) and links here instead of rendering a second copy of the form. */
const GROUP = "Audio";

/**
 * Admin → Audio: the processing chain, what it has covered, and two ways to act on it.
 *
 * The layout follows the order an operator actually works in — look at the numbers, change
 * a setting, hear it on one call, then decide whether the archive should be brought up to
 * match. The preview is deliberately *before* the reprocess button: re-rendering a hundred
 * thousand calls to find out a setting was wrong is an expensive way to learn it.
 */
export function AudioTab({ canWrite }: { canWrite: boolean }) {
  const status = useAudioStatus();
  const settings = useSettings();
  const save = useSaveSettings();
  const reprocess = useAudioReprocess();
  const preview = useAudioPreview();
  const profiles = useAudioProfiles();
  const remove = useDeleteAudioProfile();
  const apply = useApplyAudioProfile();

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<AudioProfile | null>(null);
  /**
   * `?call=` on the tab's own URL, put there by the Tune… link on a call page. It is the
   * whole point of that link: the operator arrives at the editor with the recording that
   * sounded wrong already loaded into the preview.
   */
  const [searchParams] = useSearchParams();
  const deepLinkedCall = searchParams.get("call") ?? "";

  const [draft, setDraft] = useState<Record<string, string>>({});
  const [callId, setCallId] = useState("");
  const [force, setForce] = useState(false);
  const [rendered, setRendered] = useState<AudioPreview | null>(null);

  const items = useMemo(
    () => (settings.data ?? []).filter((setting) => setting.group === GROUP),
    [settings.data],
  );
  const dirty = Object.keys(draft).length > 0;

  return (
    <>
      <Card>
        <CardHeader
          title="Audio processing"
          hint={HELP["admin.tab.audio"]}
          actions={
            <Button size="sm" variant="ghost" tip="Re-read the counts" onClick={() => status.refetch()}>
              <RefreshCw size={13} aria-hidden="true" /> Refresh
            </Button>
          }
        />
        {status.isLoading ? (
          <SkeletonText lines={3} className="p-4" />
        ) : status.error ? (
          <ErrorState error={status.error} onRetry={() => status.refetch()} />
        ) : status.data ? (
          <div className="grid grid-cols-2 gap-3 p-4 sm:grid-cols-4">
            <Stat
              label="Processed"
              value={formatNumber(status.data.processed)}
              hint="Calls with a processed copy on disk"
            />
            <Stat
              label="Pending"
              value={formatNumber(status.data.pending)}
              hint="Calls with audio and no current processed copy"
              tone={status.data.pending > 0 ? "warn" : undefined}
            />
            <Stat
              label="Failed"
              value={formatNumber(status.data.failed)}
              hint="Jobs parked after their retries; those calls play back raw"
              tone={status.data.failed > 0 ? "bad" : undefined}
            />
            <Stat
              label="Queued"
              value={formatNumber(status.data.jobs.queued ?? 0)}
              hint="Waiting for the worker"
            />
            <Stat
              label="Pages today"
              value={formatNumber(status.data.alert_today)}
              hint="Calls opening with alert tones since local midnight"
            />
            <Stat
              label="Tones today"
              value={formatNumber(status.data.tone_today)}
              hint="Calls with any sustained tone since local midnight"
            />
            <Stat
              label="Average gain"
              value={
                status.data.average_gain_db === null
                  ? "—"
                  : `${status.data.average_gain_db > 0 ? "+" : ""}${status.data.average_gain_db} dB`
              }
              hint="Over the last thousand processed calls"
            />
            <Stat
              label="Pipeline"
              value={`v${status.data.version}`}
              hint={`Reprocess version ${status.data.reprocess_version}${
                status.data.enabled ? "" : " — processing is switched off"
              }`}
              tone={status.data.enabled ? undefined : "warn"}
            />
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader
          title="Chain"
          hint={HELP["admin.audio.settings"]}
          actions={
            <Button
              size="sm"
              variant="primary"
              tip={canWrite ? "Save the chain" : HELP["app.readOnly"]}
              disabled={!canWrite || !dirty || save.isPending}
              onClick={() =>
                save.mutate(draft, {
                  onSuccess: () => {
                    toast("Audio settings saved", { tone: "success" });
                    setDraft({});
                  },
                  onError: (error) => mutationErrorToast(error, "Could not save the settings"),
                })
              }
            >
              Save changes
            </Button>
          }
        />
        {settings.isLoading ? (
          <SkeletonText lines={8} className="p-4" />
        ) : (
          <div className="grid gap-3 p-4 sm:grid-cols-2">
            {items.map((setting) => (
              <SettingField
                key={setting.key}
                setting={setting}
                value={draft[setting.key] ?? setting.value}
                dirty={setting.key in draft}
                disabled={!canWrite}
                onChange={(next) => setDraft({ ...draft, [setting.key]: next })}
              />
            ))}
          </div>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Audio profiles"
          hint={HELP["admin.audio.profiles"]}
          actions={
            <Button
              size="sm"
              variant="primary"
              tip={canWrite ? HELP["admin.audio.profiles.new"] : HELP["app.readOnly"]}
              disabled={!canWrite}
              onClick={() => {
                setEditing(null);
                setEditorOpen(true);
              }}
            >
              <Plus size={13} aria-hidden="true" /> New profile
            </Button>
          }
        />
        {profiles.isLoading ? (
          <SkeletonText lines={4} className="p-4" />
        ) : profiles.error ? (
          <ErrorState error={profiles.error} onRetry={() => profiles.refetch()} />
        ) : (
          <div className="divide-y divide-line">
            {(profiles.data ?? []).map((profile) => (
              <div key={profile.id} className="space-y-2 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-ink">{profile.name}</span>
                  {profile.builtin ? (
                    <Tip label={HELP["admin.audio.profiles.builtin"]}>
                      <span
                        tabIndex={0}
                        className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted"
                      >
                        built in
                      </span>
                    </Tip>
                  ) : null}
                  <span className="text-[11px] tabular-nums text-muted">
                    {formatNumber(profile.talkgroup_count)} channel
                    {profile.talkgroup_count === 1 ? "" : "s"}
                  </span>
                  <div className="ml-auto flex items-center gap-1.5">
                    <Button
                      size="sm"
                      variant="ghost"
                      tip={canWrite ? HELP["admin.audio.profiles.apply"] : HELP["app.readOnly"]}
                      disabled={!canWrite || profile.talkgroup_count === 0 || apply.isPending}
                      onClick={() =>
                        apply.mutate(
                          { id: profile.id, body: { all_with_profile: true, hours: 24 } },
                          {
                            onSuccess: (result) =>
                              // With processing switched off the server assigns the profile
                              // and queues nothing — the worker would only cancel it.
                              status.data?.enabled === false
                                ? toast("Profile assigned. Audio processing is off, so nothing was queued", {
                                    tone: "info",
                                  })
                                : toast(
                                    `Queued ${formatNumber(result.queued)} of ${formatNumber(result.matched)} call(s) on ${formatNumber(result.talkgroups)} channel(s)`,
                                    { tone: "success" },
                                  ),
                            onError: (error) => mutationErrorToast(error, "Could not queue the re-render"),
                          },
                        )
                      }
                    >
                      <Play size={13} aria-hidden="true" /> Apply
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      tip={canWrite ? HELP["admin.audio.profiles.edit"] : HELP["app.readOnly"]}
                      disabled={!canWrite}
                      onClick={() => {
                        setEditing(profile);
                        setEditorOpen(true);
                      }}
                    >
                      Edit
                    </Button>
                    {/* A builtin has no Delete at all rather than a Delete that always
                        fails: the server refuses it, and a control whose only outcome is
                        an error is worse than no control. */}
                    {profile.builtin ? null : (
                      <Button
                        size="sm"
                        variant="ghost"
                        tip={canWrite ? HELP["admin.audio.profiles.delete"] : HELP["app.readOnly"]}
                        disabled={!canWrite || remove.isPending}
                        aria-label={`Delete ${profile.name}`}
                        onClick={() =>
                          remove.mutate(profile.id, {
                            onSuccess: () => toast(`Deleted ${profile.name}`, { tone: "success" }),
                            // The refusals worth reading are the server's: a profile still
                            // carrying channels says how many, which is the next thing to do.
                            onError: (error) => mutationErrorToast(error, "Could not delete the profile"),
                          })
                        }
                      >
                        <Trash2 size={13} aria-hidden="true" />
                      </Button>
                    )}
                  </div>
                </div>
                {profile.description ? (
                  <p className="text-xs leading-relaxed text-ink-2">{profile.description}</p>
                ) : null}
                {/* Wraps rather than scrolling: a filter graph reads fine broken at any
                    character, and a scroll box would need a tab stop of its own. */}
                <pre className="whitespace-pre-wrap break-all rounded bg-surface-2 p-2 font-mono text-[10px] leading-relaxed text-ink-2">
                  {profile.rendered}
                </pre>
              </div>
            ))}
          </div>
        )}
      </Card>

      <AudioProfileDialog
        open={editorOpen}
        onOpenChange={setEditorOpen}
        profile={editing}
        callId={deepLinkedCall}
      />

      <Card>
        <CardHeader title="Try it on one call" hint={HELP["admin.audio.preview"]} />
        <div className="space-y-3 p-4">
          <div className="flex flex-wrap items-end gap-2">
            <div>
              <label htmlFor="audio-preview-call" className="mb-1 block text-xs font-medium text-ink-2">
                Call id
              </label>
              <input
                id="audio-preview-call"
                type="number"
                inputMode="numeric"
                value={callId}
                placeholder="123456"
                onChange={(event) => setCallId(event.target.value)}
                className={`${inputClass} w-40`}
              />
            </div>
            <Button
              size="sm"
              variant="secondary"
              disabled={!canWrite || !callId || preview.isPending}
              tip={canWrite ? HELP["admin.audio.preview"] : HELP["app.readOnly"]}
              onClick={() =>
                preview.mutate(Number(callId), {
                  onSuccess: setRendered,
                  onError: (error) => mutationErrorToast(error, "Could not render that call"),
                })
              }
            >
              <AudioLines size={13} aria-hidden="true" /> Render
            </Button>
          </div>

          {rendered ? (
            <div className="space-y-3 border-t border-line pt-3">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Stat label="Level before" value={`${rendered.meta.mean_db} dBFS`} hint="Whole-clip RMS" />
                <Stat
                  label="Level after"
                  value={`${rendered.meta.mean_db_after} dBFS`}
                  hint="After the chain"
                />
                <Stat
                  label="Peak after"
                  value={`${rendered.meta.peak_db_after} dBFS`}
                  hint="The limiter's ceiling"
                />
                <Stat
                  label="Trimmed"
                  value={`${rendered.meta.head_trim_s}s / ${rendered.meta.tail_trim_s}s`}
                  hint="Dead air taken off the head and the tail"
                />
              </div>
              {Array.isArray(rendered.meta.tones) && rendered.meta.tones.length > 0 ? (
                <p className="flex items-center gap-1.5 text-xs text-ink-2">
                  <Bell size={13} aria-hidden="true" className="text-warning-ink" />
                  {String(rendered.meta.tone_pattern)}:{" "}
                  {(rendered.meta.tones as { start_s: number; end_s: number; hz: number }[])
                    .map((tone) => `${tone.hz} Hz for ${(tone.end_s - tone.start_s).toFixed(2)}s`)
                    .join(", ")}
                </p>
              ) : null}
              <div className="flex flex-wrap items-center gap-4">
                <PreviewAudio label="Original" src={apiUrl(`/calls/${rendered.call_id}/audio?raw=1`)} />
                <PreviewAudio label="Processed" src={apiUrl(rendered.processed_url.replace(/^\/api/, ""))} />
              </div>
              <pre className="whitespace-pre-wrap break-all rounded bg-surface-2 p-2 font-mono text-[10px] leading-relaxed text-ink-2">
                {rendered.filter}
              </pre>
            </div>
          ) : null}
        </div>
      </Card>

      <Card>
        <CardHeader title="Reprocess the archive" hint={HELP["admin.audio.reprocess"]} />
        <div className="flex flex-wrap items-center gap-3 p-4">
          <Switch
            checked={force}
            disabled={!canWrite}
            onCheckedChange={setForce}
            label="Force"
            tip="Requeue calls whose processed copy is already current"
          />
          <Button
            size="sm"
            variant="secondary"
            disabled={!canWrite || reprocess.isPending}
            tip={canWrite ? HELP["admin.audio.reprocess"] : HELP["app.readOnly"]}
            onClick={() =>
              reprocess.mutate(
                { force },
                {
                  onSuccess: (result) =>
                    toast(
                      `Queued ${formatNumber(result.queued)} of ${formatNumber(result.matched)} call(s)`,
                      { tone: "success" },
                    ),
                  onError: (error) => mutationErrorToast(error, "Could not queue the backfill"),
                },
              )
            }
          >
            <Play size={13} aria-hidden="true" /> Queue backfill
          </Button>
          <p className="text-[11px] text-muted">
            Runs at the backlog band, behind anything arriving live. Raise{" "}
            <code className="font-mono">audio_reprocess_version</code> above and press this to bring the whole
            archive up to the settings as they now stand.
          </p>
        </div>
      </Card>
    </>
  );
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint: string;
  tone?: "warn" | "bad";
}) {
  return (
    <div title={hint}>
      <div className="text-xs text-muted">{label}</div>
      <div
        className={
          tone === "bad"
            ? "text-lg font-semibold tabular-nums text-critical-ink"
            : tone === "warn"
              ? "text-lg font-semibold tabular-nums text-warning-ink"
              : "text-lg font-semibold tabular-nums text-ink"
        }
      >
        {value}
      </div>
    </div>
  );
}

function PreviewAudio({ label, src }: { label: string; src: string }) {
  return (
    <div className="flex items-center gap-2 text-xs text-ink-2">
      <span className="w-20 shrink-0">{label}</span>
      {/* biome-ignore lint/a11y/useMediaCaption: scanner audio carries no caption track */}
      <audio controls preload="none" src={src} aria-label={label} className="h-8" />
    </div>
  );
}
