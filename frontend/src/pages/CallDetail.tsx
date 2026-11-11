import {
  ArrowLeft,
  Bell,
  ChevronLeft,
  ChevronRight,
  ClipboardCopy,
  Download,
  Link2,
  Lock,
  MoreHorizontal,
  Play,
  RefreshCw,
  SlidersHorizontal,
  Star,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { apiUrl, isMockMode } from "../api/client";
import {
  useAgencyLabel,
  useCall,
  useCallTranscripts,
  useConversation,
  useCurrentUser,
  useMakePrimary,
  useRadio,
  useRetranscribe,
  useTalkgroups,
  useUpdateCall,
  useWaveform,
} from "../api/hooks";
import type { Call, CallSourceQuality, TranscriptSegment } from "../api/types";
import { hasPlayableAudio, isBackfilled } from "../api/types";
import { formatPlayTime, usePlaybackDuration } from "../components/player/playback";
import { Waveform } from "../components/player/Waveform";
import { Button, GuardedButton } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { CategoryIcon } from "../components/ui/CategoryIcon";
import { Chip, chipClass, StatusChip } from "../components/ui/Chip";
import { TextArea, TextInput } from "../components/ui/Field";
import { Popover } from "../components/ui/Popover";
import { QualityDot } from "../components/ui/QualityDot";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { callSummary } from "../lib/callSummary";
import { copyAndToast } from "../lib/clipboard";
import { cn } from "../lib/cn";
import {
  audioProfileLabel,
  callTalkgroupLabel,
  categoryLabel,
  formatBytes,
  formatCallTime,
  formatDateTime,
  formatDuration,
  formatFrequency,
  formatNumber,
  formatUtc,
  siteLabel,
  timeSourceLabel,
} from "../lib/format";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import { useMediaQuery } from "../lib/useMediaQuery";
import { usePlayerStore } from "../state/player";
import { toast } from "../state/toasts";

/**
 * The recorder's own metadata, flattened for display.
 *
 * `raw_meta` is whatever the recorder wrote — a flat map of ID3 frames from sdrtrunk, a
 * nested JSON sidecar from trunk-recorder — and nothing here parses it back, so a nested
 * value is shown as its JSON rather than being flattened into rows that imply a schema
 * this panel does not have.
 */
/**
 * Field names a recorder spells tersely, given the name they have everywhere else.
 *
 * Only keys whose bare form would read as an abbreviation are here — trunkd's P25 facts,
 * which are the ones a person is most likely to be reading this panel for: which key
 * encrypted the call, and under which network. Everything else is shown as the recorder
 * wrote it, because inventing a label for a field nobody has documented is worse than
 * the field's own name.
 */
const RAW_META_LABELS: Record<string, string> = {
  algorithm: "Encryption algorithm",
  durationMs: "Duration (ms)",
  keyId: "Key ID",
  nac: "NAC",
  recorder: "Recorder",
  site: "Site",
  systemLabel: "System",
  talkgroupLabel: "Talkgroup",
};

function rawMetaEntries(meta: Record<string, unknown>, id3: Record<string, string>): [string, string][] {
  const source = Object.keys(meta).length ? meta : id3;
  return Object.entries(source).map(([key, value]) => [
    RAW_META_LABELS[key] ?? key,
    value === null || value === undefined
      ? ""
      : typeof value === "object"
        ? JSON.stringify(value)
        : String(value),
  ]);
}

function rawMetaTitle(ingestSource: string): string {
  if (ingestSource === "trunk_recorder") return "Sidecar metadata";
  if (ingestSource === "trunkd") return "Recorder metadata";
  return "ID3 frames";
}

const TABS = [
  { id: "transcript", label: "Transcript", tip: HELP["call.tabs.transcript"] },
  { id: "others", label: "Other transcripts", tip: HELP["call.tabs.others"] },
  { id: "metadata", label: "Raw metadata", tip: HELP["call.tabs.metadata"] },
  { id: "links", label: "Links", tip: HELP["call.tabs.links"] },
];

/** Everything known about one transmission. */
/**
 * What the tone chip says on hover: the segments themselves, because "625 Hz then 750 Hz"
 * is what identifies whose encoder sent the page.
 */
function toneTip(meta: Call["audio_meta"]): string {
  const tones = (meta?.tones ?? []) as { start_s: number; end_s: number; hz: number }[];
  if (!Array.isArray(tones) || tones.length === 0) return "A sustained tone was found in this call";
  return tones
    .map((tone) => `${Math.round(tone.hz)} Hz for ${(tone.end_s - tone.start_s).toFixed(2)}s`)
    .join(", then ");
}

/**
 * The profile named in the call's own processing metadata, or `null` for the global chain.
 *
 * Read off `audio_meta` rather than off the talkgroup's current setting on purpose: a
 * profile assigned this morning does not retroactively describe a file rendered last week,
 * and the whole reason the name is stored per call is to answer "what actually produced
 * *this* audio" rather than "what would produce it now".
 */
function audioProfileName(meta: Call["audio_meta"]): string | null {
  const profile = (meta as Record<string, unknown> | null)?.profile;
  return typeof profile === "string" && profile !== "" ? profile : null;
}

export function CallDetail() {
  const { id } = useParams();
  const callId = Number(id);
  const navigate = useNavigate();
  const call = useCall(Number.isFinite(callId) ? callId : null);
  // Only a call whose recording is on hand has peaks to draw: anything else answers 410,
  // so the request waits for the row and is not made at all for discarded/purged/missing.
  const waveform = useWaveform(
    Number.isFinite(callId) ? callId : null,
    hasPlayableAudio(call.data?.audio_state),
  );
  const transcripts = useCallTranscripts(Number.isFinite(callId) ? callId : null);
  const { canWrite, isAdmin } = useCurrentUser();
  const agencyName = useAgencyLabel();
  const updateCall = useUpdateCall();
  const retranscribe = useRetranscribe();
  const makePrimary = useMakePrimary();
  const [tab, setTab] = useState("transcript");

  const current = usePlayerStore((state) => state.current);
  const playing = usePlayerStore((state) => state.playing);
  const currentTime = usePlayerStore((state) => state.currentTime);
  const select = usePlayerStore((state) => state.select);
  const seek = usePlayerStore((state) => state.seek);
  const setQueue = usePlayerStore((state) => state.setQueue);

  const isCurrent = current?.id === callId;
  const detail = call.data;
  // One length for this call everywhere on screen — the player's once it is loaded there.
  const length = usePlaybackDuration(detail);
  // The recording details are open from md up and folded on a phone, where the transcript
  // is what the first screen is for; a reader's own toggle sticks until the width changes.
  const wide = useMediaQuery("(min-width: 768px)");
  const [detailsOpen, setDetailsOpen] = useState(wide);
  useEffect(() => setDetailsOpen(wide), [wide]);
  const rawEntries = useMemo(
    () => (detail ? rawMetaEntries(detail.raw.raw_meta, detail.raw.id3) : []),
    [detail],
  );

  // `CallDetail.conversation` is the conversation *record*; its calls come from
  // `GET /conversations/{id}`, which is one extra request but keeps the call
  // payload from carrying a whole exchange every time.
  const conversation = useConversation(detail?.conversation_id ?? null);
  // The patch chips show each member channel by name, not just its number.
  const talkgroups = useTalkgroups();
  // The roster identity of the radio that keyed up. One extra request on this page,
  // and none at all on the calls list.
  const radio = useRadio(detail?.radio_id ?? null);
  const identity = [
    radio.data?.current_unit
      ? radio.data.current_unit.name
        ? `${radio.data.current_unit.callsign} — ${radio.data.current_unit.name}`
        : radio.data.current_unit.callsign
      : null,
    radio.data?.current_person?.name ?? null,
  ]
    .filter(Boolean)
    .join(" · ");
  const talkgroupByTgid = useMemo(
    () => new Map((talkgroups.data?.items ?? []).map((tg) => [tg.tgid, tg.alias])),
    [talkgroups.data],
  );
  const thread = useMemo(() => conversation.data?.calls ?? [], [conversation.data]);

  // "DPS LAW 2D · Oct 29, 20:32:19 · Keyup": the tab says which call it is.
  useDocumentTitle(
    detail ? (detail.patch_label ?? callTalkgroupLabel(detail)) : call.isLoading ? null : "Call",
    detail ? formatCallTime(detail.started_at) : null,
  );

  // The conversation is the natural queue here: "next" should walk the exchange,
  // not whatever list the reader happened to arrive from.
  useEffect(() => {
    if (thread.length) setQueue(thread);
  }, [thread, setQueue]);

  if (call.isLoading) return <SkeletonText lines={10} className="p-6" />;
  if (call.error) return <ErrorState error={call.error} onRetry={() => void call.refetch()} />;
  if (!detail)
    return <EmptyState title="Call not found" hint="It may have been purged by a retention rule." />;

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4 pb-40 md:p-6">
      {/* Navigation, then the two things worth a button of their own: Star (for someone
          who can star) and a menu with the rest. Seven buttons in a row pushed Play to
          60% down a phone screen. */}
      <div className="flex items-center gap-1">
        <Button
          tip="Go back to where you came from"
          size="sm"
          variant="ghost"
          aria-label="Back"
          onClick={() => void navigate(-1)}
        >
          <ArrowLeft size={14} aria-hidden="true" /> <span className="hidden sm:inline">Back</span>
        </Button>
        <Button
          tip={HELP["call.prev"]}
          size="sm"
          variant="ghost"
          disabled={detail.prev_call_id === null}
          onClick={() => void navigate(`/calls/${detail.prev_call_id}`)}
        >
          <ChevronLeft size={14} aria-hidden="true" /> Previous
        </Button>
        <Button
          tip={HELP["call.next"]}
          size="sm"
          variant="ghost"
          disabled={detail.next_call_id === null}
          onClick={() => void navigate(`/calls/${detail.next_call_id}`)}
        >
          Next <ChevronRight size={14} aria-hidden="true" />
        </Button>
        <div className="ml-auto flex items-center gap-1">
          {/* Edit controls are only rendered for someone who can use them: a guest saw a
              row of disabled buttons that said nothing but "you can't". */}
          {canWrite ? (
            <Button
              tip={HELP["call.star"]}
              size="sm"
              active={detail.starred}
              aria-pressed={detail.starred}
              onClick={() => updateCall.mutate({ id: detail.id, patch: { starred: !detail.starred } })}
            >
              <Star size={14} aria-hidden="true" fill={detail.starred ? "currentColor" : "none"} /> Star
            </Button>
          ) : null}
          <CallActions
            detail={detail}
            canWrite={canWrite}
            retranscribing={retranscribe.isPending}
            onRetranscribe={() =>
              retranscribe.mutate(
                { id: detail.id },
                {
                  onSuccess: () => toast("Queued for transcription", { tone: "success" }),
                  onError: () => toast("Could not queue it", { tone: "error" }),
                },
              )
            }
          />
        </div>
      </div>

      {/* Title, then the player, then the transcript, then the metadata: what a reader
          came for, in the order they want it, with Play on the first phone screen. */}
      <Card>
        <div className="p-4 pb-3">
          <div className="min-w-0">
            <Tip label={HELP["call.header.talkgroup"]}>
              {/* The page's one heading: the channel this call was on. */}
              <h1 className="text-lg font-semibold text-ink">
                <Link to={`/talkgroups/${detail.talkgroup_id}`} className="hover:underline">
                  {detail.patch_label ?? callTalkgroupLabel(detail)}
                </Link>
              </h1>
            </Tip>
            {/* Three facts as three separate items, not one "A · B · C" string. */}
            <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-2">
              <span className="inline-flex items-center gap-1.5">
                <CategoryIcon category={detail.category} serviceTag={detail.service_tag} />
                {detail.service_tag ?? categoryLabel(detail.category)}
              </span>
              <span className="tabular-nums text-muted">TG {detail.tgid}</span>
              {detail.agency_code ? <span>{agencyName(detail.agency_code)}</span> : null}
            </p>
          </div>
        </div>

        {detail.patch_member_tgids.length ||
        detail.duplicate_of ||
        detail.emergency ||
        detail.encrypted ||
        detail.tone_detected ||
        detail.audio_type ? (
          <div className="flex flex-wrap items-center gap-1.5 border-t border-line px-4 py-2">
            {detail.patch_label ? (
              <span className="text-xs font-medium text-ink-2">{detail.patch_label}</span>
            ) : null}
            {/* Each member of the patch is a link to that channel's own traffic —
                a patch is only interesting next to what its members do apart. */}
            {detail.patch_member_tgids.map((tgid) => (
              <Tip key={tgid} label={HELP["call.header.patch"]}>
                <Link to={`/calls?talkgroup=${tgid}&preset=7d`} className={chipClass(false)}>
                  {tgid}
                  {talkgroupByTgid.get(tgid) ? (
                    <span className="text-[11px] opacity-80">{talkgroupByTgid.get(tgid)}</span>
                  ) : null}
                </Link>
              </Tip>
            ))}
            {detail.duplicate_of ? (
              <StatusChip status="duplicate" tip={HELP["call.header.duplicate"]} />
            ) : null}
            {detail.emergency ? <StatusChip status="failed" tip={HELP["call.header.emergency"]} /> : null}
            {/* A page is the badge a reader is scanning the header for; the tone segments
                behind it are on the chip, because "625 Hz then 750 Hz" is what says which
                agency's encoder sent it. */}
            {detail.tone_detected ? (
              <Tip label={toneTip(detail.audio_meta)}>
                <span
                  className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium ${
                    detail.alert ? "border-warning text-warning-ink" : "border-line text-ink-2"
                  }`}
                >
                  <Bell size={11} aria-hidden="true" fill={detail.alert ? "currentColor" : "none"} />
                  {detail.alert ? "page" : "tone"}
                </span>
              </Tip>
            ) : null}
            {detail.encrypted ? (
              <Tip label={HELP["call.header.encrypted"]}>
                <span className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-[11px] font-medium text-ink-2">
                  <Lock size={11} aria-hidden="true" />
                  encrypted
                </span>
              </Tip>
            ) : null}
            {detail.audio_type ? (
              <Tip label={HELP["call.header.audioType"]}>
                <span className="rounded border border-line px-1.5 py-0.5 text-[11px] text-ink-2">
                  {detail.audio_type}
                </span>
              </Tip>
            ) : null}
          </div>
        ) : null}

        <div className="border-t border-line p-4">
          <div className="mb-2 flex min-h-11 items-center gap-2">
            {/* One player per call: this card only loads the call into the bar at the
                foot of the screen. Once it is there, the bar is the player and the
                waveform is where to seek and scrub. */}
            {isCurrent ? null : (
              <Button
                tip={HELP["player.play"]}
                variant="primary"
                size="icon"
                className="h-11 w-11"
                aria-label="Play this call"
                onClick={() => {
                  select(detail as Call, { play: true });
                  // This button goes away now that the bar is the player; focus moves to
                  // the waveform, which is where this call is controlled from here on.
                  requestAnimationFrame(() => document.getElementById("call-waveform")?.focus());
                }}
              >
                <Play size={18} aria-hidden="true" />
              </Button>
            )}
            <Tip label={HELP["player.elapsed"]}>
              <span className="text-xs tabular-nums text-ink-2" data-testid="call-playhead">
                {isCurrent ? (
                  <>
                    <span className="sr-only">
                      {playing ? "Playing in the player below: " : "In the player below: "}
                    </span>
                    {formatPlayTime(currentTime)} / {formatPlayTime(length)}
                  </>
                ) : (
                  formatPlayTime(length)
                )}
              </span>
            </Tip>
            {isMockMode() ? (
              <span className="text-[11px] text-muted">
                Sample data: the waveform and transcript sync work, but there is no audio to hear.
              </span>
            ) : null}
          </div>
          <Waveform
            peaks={waveform.data?.peaks ?? []}
            duration={length}
            position={isCurrent ? currentTime : 0}
            onSeek={(seconds) => {
              // The first touch on a call that is not loaded yet loads it and plays from
              // there; after that the waveform only moves the playhead.
              if (!isCurrent) select(detail as Call, { play: true });
              seek(seconds);
            }}
            tip={HELP["call.waveform"]}
            id="call-waveform"
          />
        </div>
      </Card>

      <Tabs
        idPrefix="call"
        items={TABS}
        value={tab}
        onChange={setTab}
        label="Call sections"
        listClassName="flex flex-wrap gap-1 border-b border-line pb-2"
      />

      {tab === "transcript" ? (
        <TabPanel idPrefix="call" value="transcript">
          <TranscriptPanel
            segments={detail.transcript?.segments ?? []}
            text={detail.transcript_text}
            quality={detail.transcript_quality}
            reasons={detail.transcript?.quality_reasons ?? []}
            status={detail.transcript_status}
            position={isCurrent ? currentTime : -1}
            onSeek={(seconds) => {
              if (!isCurrent) select(detail as Call, { play: true });
              seek(seconds);
            }}
            canWrite={canWrite}
            onSave={(next) =>
              updateCall.mutate(
                { id: detail.id, patch: { transcript_text: next } },
                {
                  onSuccess: () => toast("Transcript saved", { tone: "success" }),
                  onError: () => toast("Could not save the transcript", { tone: "error" }),
                },
              )
            }
          />
        </TabPanel>
      ) : null}

      {tab === "others" ? (
        <TabPanel idPrefix="call" value="others">
          <Card>
            <CardHeader title="Other transcripts" hint={HELP["call.otherTranscripts"]} />
            {transcripts.isLoading ? (
              <SkeletonText lines={4} className="p-4" />
            ) : (transcripts.data ?? []).length <= 1 ? (
              <EmptyState
                title="Only one transcript"
                hint="Re-transcribing this call on another backend would add a second one here to compare."
              />
            ) : (
              <ul className="divide-y divide-line">
                {(transcripts.data ?? []).map((transcript) => (
                  <li key={transcript.id} className="p-4">
                    <div className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
                      <span className="font-medium text-ink">{transcript.backend_name}</span>
                      <span>{transcript.model}</span>
                      <QualityDot quality={transcript.quality} reasons={transcript.quality_reasons} />
                      {transcript.is_primary ? (
                        <StatusChip status="ok" tip="This is the transcript shown everywhere else" />
                      ) : null}
                      <span className="ml-auto tabular-nums">{formatDateTime(transcript.created_at)}</span>
                    </div>
                    <p className="mt-1.5 text-sm text-ink">{transcript.edited_text ?? transcript.text}</p>
                    <p className="mt-1 text-[11px] tabular-nums text-muted">
                      avg logprob {transcript.avg_logprob} · no-speech {transcript.no_speech_prob} ·
                      compression {transcript.compression_ratio} · {transcript.processing_ms} ms
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </TabPanel>
      ) : null}

      {tab === "metadata" ? (
        <TabPanel idPrefix="call" value="metadata">
          <Card>
            <CardHeader title="Raw metadata" hint="Exactly what the ingest read out of the file." />
            <dl className="grid gap-x-6 gap-y-2 p-4 text-xs sm:grid-cols-2">
              <Row label="File" help="call.raw.path" value={detail.raw.file_path} mono />
              <Row label="Name" help="call.raw.path" value={detail.raw.file_name} mono />
              <Row label="Size" help="call.raw.path" value={formatBytes(detail.raw.file_size)} />
              <Row label="SHA-256" help="call.raw.sha256" value={detail.raw.sha256 ?? "—"} mono />
              <Row
                label="Audio"
                help="call.raw.path"
                value={`${detail.raw.bitrate ?? "?"} bps · ${detail.raw.sample_rate ?? "?"} Hz · ${detail.raw.channels ?? "?"} ch`}
              />
              <Row label="Recorder" help="call.raw.path" value={detail.raw.recorder_version ?? "—"} />
            </dl>
            <div className="border-t border-line p-4">
              {/* One panel, either dialect: an sdrtrunk file's ID3 frames or a
                  trunk-recorder JSON sidecar, whichever this call arrived with. */}
              <h3 className="mb-2 text-xs font-medium text-ink-2">
                {rawMetaTitle(detail.ingest_source)}
                <Tip label={HELP["call.raw.meta"]}>
                  <span className="ml-1 cursor-help text-muted">?</span>
                </Tip>
              </h3>
              {rawEntries.length ? (
                <dl className="space-y-1 font-mono text-[11px]">
                  {rawEntries.map(([key, value]) => (
                    <div key={key} className="flex gap-2">
                      <dt className="w-32 shrink-0 break-all text-muted">{key}</dt>
                      <dd className="min-w-0 break-all text-ink-2">{value || "—"}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                // Written by an older ingest, before the recorder's own metadata was
                // kept verbatim: everything it said is still in the columns above.
                <p className="text-[11px] text-muted">
                  Not retained for this call — everything the recorder wrote is in the fields above and in the
                  talkgroup, radio and patch this call is filed under.
                </p>
              )}
            </div>
          </Card>
        </TabPanel>
      ) : null}

      {tab === "links" ? (
        <TabPanel idPrefix="call" value="links">
          <Card>
            <CardHeader title="External links" hint={HELP["call.links"]} />
            {detail.links.length === 0 ? (
              <EmptyState
                title="Nothing linked to this call"
                hint="Links are created by the correlation API when an incident in another system matches."
              />
            ) : (
              <ul className="divide-y divide-line">
                {detail.links.map((link) => (
                  <li key={link.id} className="flex flex-wrap items-center gap-2 p-3 text-xs">
                    <span className="font-medium text-ink">{link.source}</span>
                    <span className="font-mono text-ink-2">{link.external_id}</span>
                    <StatusChip
                      status={link.link_confidence === "strong" ? "ok" : (link.link_confidence ?? "weak")}
                      tip={`Matched by ${link.link_method ?? "time"}. ${link.link_note ?? ""}`}
                    />
                    <span className="ml-auto text-muted">{formatDateTime(link.created_at)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </TabPanel>
      ) : null}

      {/* The facts about the recording. On a phone they wait behind a disclosure, below
          the transcript; from md up they are open. */}
      <Card>
        <details
          open={detailsOpen}
          onToggle={(event) => setDetailsOpen(event.currentTarget.open)}
          className="group"
        >
          <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-4 py-2 text-sm font-semibold text-ink [&::-webkit-details-marker]:hidden">
            <ChevronRight
              size={14}
              aria-hidden="true"
              className="text-muted transition-transform group-open:rotate-90"
            />
            <h2>Recording details</h2>
            <span className="flex min-w-0 gap-3 truncate text-xs font-normal text-ink-2">
              <span className="truncate">{detail.radio_label ?? "Unresolved radio"}</span>
              <span className="shrink-0 tabular-nums text-muted">{formatCallTime(detail.started_at)}</span>
            </span>
          </summary>
          <div className="grid grid-cols-2 gap-x-6 gap-y-3 border-t border-line p-4 sm:flex sm:flex-wrap sm:items-start">
            <Field label="Radio" help="call.header.radio">
              {detail.radio_id ? (
                <Link to={`/radios/${detail.radio_id}`} className="text-ink hover:underline">
                  {detail.radio_label}
                </Link>
              ) : (
                <span className="text-muted">Unresolved</span>
              )}
              {/* Who that radio belongs to, from the roster. Fetched from the radio's own
                  detail rather than added to the call payload: the calls list is the hot
                  query in this application and must not grow a join for a label only the
                  detail page shows. */}
              {identity ? (
                <Tip label={HELP["call.header.identity"]}>
                  <span className="block text-[11px] text-muted">{identity}</span>
                </Tip>
              ) : null}
            </Field>

            <Field label="Started" help="call.header.time">
              <span className="tabular-nums">{formatCallTime(detail.started_at)}</span>
              <span className="block text-[11px] text-muted">{formatUtc(detail.started_at)}</span>
              {/* Beside the time, because the time is what it qualifies: this clock says one
                  thing and the recording only reached the archive long afterwards. Muted and
                  wordless — nothing is wrong with the row, and a warning colour would say so. */}
              {isBackfilled(detail) ? (
                <Tip label={HELP["calls.table.backfilled"]}>
                  <span className="mt-0.5 inline-block rounded bg-surface-2 px-1 text-[11px] font-medium text-muted">
                    backfilled
                  </span>
                </Tip>
              ) : null}
            </Field>

            <Field label="Length" help="call.header.duration">
              <span className="tabular-nums">{formatPlayTime(length)}</span>
            </Field>

            <Field label="Frequency" help="call.header.frequency">
              <span className="tabular-nums">{formatFrequency(detail.frequency_hz)}</span>
            </Field>

            <Field label="Source" help="call.header.site">
              {/* "System / Site", never the site alone: two systems can both have a "Springfield". */}
              {siteLabel({ name: detail.site_name ?? "—", system_name: detail.system_name })}
            </Field>

            {/* How the time was established and which audio chain ran are how the archive
                works, not facts about the call: shown once signed in, not to a guest. */}
            {canWrite ? (
              <Field label="Time source" help="call.header.timeSource">
                <StatusChip
                  status={detail.time_source}
                  label={timeSourceLabel(detail.time_source)}
                  tip={HELP["call.header.timeSource"]}
                />
              </Field>
            ) : null}

            {/* Which chain produced the file on disk. Worth a line of its own because it is
                the first question asked about a recording that sounds wrong, and the answer
                — a profile, or the standard chain — decides where the fix belongs. */}
            {canWrite ? (
              <Field label="Audio profile" help="call.header.audioProfile">
                <span>{audioProfileLabel(audioProfileName(detail.audio_meta))}</span>
                {/* Only an admin can edit a profile, so only an admin is offered the way in;
                  the link carries the call so the editor's preview opens on this recording. */}
                {isAdmin ? (
                  <Tip label={HELP["call.tune"]}>
                    <Link
                      to={`/admin?tab=audio&call=${detail.id}`}
                      className="mt-0.5 inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
                    >
                      <SlidersHorizontal size={11} aria-hidden="true" /> Tune…
                    </Link>
                  </Tip>
                ) : null}
              </Field>
            ) : null}
          </div>
          {/* Which recorder and how clean its decode was: operator detail, signed in only. */}
          {canWrite ? (
            <div className="border-t border-line px-4 py-2">
              <SourceFact instance={detail.source_instance} quality={detail.source_quality} />
            </div>
          ) : null}
        </details>
      </Card>

      {detail.sources.length > 1 ? (
        <Card>
          <CardHeader title="Who keyed up" hint={HELP["call.sources"]} />
          <ol className="divide-y divide-line" aria-label="Radios that keyed up">
            {detail.sources.map((source) => (
              <li key={source.id}>
                {/* The offset is the point of the list, so the whole row seeks to it —
                    "the second unit" is otherwise a guess with a scrub bar. */}
                <Tip label={HELP["call.sources.seek"]}>
                  <button
                    type="button"
                    className={cn(
                      "flex w-full items-center gap-3 px-4 py-2 text-left text-xs hover:bg-surface-2",
                      isCurrent &&
                        currentTime >= source.pos_s &&
                        (source.len_s === null || currentTime < source.pos_s + source.len_s) &&
                        "bg-surface-2",
                    )}
                    onClick={() => {
                      if (!isCurrent) select(detail as Call, { play: true });
                      seek(source.pos_s);
                    }}
                  >
                    <span className="w-12 shrink-0 tabular-nums text-muted">{source.pos_s.toFixed(1)}s</span>
                    {source.radio_id ? (
                      <Link
                        to={`/radios/${source.radio_id}`}
                        className="font-medium text-ink hover:underline"
                        onClick={(event) => event.stopPropagation()}
                      >
                        {source.radio_label ?? source.src}
                      </Link>
                    ) : (
                      <span className="font-medium text-ink">{source.radio_label ?? source.src ?? "—"}</span>
                    )}
                    {source.emergency ? (
                      <StatusChip status="failed" tip={HELP["call.sources.emergency"]} />
                    ) : null}
                    <span className="ml-auto tabular-nums text-muted">
                      {source.len_s === null ? "" : `${source.len_s.toFixed(1)}s`}
                    </span>
                  </button>
                </Tip>
              </li>
            ))}
          </ol>
        </Card>
      ) : null}

      {/* Two recorders cover this system, so one transmission can land twice; reconciliation
          keeps the better copy here and hides the rest behind it (docs/RECONCILIATION.md).
          The score is the number the decision was actually made on. */}
      {detail.duplicates.length > 0 ? (
        <Card>
          <CardHeader title="Other copies" hint={HELP["call.duplicates"]} />
          <ul className="divide-y divide-line" aria-label="Other copies of this transmission">
            {detail.duplicates.map((copy) => (
              <li key={copy.id} className="flex flex-wrap items-center gap-3 px-4 py-2 text-xs">
                <Link to={`/calls/${copy.id}`} className="font-medium text-ink hover:underline">
                  #{copy.id}
                </Link>
                <StatusChip status={copy.ingest_source} tip={HELP["call.duplicates.recorder"]} />
                <span className="tabular-nums text-muted">{formatDuration(copy.duration_s)}</span>
                <span className="text-muted">{formatCallTime(copy.started_at)}</span>
                <StatusChip status={copy.time_source} tip={HELP["call.header.timeSource"]} />
                {copy.audio_state === "present" ? null : (
                  <StatusChip status={copy.audio_state} tip={HELP["call.duplicates.noAudio"]} />
                )}
                <Tip label={HELP["call.duplicates.score"]}>
                  <span className="ml-auto tabular-nums text-muted">{copy.score.toFixed(0)}</span>
                </Tip>
                {canWrite ? (
                  <GuardedButton
                    size="sm"
                    variant="ghost"
                    canWrite={canWrite}
                    tip={HELP["call.duplicates.makePrimary"]}
                    disabledTip={HELP["app.readOnly"]}
                    disabled={makePrimary.isPending}
                    onClick={() => makePrimary.mutate(copy.id)}
                  >
                    Make primary
                  </GuardedButton>
                ) : null}
              </li>
            ))}
          </ul>
          {detail.reconcile_reason ? (
            <p className="border-t border-line px-4 py-2 text-xs text-muted">
              This is the copy the archive shows, decided on{" "}
              <span className="font-medium text-ink">{String(detail.reconcile_reason.rule)}</span>.
            </p>
          ) : null}
        </Card>
      ) : null}

      {/* Notes and tags are an operator's working space; a guest has nothing to do there. */}
      {canWrite ? (
        <NotesAndTags
          callId={detail.id}
          notes={detail.notes}
          tags={detail.tags}
          canWrite={canWrite}
          onSave={(patch) =>
            updateCall.mutate(
              { id: detail.id, patch },
              {
                onSuccess: () => toast("Saved", { tone: "success" }),
                onError: () => toast("Could not save", { tone: "error" }),
              },
            )
          }
        />
      ) : null}

      <Card>
        <CardHeader title="This exchange" hint={HELP["call.conversation"]} />
        {conversation.isLoading ? (
          <SkeletonText lines={3} className="p-4" />
        ) : thread.length === 0 ? (
          <EmptyState
            title="Not part of an exchange"
            hint="Calls are grouped into a conversation when they follow each other on one talkgroup within a few seconds."
          />
        ) : (
          <ul className="divide-y divide-line">
            {thread.map((other) => (
              <li key={other.id}>
                <Link
                  to={`/calls/${other.id}`}
                  className={cn(
                    "flex items-center gap-3 px-4 py-2 text-sm hover:bg-surface-2",
                    other.id === detail.id && "bg-accent-soft",
                  )}
                >
                  <span className="w-36 shrink-0 tabular-nums text-ink-2">
                    {formatCallTime(other.started_at)}
                  </span>
                  <span className="w-24 shrink-0 truncate text-ink-2">{other.radio_label ?? "—"}</span>
                  <span className="min-w-0 flex-1 truncate text-ink">
                    {other.transcript_text ?? <span className="text-muted">{other.transcript_status}</span>}
                  </span>
                  <span className="shrink-0 tabular-nums text-muted">{formatDuration(other.duration_s)}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

/**
 * Everything one does *with* a call, in one menu: copying, the audio file, and — for
 * someone who can — re-transcribing and deleting. Star stays a button of its own.
 */
function CallActions({
  detail,
  canWrite,
  retranscribing,
  onRetranscribe,
}: {
  detail: Call & { transcript?: { text?: string | null } | null };
  canWrite: boolean;
  retranscribing: boolean;
  onRetranscribe: () => void;
}) {
  const [open, setOpen] = useState(false);
  const item = "w-full justify-start";
  const run = (action: () => void) => () => {
    setOpen(false);
    action();
  };
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      align="end"
      className="w-56 p-1.5"
      trigger={
        <Button tip={HELP["call.actions"]} size="sm" variant="ghost" aria-label="More actions for this call">
          <MoreHorizontal size={16} aria-hidden="true" />
          <span className="hidden sm:inline">Actions</span>
        </Button>
      }
    >
      <div className="flex flex-col gap-0.5">
        <Button
          tip={HELP["call.copyLink"]}
          tipSide="left"
          size="sm"
          variant="ghost"
          className={item}
          onClick={run(() => {
            const url = `${window.location.origin}/calls/${detail.id}`;
            void copyAndToast(url, { label: "Link", body: url });
          })}
        >
          <Link2 size={14} aria-hidden="true" /> Copy link
        </Button>
        <Button
          tip={HELP["call.copySummary"]}
          tipSide="left"
          size="sm"
          variant="ghost"
          className={item}
          onClick={run(
            () =>
              void copyAndToast(
                // The detail row carries the whole transcript; the table row carries only
                // the snippet, so the same formatter says more here — by design.
                callSummary({
                  ...detail,
                  transcript_text: detail.transcript?.text ?? detail.transcript_text,
                }),
                { label: "Call summary" },
              ),
          )}
        >
          <ClipboardCopy size={14} aria-hidden="true" /> Copy summary
        </Button>
        <Button
          tip={HELP["call.openAudio"]}
          tipSide="left"
          size="sm"
          variant="ghost"
          className={item}
          onClick={run(() => window.open(apiUrl(`/calls/${detail.id}/audio`), "_blank", "noreferrer"))}
        >
          <Download size={14} aria-hidden="true" /> Open audio file
        </Button>
        {canWrite ? (
          <>
            <Button
              tip={HELP["call.retranscribe"]}
              tipSide="left"
              size="sm"
              variant="ghost"
              className={item}
              disabled={retranscribing}
              onClick={run(onRetranscribe)}
            >
              <RefreshCw size={14} aria-hidden="true" /> Re-transcribe
            </Button>
            <Button
              tip={HELP["call.delete"]}
              tipSide="left"
              size="sm"
              variant="ghost"
              className={item}
              onClick={run(() => toast("Deleting is an admin action and is disabled in this build"))}
            >
              <Trash2 size={14} aria-hidden="true" /> Delete…
            </Button>
          </>
        ) : null}
      </div>
    </Popover>
  );
}

/**
 * Which receiver's recording this is, in one line.
 *
 * It appears only when the recorder actually said — which is a minority of calls and every
 * call once more than one receiver is pushing — because the fact is only interesting where
 * there was a choice. The frame counts are what the choice was made on (docs/RECORDERS.md,
 * "Best source"), so they are shown rather than summarized into a verdict the page would
 * have to invent.
 */
export function SourceFact({
  instance,
  quality,
}: {
  instance: string | null;
  quality: CallSourceQuality | null;
}) {
  const name = instance ?? (typeof quality?.instance === "string" ? quality.instance : null);
  if (!name) return null;
  // Separate items with space between them, not one "A · B · C" string.
  const parts: string[] = [`Recorded by ${name}`];
  if (typeof quality?.voice_frames === "number") {
    parts.push(`${formatNumber(quality.voice_frames)} voice frames`);
  }
  if (typeof quality?.bad_frames === "number") parts.push(`${formatNumber(quality.bad_frames)} bad frames`);
  if (typeof quality?.rssi_dbfs === "number") parts.push(`Signal ${quality.rssi_dbfs.toFixed(1)} dBFS`);
  return (
    <Tip label={HELP["call.source"]}>
      <p className="flex flex-wrap gap-x-3 text-xs text-muted">
        {parts.map((part) => (
          <span key={part} className="tabular-nums">
            {part}
          </span>
        ))}
      </p>
    </Tip>
  );
}

function Field({
  label,
  help,
  children,
}: {
  label: string;
  help: keyof typeof HELP;
  children: React.ReactNode;
}) {
  return (
    <Tip label={HELP[help]}>
      <div className="min-w-0">
        <p className="text-xs text-muted">{label}</p>
        <div className="text-sm text-ink-2">{children}</div>
      </div>
    </Tip>
  );
}

function Row({
  label,
  help,
  value,
  mono,
}: {
  label: string;
  help: keyof typeof HELP;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <Tip label={HELP[help]}>
        <dt className="text-xs text-muted">{label}</dt>
      </Tip>
      <dd className={cn("break-all text-ink-2", mono && "font-mono text-[11px]")}>{value}</dd>
    </div>
  );
}

/** The transcript, with the line under the playhead marked. */
function TranscriptPanel({
  segments,
  text,
  quality,
  reasons,
  status,
  position,
  onSeek,
  canWrite,
  onSave,
}: {
  segments: TranscriptSegment[];
  text: string | null;
  quality: string | null;
  reasons: string[];
  status: string;
  position: number;
  onSeek: (seconds: number) => void;
  canWrite: boolean;
  onSave: (text: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text ?? "");
  const activeIndex = useMemo(
    () => segments.findIndex((segment) => position >= segment.start && position < segment.end),
    [segments, position],
  );

  return (
    <Card>
      <CardHeader
        title="Transcript"
        hint={HELP["call.transcript"]}
        actions={
          <>
            <QualityDot quality={quality as never} reasons={reasons} />
            <StatusChip status={status} tip={HELP["calls.filter.transcriptStatus"]} />
            {editing ? (
              <>
                <Button
                  tip={HELP["call.transcript.cancel"]}
                  size="sm"
                  variant="ghost"
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </Button>
                <Button
                  tip={HELP["call.transcript.save"]}
                  size="sm"
                  variant="primary"
                  onClick={() => {
                    onSave(draft);
                    setEditing(false);
                  }}
                >
                  Save
                </Button>
              </>
            ) : canWrite ? (
              <Button
                tip={HELP["call.transcript.edit"]}
                size="sm"
                onClick={() => {
                  setDraft(text ?? "");
                  setEditing(true);
                }}
              >
                Correct
              </Button>
            ) : null}
          </>
        }
      />
      <div className="p-4">
        {editing ? (
          <TextArea
            value={draft}
            rows={5}
            aria-label="Corrected transcript"
            onChange={(event) => setDraft(event.target.value)}
          />
        ) : segments.length ? (
          <ol className="space-y-1">
            {segments.map((segment, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: a segment's identity is its position in the transcript; two silent segments can share a start time.
              <li key={`${segment.start}-${index}`}>
                <Tip label={HELP["call.transcript.segment"]}>
                  <button
                    type="button"
                    onClick={() => onSeek(segment.start)}
                    className={cn(
                      "flex w-full gap-3 rounded px-2 py-1 text-left text-sm hover:bg-surface-2",
                      index === activeIndex && "segment-active",
                    )}
                  >
                    <span className="w-12 shrink-0 tabular-nums text-[11px] text-muted">
                      {segment.start.toFixed(1)}s
                    </span>
                    <span className="min-w-0 flex-1 text-ink">{segment.text}</span>
                  </button>
                </Tip>
              </li>
            ))}
          </ol>
        ) : text ? (
          <p className="text-sm text-ink">{text}</p>
        ) : (
          <EmptyState title="No transcript" hint={HELP["call.transcript.empty"]} />
        )}
        {reasons.length ? (
          <p className="mt-3 text-[11px] text-ink-2">
            <span className="font-medium">Quality flags:</span> {reasons.join("; ")}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function NotesAndTags({
  callId,
  notes,
  tags,
  canWrite,
  onSave,
}: {
  callId: number;
  notes: string;
  tags: string[];
  canWrite: boolean;
  onSave: (patch: { notes?: string; tags?: string[] }) => void;
}) {
  const [draft, setDraft] = useState(notes);
  const [tagDraft, setTagDraft] = useState("");
  // A new call means new notes; without this the previous call's text would stay.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `callId` is listed deliberately — navigating between two calls whose notes are both empty must still reset the box.
  useEffect(() => {
    setDraft(notes);
  }, [notes, callId]);

  return (
    <Card>
      <CardHeader
        title="Your notes and tags"
        hint="Notes are searchable alongside the transcript; tags are filterable."
        actions={
          <Button
            tip={canWrite ? HELP["call.notes.save"] : HELP["app.readOnly"]}
            size="sm"
            variant="primary"
            disabled={!canWrite || draft === notes}
            onClick={() => onSave({ notes: draft })}
          >
            Save notes
          </Button>
        }
      />
      <div className="space-y-3 p-4">
        <TextArea
          value={draft}
          rows={3}
          disabled={!canWrite}
          aria-label="Notes about this call"
          placeholder={canWrite ? "What was happening here?" : "Sign in to add notes"}
          onChange={(event) => setDraft(event.target.value)}
        />
        <div className="flex flex-wrap items-center gap-1.5">
          {tags.map((tag) => (
            <Chip
              key={tag}
              label={tag}
              tip={canWrite ? HELP["call.tags.remove"] : HELP["app.readOnly"]}
              active
              onClick={() => canWrite && onSave({ tags: tags.filter((value) => value !== tag) })}
            />
          ))}
          <Tip label={canWrite ? HELP["call.tags"] : HELP["app.readOnly"]}>
            <TextInput
              value={tagDraft}
              disabled={!canWrite}
              aria-label="Add a tag"
              placeholder="Add a tag…"
              className="h-8 w-40"
              onChange={(event) => setTagDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Enter" || !tagDraft.trim()) return;
                event.preventDefault();
                onSave({ tags: [...new Set([...tags, tagDraft.trim()])] });
                setTagDraft("");
              }}
            />
          </Tip>
        </div>
      </div>
    </Card>
  );
}
