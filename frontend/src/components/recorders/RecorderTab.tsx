import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  FileArchive,
  Link2,
  MapPin,
  PauseCircle,
  Radio,
  RadioTower,
  Settings,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { errorDetail } from "../../api/client";
import {
  eventLogDownloadUrl,
  KEYS,
  useCurrentUser,
  useEventLogs,
  useForgetRecorder,
  useOccupancy,
  useRecorders,
  useSaveRecorderPolicy,
  useSites,
} from "../../api/hooks";
import { useRecorderStream } from "../../api/live";
import {
  RECORDER_STANDBY,
  type Recorder,
  type RecorderCounters,
  type RecorderLink,
  type RecorderStandby,
  type RecorderStation,
  type RecorderUplink,
  type Tuner,
  type TunerDescription,
  type TunerGain,
  tunerGain,
} from "../../api/types";
import { HELP } from "../../help/help";
import { bandList, formatBands, sameBands } from "../../lib/bands";
import { formatNumber, formatRelative, siteLabel } from "../../lib/format";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Card, CardHeader } from "../ui/Card";
import { StatusChip } from "../ui/Chip";
import { Dialog } from "../ui/Dialog";
import { TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { SkeletonText } from "../ui/Skeleton";
import { EmptyState, ErrorState } from "../ui/States";
import { Tip } from "../ui/Tooltip";
import { BandStrip, BandStripLegend, TunerBandStrip } from "./BandStrip";
import { ConnectRecorderCard } from "./ConnectRecorderCard";
import { RecorderSettingsDialog } from "./RecorderSettingsDialog";
import { SpectrumView } from "./SpectrumView";
import { TunerEditDialog } from "./TunerEditDialog";

/** A recorder counts as online if it has been heard from within this window. */
const ONLINE_MS = 90_000;

/**
 * How long since a recorder last *reported* before its self-description is called stale.
 *
 * Deliberately the same 60 seconds as the arbiter's default staleness threshold, and
 * deliberately read against `reported_at` rather than `last_seen_at`: that is the clock the
 * arbiter fails a recorder over, so what the page calls stale and what keyup acts on are
 * the same fact. (`last_seen_at` moves on a bare desired-state poll, which proves the
 * process is up and nothing about whether it is hearing anything.)
 */
const REPORT_STALE_MS = 60_000;

/** The color and the words for each standby class — the badge and the select share them. */
const STANDBY_TONE: Record<RecorderStandby, string> = {
  hot: "border-good text-good-ink",
  warm: "border-warning text-warning-ink",
  cold: "border-line text-ink-2",
};

const STANDBY_TIP: Record<RecorderStandby, string> = {
  hot: "Records whenever it is healthy",
  warm: "A spare: tuners off until keyup promotes it because the hot recorders went quiet",
  cold: "Never switched on automatically — a bench box keyup keeps its hands off",
};

const MHz = (hz: number | null | undefined): string =>
  hz === null || hz === undefined || !Number.isFinite(hz) ? "—" : (hz / 1e6).toFixed(4);

/** "845.5–865.5 MHz" from a center and a span. */
function spanLabel(centerHz: unknown, spanHz: unknown): string {
  if (typeof centerHz !== "number" || typeof spanHz !== "number" || spanHz <= 0) return "—";
  const lo = (centerHz - spanHz / 2) / 1e6;
  const hi = (centerHz + spanHz / 2) / 1e6;
  return `${lo.toFixed(1)}–${hi.toFixed(1)} MHz`;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A string worth rendering: present, and not just whitespace. */
function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

/**
 * The wideband level meter's scale, in dBFS.
 *
 * The figure is ADC-referenced, not dBm: it moves with every gain setting, so the meter is
 * read against the gain summary beside it and never as a signal strength. The two
 * thresholds are what an operator actually acts on — below {@link RSSI_QUIET} the front end
 * is hearing almost nothing and wants more gain or a better antenna; above {@link RSSI_HOT}
 * it is close enough to full scale that the converter is compressing and wants *less*.
 */
const RSSI_FLOOR = -110;
const RSSI_CEILING = 0;
const RSSI_QUIET = -80;
const RSSI_HOT = -25;

type LevelBand = "quiet" | "good" | "hot";

function levelBand(dbfs: number): LevelBand {
  if (dbfs > RSSI_HOT) return "hot";
  if (dbfs < RSSI_QUIET) return "quiet";
  return "good";
}

const LEVEL_FILL: Record<LevelBand, string> = {
  quiet: "bg-line-strong",
  good: "bg-good",
  hot: "bg-warning",
};

const LEVEL_TIP: Record<LevelBand, string> = {
  quiet: "Quiet: the front end is barely hearing anything at this gain",
  good: "A healthy level for this gain setting",
  hot: "Hot: close to full scale, and the converter may be compressing — try less gain",
};

/** "LNA 24 · VGA 20 · amp off" — the gain an RSSI figure has to be read against. */
function gainSummary(gain: TunerGain | null | undefined): string | null {
  if (!gain) return null;
  const parts: string[] = [];
  if (num(gain.gain) !== null) parts.push(`gain ${formatNumber(gain.gain)}`);
  if (num(gain.lna_db) !== null) parts.push(`LNA ${formatNumber(gain.lna_db)}`);
  if (num(gain.vga_db) !== null) parts.push(`VGA ${formatNumber(gain.vga_db)}`);
  if (typeof gain.amp === "boolean") parts.push(`amp ${gain.amp ? "on" : "off"}`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** Bytes as whole GiB — the unit a disk-space figure is actually discussed in. */
const GIB = 1024 ** 3;
const MIB = 1024 ** 2;

/** Below this much free space the recorder is close to having nowhere to spool. */
const LOW_DISK_BYTES = 5 * GIB;

/** How long to coalesce a burst of tuner.status frames before one refetch. */
const REFETCH_DEBOUNCE_MS = 800;
/** Ceiling on how often a stream of tuner.status frames may refetch the site list. */
const SITES_REFRESH_MS = 60_000;

/** Admin → Recorder: the recorders keyup manages, their tuners, and the band occupancy. */
export function RecorderTab() {
  const queryClient = useQueryClient();
  // The edit button gates on the admin scope, because the PUT needs `admin`, not just write.
  const { isAdmin } = useCurrentUser();
  const recorders = useRecorders();
  const sites = useSites();
  const occupancy = useOccupancy();
  const [editing, setEditing] = useState<{ recorder: Recorder; tuner: Tuner } | null>(null);

  // Live: a tuner.status frame carries only ids. A busy recorder can push many a second, so
  // coalesce a burst into one refetch rather than invalidating on every frame.
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sitesRefreshedAt = useRef(0);
  useRecorderStream(true, () => {
    if (debounce.current) return;
    debounce.current = setTimeout(() => {
      debounce.current = null;
      void queryClient.invalidateQueries({ queryKey: KEYS.recorders });
      // A status push carries observations, and an observation of a tower keyup has never
      // heard of *creates a site*. The tuner rows below name their focus site out of this
      // list, so without refreshing it a brand-new site reads as "Site 42" until reload.
      //
      // But a busy recorder pushes tuner.status continuously, and the 800 ms debounce
      // above only coalesces a burst — it does not bound the rate. With this tab open,
      // `/api/sites` was being refetched several times a minute *per browser*, and that
      // endpoint counts calls per site across the whole archive. Two open tabs kept about
      // ten of those queries in flight at once and took Postgres to 294 % CPU.
      //
      // A brand-new site is a rare event and a minute-late name on a tuner row is not
      // worth that, so the list is refreshed at most once a minute. Everything that
      // *edits* a site still invalidates immediately (`invalidateSites` in api/hooks).
      const now = Date.now();
      if (now - sitesRefreshedAt.current >= SITES_REFRESH_MS) {
        sitesRefreshedAt.current = now;
        void queryClient.invalidateQueries({ queryKey: ["sites"] });
      }
    }, REFETCH_DEBOUNCE_MS);
  });
  useEffect(
    () => () => {
      if (debounce.current) clearTimeout(debounce.current);
    },
    [],
  );

  // "System / Site", not the bare name: a tuner focused on "Springfield" says nothing when two
  // systems each have one, and a recorder is exactly where that mistake gets made.
  const siteName = useMemo(() => {
    const map = new Map<number, string>();
    for (const site of sites.data ?? []) map.set(site.id, siteLabel(site));
    return map;
  }, [sites.data]);

  if (recorders.isLoading) return <SkeletonText lines={8} />;
  if (recorders.error) return <ErrorState error={recorders.error} onRetry={() => void recorders.refetch()} />;

  const rows = recorders.data ?? [];

  return (
    <div className="space-y-4">
      {rows.length === 0 ? (
        <EmptyState
          title="No recorders yet"
          hint="A recorder appears here the first time it polls keyup with a token carrying the upload scope. Mint one below and paste the configuration it prints into the recorder."
          icon={<RadioTower size={28} />}
        />
      ) : (
        rows.map((recorder) => (
          <RecorderCard
            key={recorder.id}
            recorder={recorder}
            siteName={siteName}
            canAdmin={isAdmin}
            onEdit={(tuner) => setEditing({ recorder, tuner })}
          />
        ))
      )}

      <ConnectRecorderCard canAdmin={isAdmin} />

      {(occupancy.data ?? []).length > 0 ? (
        <Card>
          <CardHeader
            title="Band occupancy"
            hint={HELP["admin.recorder.occupancy"]}
            actions={<BandStripLegend />}
          />
          <div className="space-y-4 p-4">
            {(occupancy.data ?? []).map((row) => (
              <div key={row.site_id}>
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span className="font-medium text-ink">
                    {siteName.get(row.site_id) ?? row.site_name ?? `Site ${row.site_id}`}
                  </span>
                  <span className="text-ink-2">
                    {row.channels.length} channels · observed {formatRelative(row.observed_at)}
                  </span>
                </div>
                <BandStrip occupancy={row} />
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {editing ? (
        <TunerEditDialog
          recorder={editing.recorder}
          tuner={editing.tuner}
          sites={sites.data ?? []}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </div>
  );
}

function RecorderCard({
  recorder,
  siteName,
  canAdmin,
  onEdit,
}: {
  recorder: Recorder;
  siteName: Map<number, string>;
  canAdmin: boolean;
  onEdit: (tuner: Tuner) => void;
}) {
  const online = recorder.last_seen_at
    ? Date.now() - new Date(recorder.last_seen_at).getTime() <= ONLINE_MS
    : false;
  // Which tuner's spectrum panel is expanded, if any. The stream opens only while a panel
  // is open, so nothing streams when the row is collapsed.
  const [openSpectrum, setOpenSpectrum] = useState<number | null>(null);
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Radio size={15} aria-hidden="true" /> {recorder.label ?? recorder.instance_id}
          </span>
        }
        hint={
          <span className="tabular-nums">
            {recorder.instance_id} · {recorder.recorder_version ?? "unknown version"} · desired v
            {recorder.version} · last seen {formatRelative(recorder.last_seen_at)}
          </span>
        }
        actions={
          <span className="flex items-center gap-2">
            <StandbyBadge standby={recorder.standby} promotedAt={recorder.promoted_at} />
            <StatusChip
              status={online ? "healthy" : "degraded"}
              tip={online ? "Heard from within the last 90 seconds" : "No status in the last 90 seconds"}
            />
          </span>
        }
      />
      <RecorderPolicyStrip recorder={recorder} canAdmin={canAdmin} />
      <UplinkStrip recorder={recorder} canAdmin={canAdmin} />
      <LinkStrip link={recorder.link} />
      <StationPanel station={recorder.station} reportedAt={recorder.reported_at} />
      {recorder.tuners.length === 0 ? (
        <p className="p-4 text-xs text-ink-2">This recorder has not reported any tuners yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[62rem] text-left text-xs">
            <thead className="text-ink-2">
              <tr className="border-b border-line">
                <th className="px-3 py-2 font-medium">Tuner</th>
                <th className="px-3 py-2 font-medium">Hardware</th>
                <th className="px-3 py-2 font-medium">Span</th>
                <th className="px-3 py-2 font-medium">Role</th>
                <th className="px-3 py-2 font-medium">Focus</th>
                <th className="px-3 py-2 font-medium">State</th>
                <th className="px-3 py-2 font-medium">Level (dBFS)</th>
                <th className="px-3 py-2 font-medium">Overflow</th>
                <th className="px-3 py-2">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {recorder.tuners.map((tuner) => (
                <TunerRow
                  key={tuner.id}
                  tuner={tuner}
                  siteName={siteName}
                  canAdmin={canAdmin}
                  onEdit={() => onEdit(tuner)}
                  spectrumOpen={openSpectrum === tuner.id}
                  onToggleSpectrum={() =>
                    setOpenSpectrum((current) => (current === tuner.id ? null : tuner.id))
                  }
                  spectrumPanel={
                    openSpectrum === tuner.id ? (
                      <SpectrumView
                        instanceId={recorder.instance_id}
                        tunerKey={tuner.tuner_key}
                        spectrumEnabled={tuner.desired?.spectrum ?? false}
                      />
                    ) : null
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <CountersStrip counters={recorder.counters} />
      <EventLogPanel recorder={recorder} canAdmin={canAdmin} />
    </Card>
  );
}

/**
 * "Link: 2 peers" and what each of them is doing.
 *
 * The whole `link` object is opaque on the wire — keyup stores what the recorder sends and
 * never decides anything from it — so this renders the keys it recognizes and quietly
 * ignores the rest. A recorder that reports no peers renders nothing, exactly as one that
 * reports no station does: an empty row would say "no peers" on every single-recorder
 * deployment, which is every deployment that has never heard of linking.
 */
export function LinkStrip({ link }: { link: RecorderLink | null }) {
  const peers = Array.isArray(link?.peers) ? link.peers : [];
  if (peers.length === 0) return null;
  return (
    <div className="flex flex-wrap items-start gap-x-4 gap-y-1.5 border-b border-line px-4 py-2.5">
      <Tip label={HELP["admin.recorder.link"]}>
        <span className="flex items-center gap-1.5 text-xs text-ink">
          <Link2 size={13} aria-hidden="true" className="text-ink-2" />
          {`Link: ${peers.length} peer${peers.length === 1 ? "" : "s"}`}
        </span>
      </Tip>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-2">
        {peers.map((peer, index) => (
          <li key={text(peer.instance) ?? index} className="flex items-center gap-1.5">
            <StatusChip
              status={peer.alive ? "healthy" : "degraded"}
              tip={peer.alive ? "This peer is alive" : "This peer is not answering"}
            />
            <span className="text-ink">{text(peer.instance) ?? "unnamed peer"}</span>
            <span>seen {formatRelative(text(peer.last_seen))}</span>
            <span className="tabular-nums">{claimsLabel(peer.claims)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * What a peer currently claims, in words.
 *
 * The recorder decides what a claim *is* — a site, a tuner, a whole system — so this reads a
 * list of strings or numbers and otherwise says how many there are rather than inventing a
 * rendering for a shape it has not seen.
 */
function claimsLabel(claims: unknown): string {
  if (!Array.isArray(claims)) return "";
  if (claims.length === 0) return "no claims";
  const words = claims.filter((c): c is string | number => typeof c === "string" || typeof c === "number");
  return words.length === claims.length ? `claims ${words.join(", ")}` : `${claims.length} claims`;
}

/**
 * The days of raw event log this recorder has shipped, and how far behind it says it is.
 *
 * Two sources, deliberately both shown: `recorder.event_log` is the *recorder's* account of
 * its backlog, and the list under it is what actually arrived here. The two disagreeing is
 * the fact worth seeing — a recorder that says it is caught up while nothing has landed for
 * a week has an upload problem, and neither figure alone would say so.
 *
 * The list is admin-only because the endpoint is. A non-admin sees the recorder's own
 * summary and no list, rather than an error.
 */
export function EventLogPanel({ recorder, canAdmin }: { recorder: Recorder; canAdmin: boolean }) {
  const status = recorder.event_log;
  const logs = useEventLogs(recorder.instance_id, canAdmin);
  const rows = logs.data ?? [];
  const pending = num(status?.pending);
  const lastDate = text(status?.last_date);
  if (!status && rows.length === 0) return null;

  return (
    <div className="border-t border-line px-4 py-2.5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <Tip label={HELP["admin.recorder.eventLogs"]}>
          <span className="flex items-center gap-1.5 text-xs text-ink">
            <FileArchive size={13} aria-hidden="true" className="text-ink-2" /> Event logs
          </span>
        </Tip>
        {pending !== null ? (
          <span className={`text-[11px] ${pending > 0 ? "text-warning-ink" : "text-ink-2"}`}>
            {pending === 0
              ? "nothing pending"
              : `${formatNumber(pending)} day${pending === 1 ? "" : "s"} pending`}
          </span>
        ) : null}
        {lastDate ? <span className="text-[11px] text-ink-2">last shipped {lastDate}</span> : null}
      </div>
      {canAdmin ? (
        rows.length === 0 ? (
          <p className="mt-1 text-[11px] text-ink-2">Nothing archived here yet.</p>
        ) : (
          <ul className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
            {rows.map((row) => (
              <li key={row.id} className="flex items-center gap-1.5">
                <a
                  href={eventLogDownloadUrl(row.id)}
                  className="tabular-nums text-accent-ink underline decoration-dotted underline-offset-2"
                >
                  {row.date}
                </a>
                <span className="tabular-nums text-ink-2">{formatBytes(row.bytes)}</span>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </div>
  );
}

/** Bytes at the granularity a log file is discussed in — MiB, or KiB when it is tiny. */
function formatBytes(bytes: number): string {
  if (bytes >= GIB) return `${(bytes / GIB).toFixed(1)} GiB`;
  if (bytes >= MIB) return `${(bytes / MIB).toFixed(1)} MiB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KiB`;
}

/**
 * What this recorder is *for*, at a glance: its standby class, and whether keyup is
 * currently the reason it is running.
 *
 * The promoted badge is the one that matters operationally — a spare that is recording
 * because the arbiter turned it on looks exactly like a spare somebody turned on by hand,
 * and the difference decides whether it will stand itself down again.
 */
export function StandbyBadge({
  standby,
  promotedAt,
}: {
  standby: RecorderStandby;
  promotedAt: string | null;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <Tip label={STANDBY_TIP[standby]}>
        <span
          className={`rounded-full border px-2 py-0.5 text-[11px] ${STANDBY_TONE[standby]}`}
          data-testid="standby-badge"
        >
          {standby}
        </span>
      </Tip>
      {promotedAt ? (
        <Tip label={HELP["admin.recorder.promoted"]}>
          <span className="rounded-full border border-warning px-2 py-0.5 text-[11px] text-warning-ink">
            promoted
          </span>
        </Tip>
      ) : null}
    </span>
  );
}

/**
 * How long since this recorder last reported, called out in red once it is past the window
 * the arbiter fails a recorder over.
 *
 * A recorder that has *never* reported is not "0 seconds ago" — it has registered and said
 * nothing since — so it reads "never", in the same warning color.
 */
export function ReportedAge({ reportedAt }: { reportedAt: string | null }) {
  const age = reportedAt === null ? null : Date.now() - new Date(reportedAt).getTime();
  const stale = age === null || age > REPORT_STALE_MS;
  return (
    <Tip label={HELP["admin.recorder.reported"]}>
      <span className={`text-[11px] tabular-nums ${stale ? "text-critical-ink" : "text-ink-2"}`}>
        reported {reportedAt === null ? "never" : formatRelative(reportedAt)}
      </span>
    </Tip>
  );
}

/**
 * The recorder's own policy: what it is for, and which spare to prefer.
 *
 * Both controls save on change rather than behind a Save button, because each is a single
 * value with an immediate meaning and no invalid intermediate state — unlike the tuner
 * drawer, where a center frequency has to be checked against a span before it is worth
 * sending. The priority field commits on blur (and on Enter), so typing "10" over "100"
 * does not fire three saves on the way.
 */
export function RecorderPolicyStrip({ recorder, canAdmin }: { recorder: Recorder; canAdmin: boolean }) {
  const save = useSaveRecorderPolicy();
  const forget = useForgetRecorder();
  const [priority, setPriority] = useState(String(recorder.priority));
  const [label, setLabel] = useState(recorder.label ?? "");
  const [confirming, setConfirming] = useState(false);
  const [editing, setEditing] = useState(false);
  const priorityId = `recorder-priority-${recorder.id}`;
  const labelId = `recorder-label-${recorder.id}`;

  // The list refetches every 30 s and on every tuner.status frame; adopt the server's value
  // whenever it changes underneath, so an edit made elsewhere is not masked by stale text.
  useEffect(() => setPriority(String(recorder.priority)), [recorder.priority]);
  useEffect(() => setLabel(recorder.label ?? ""), [recorder.label]);

  const commit = (policy: { standby?: RecorderStandby; priority?: number; label?: string }) =>
    save.mutate(
      { recorderId: recorder.id, policy },
      {
        onSuccess: () => toast("Recorder policy saved", { tone: "success" }),
        onError: (error) => {
          setPriority(String(recorder.priority));
          toast("Could not save the recorder policy", { tone: "error", body: errorDetail(error) });
        },
      },
    );

  // The label commits on blur (and on Enter) like the priority beside it: a name is typed a
  // character at a time, and a PUT per keystroke would be a PUT per keystroke.
  const commitLabel = () => {
    const text = label.trim();
    if (text === (recorder.label ?? "")) return;
    commit({ label: text });
  };

  const commitPriority = () => {
    const text = priority.trim();
    // `Number("")` is 0, and an emptied field means "I changed my mind", not "priority 0".
    if (text === "") {
      setPriority(String(recorder.priority));
      return;
    }
    const value = Number(text);
    if (!Number.isInteger(value) || value < 0) {
      setPriority(String(recorder.priority));
      toast("Priority must be a whole number, and lower is preferred", { tone: "error" });
      return;
    }
    if (value === recorder.priority) return;
    commit({ priority: value });
  };

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2.5">
      <span className="flex items-center gap-1.5 text-[11px] text-ink-2">
        {/* `htmlFor` rather than wrapping, for the reason the priority field below gives. */}
        <label htmlFor={labelId}>Name</label>
        <Tip label={HELP["admin.recorder.label"]}>
          <TextInput
            id={labelId}
            aria-label={`Name for ${recorder.instance_id}`}
            className="h-8 w-44"
            placeholder={recorder.instance_id}
            value={label}
            disabled={!canAdmin || save.isPending}
            onChange={(event) => setLabel(event.target.value)}
            onBlur={commitLabel}
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
            }}
          />
        </Tip>
      </span>
      <Select
        value={recorder.standby}
        onValueChange={(value) => commit({ standby: value as RecorderStandby })}
        options={RECORDER_STANDBY.map((value) => ({ value, label: value }))}
        ariaLabel={`Standby class for ${recorder.instance_id}`}
        tip={
          canAdmin ? HELP["admin.recorder.standby"] : "Changing what a recorder is for needs an admin account"
        }
        size="sm"
        className="w-28"
        disabled={!canAdmin || save.isPending}
      />
      <span className="flex items-center gap-1.5 text-[11px] text-ink-2">
        {/* `htmlFor` rather than wrapping: the input sits inside a tooltip trigger, and a
            label that only *contains* it is not one a screen reader can follow. */}
        <label htmlFor={priorityId}>Priority</label>
        <Tip label={HELP["admin.recorder.priority"]}>
          <TextInput
            id={priorityId}
            inputMode="numeric"
            aria-label={`Priority for ${recorder.instance_id}`}
            className="h-8 w-20 tabular-nums"
            value={priority}
            disabled={!canAdmin || save.isPending}
            onChange={(event) => setPriority(event.target.value)}
            onBlur={commitPriority}
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
            }}
          />
        </Tip>
      </span>
      {/* The key fingerprint lives on the card and not only in the dialog: "do these two
          recorders hold the same key" is a question about a *pair*, and the roster is the
          one place both of them are on screen at once. */}
      {recorder.desired_link?.key_fingerprint ? (
        <Tip label={HELP["admin.recorder.settings.fingerprint"]}>
          <span className="flex items-center gap-1.5 text-[11px] text-ink-2">
            <Link2 size={12} aria-hidden="true" />
            key <span className="font-mono text-ink">{recorder.desired_link.key_fingerprint}</span>
          </span>
        </Tip>
      ) : null}
      <span className="ml-auto flex items-center gap-2">
        <ReportedAge reportedAt={recorder.reported_at} />
        <Button
          size="sm"
          variant="ghost"
          tip={canAdmin ? HELP["admin.recorder.settings"] : HELP["app.adminOnly"]}
          disabled={!canAdmin}
          aria-label={`Settings for ${recorder.instance_id}`}
          onClick={() => setEditing(true)}
        >
          <Settings size={13} aria-hidden="true" />
          <span className="ml-1">Settings</span>
        </Button>
        <Button
          tip={canAdmin ? HELP["admin.recorder.forget"] : HELP["app.adminOnly"]}
          size="iconSm"
          variant="ghost"
          disabled={!canAdmin || forget.isPending}
          aria-label={`Forget ${recorder.label ?? recorder.instance_id}`}
          onClick={() => setConfirming(true)}
        >
          <Trash2 size={13} aria-hidden="true" />
        </Button>
      </span>

      {/* Mounted only while it is open, so its state — the revealed key above all — is built
          fresh from the recorder each time rather than surviving a close. */}
      {editing ? <RecorderSettingsDialog recorder={recorder} onClose={() => setEditing(false)} /> : null}

      <Dialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Forget ${recorder.label ?? recorder.instance_id}?`}
        description="Nothing this recorder recorded is touched — calls carry its name, not a link to this row. What goes is the name and standby policy set here."
        footer={
          <>
            <Button tip={HELP["admin.recorder.forget.cancel"]} size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              tip={HELP["admin.recorder.forget.confirm"]}
              size="sm"
              variant="danger"
              disabled={forget.isPending}
              onClick={() =>
                forget.mutate(
                  { id: recorder.id },
                  {
                    onSuccess: () => {
                      toast("Recorder forgotten", { tone: "success" });
                      setConfirming(false);
                    },
                    // A recorder that is still reporting is a 409, and the API says so:
                    // show its sentence rather than a generic failure, because "it will
                    // come straight back on its next poll" is the whole answer.
                    onError: (error) =>
                      toast("Could not forget the recorder", {
                        tone: "error",
                        body: errorDetail(error),
                      }),
                  },
                )
              }
            >
              Forget it
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          A recorder that is still reporting will refuse to be forgotten: it would be re-created by its very
          next poll. Stop it first, or wait an hour.
        </p>
      </Dialog>
    </div>
  );
}

/** How a recorder came by its current upload setting, in words an operator reads. */
const UPLINK_SOURCE: Record<string, string> = {
  config: "from its own configuration",
  keyup: "set from keyup",
  panel: "set on the recorder",
};

/** "46 calls, 38.0 MiB" — what is waiting behind a hold, when the recorder says. */
function spoolLabel(uplink: RecorderUplink): string | null {
  const calls = num(uplink.spooled_calls);
  const bytes = num(uplink.spool_bytes);
  const parts: string[] = [];
  if (calls !== null) parts.push(`${formatNumber(calls)} ${calls === 1 ? "call" : "calls"}`);
  if (bytes !== null) parts.push(`${(bytes / MIB).toFixed(1)} MiB`);
  return parts.length === 0 ? null : parts.join(", ");
}

/**
 * What this recorder is doing with the calls it records, and the one control that changes it.
 *
 * The strip reads the recorder's *own* report (`uplink`) rather than what keyup asked for,
 * because those are two different facts and only the first one is true: a hold set a second
 * ago is still "uploading" until the recorder polls, which is exactly what the pending badge
 * beside it says. Where the setting came from is worth a line of its own — a recorder can be
 * held by its own config file or by the switch on its panel, and neither is something to fix
 * from here.
 *
 * A recorder that reports no uplink and has never been given a hold renders nothing at all,
 * the same way {@link StationPanel} is absent on a recorder that has not described itself:
 * an older binary neither reports the hold nor would apply one, so a dead control on its card
 * would be a promise the software cannot keep.
 */
export function UplinkStrip({ recorder, canAdmin }: { recorder: Recorder; canAdmin: boolean }) {
  const save = useSaveRecorderPolicy();
  const [confirming, setConfirming] = useState(false);
  const uplink = recorder.uplink;
  if (!uplink && recorder.desired_spool_only === null) return null;

  // The recorder's word first; what keyup asked for only until the first report comes back.
  const holding = typeof uplink?.spool_only === "boolean" ? uplink.spool_only : recorder.desired_spool_only;
  const source = text(uplink?.source);
  const spooled = uplink ? spoolLabel(uplink) : null;
  // Quoted back in the release dialog: "Releasing will upload N spooled calls".
  const spooledCalls = uplink ? num(uplink.spooled_calls) : null;
  const url = text(uplink?.url);

  const set = (spoolOnly: boolean) =>
    save.mutate(
      { recorderId: recorder.id, policy: { spool_only: spoolOnly } },
      {
        onSuccess: () => {
          toast(spoolOnly ? "Uploads held" : "Hold released", { tone: "success" });
          setConfirming(false);
        },
        onError: (error) =>
          toast("Could not change the upload hold", { tone: "error", body: errorDetail(error) }),
      },
    );

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-4 py-2.5">
      <span className="flex items-center gap-1.5 text-xs text-ink">
        {holding ? (
          <PauseCircle size={13} aria-hidden="true" className="text-warning" />
        ) : (
          <UploadCloud size={13} aria-hidden="true" className="text-ink-2" />
        )}
        <Tip label={HELP["admin.recorder.uplink"]}>
          <span data-testid="uplink-state">
            {holding ? `Holding uploads${spooled ? ` (${spooled} spooled)` : ""}` : "Uploading"}
          </span>
        </Tip>
      </span>
      {source ? (
        <Tip label={HELP["admin.recorder.uplink.source"]}>
          <span className="text-[11px] text-ink-2">{UPLINK_SOURCE[source] ?? source}</span>
        </Tip>
      ) : null}
      {/* A recorder that is uploading and has nothing waiting says nothing: "0 calls, 0.0 MiB
          still in the spool" is the ordinary case wearing a number. It appears when there is
          actually a backlog draining — the interesting half of "Uploading". */}
      {!holding && spooled && (spooledCalls ?? 0) > 0 ? (
        <span className="text-[11px] text-ink-2">{spooled} still in the spool</span>
      ) : null}
      {url ? <span className="text-[11px] text-muted">{url}</span> : null}
      {recorder.spool_only_pending ? (
        <Tip label={HELP["admin.recorder.uplink.pending"]}>
          {/* Where it is going, not just that something is: the strip above reports what the
              recorder is still doing, and "pending" on its own would not say which way. */}
          <span className="rounded-full border border-warning px-2 py-0.5 text-[11px] text-ink">
            pending → {recorder.desired_spool_only ? "hold" : "upload"}
          </span>
        </Tip>
      ) : null}
      <span className="ml-auto">
        <Button
          size="sm"
          variant={holding ? "secondary" : "ghost"}
          tip={
            canAdmin
              ? holding
                ? HELP["admin.recorder.uplink.release"]
                : HELP["admin.recorder.uplink.hold"]
              : HELP["app.adminOnly"]
          }
          disabled={!canAdmin || save.isPending}
          // Holding is instant — nothing is lost and it is undone by the same button.
          // Releasing starts an upload of everything that piled up, so it is confirmed.
          onClick={() => (holding ? setConfirming(true) : set(true))}
        >
          {holding ? "Release hold" : "Hold uploads"}
        </Button>
      </span>

      <Dialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Release the hold on ${recorder.label ?? recorder.instance_id}?`}
        description="The recorder starts uploading again, and sends everything it spooled while it was held."
        footer={
          <>
            <Button
              tip={HELP["admin.recorder.uplink.release.cancel"]}
              size="sm"
              onClick={() => setConfirming(false)}
            >
              Cancel
            </Button>
            <Button
              tip={HELP["admin.recorder.uplink.release.confirm"]}
              size="sm"
              variant="primary"
              disabled={save.isPending}
              onClick={() => set(false)}
            >
              Release it
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {spooledCalls === null
            ? "Releasing will upload whatever the recorder has spooled."
            : `Releasing will upload ${formatNumber(spooledCalls)} spooled ${spooledCalls === 1 ? "call" : "calls"}.`}{" "}
          They go up in the background, at whatever rate the recorder's link manages.
        </p>
      </Dialog>
    </div>
  );
}

/**
 * What the operator said this station is: its name, where it stands, how to reach whoever
 * runs it. keyup cannot derive any of it and never overrides it — the recorder is the
 * authority — so the panel is simply absent on a recorder that has not described itself,
 * which is every recorder running an older binary.
 */
export function StationPanel({
  station,
  reportedAt,
}: {
  station: RecorderStation | null;
  reportedAt: string | null;
}) {
  if (!station) return null;
  const name = text(station.name);
  const location = text(station.location);
  const contact = text(station.contact);
  const notes = text(station.notes);
  const lat = num(station.latitude);
  const lon = num(station.longitude);
  const elevation = num(station.elevation_m);
  // A station object with nothing readable in it says no more than no station at all.
  if (!name && !location && !contact && !notes && lat === null && lon === null && elevation === null) {
    return null;
  }

  return (
    <div className="border-b border-line bg-surface-2/60 px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="flex items-center gap-1.5 text-xs font-medium text-ink">
          <MapPin size={13} aria-hidden="true" />
          {name ?? "Station"}
        </span>
        {location ? <span className="text-xs text-ink-2">{location}</span> : null}
        {lat !== null && lon !== null ? (
          <Tip label={HELP["admin.recorder.station.map"]}>
            <a
              href={`https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}`}
              target="_blank"
              rel="noreferrer"
              className="text-xs tabular-nums text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink"
            >
              {lat.toFixed(4)}, {lon.toFixed(4)}
            </a>
          </Tip>
        ) : null}
        {elevation !== null ? (
          <span className="text-xs tabular-nums text-ink-2">{formatNumber(elevation)} m ASL</span>
        ) : null}
        {contact ? <span className="text-xs text-ink-2">{contact}</span> : null}
        {reportedAt ? (
          <span className="ml-auto text-[11px] text-muted">described {formatRelative(reportedAt)}</span>
        ) : null}
      </div>
      {notes ? <p className="mt-1.5 max-w-3xl text-[11px] text-ink-2">{notes}</p> : null}
    </div>
  );
}

/**
 * The recorder's lifetime totals. `failed` and the free-disk figure are the two an operator
 * acts on, so a low disk gets a badge rather than being one more number in the row: a
 * recorder with nowhere left to spool loses calls silently.
 */
export function CountersStrip({ counters }: { counters: RecorderCounters | null }) {
  if (!counters) return null;
  const started = num(counters.calls_started);
  const ended = num(counters.calls_ended);
  const uploaded = num(counters.uploaded);
  const spooled = num(counters.spooled);
  const failed = num(counters.failed);
  const spoolBytes = num(counters.spool_bytes);
  const diskFree = num(counters.disk_free_bytes);
  const stats: { label: string; value: string; tone?: string }[] = [];
  if (started !== null) stats.push({ label: "started", value: formatNumber(started) });
  if (ended !== null) stats.push({ label: "ended", value: formatNumber(ended) });
  if (uploaded !== null) stats.push({ label: "uploaded", value: formatNumber(uploaded) });
  if (spooled !== null) stats.push({ label: "spooled", value: formatNumber(spooled) });
  if (failed !== null) {
    stats.push({
      label: "failed",
      value: formatNumber(failed),
      tone: failed > 0 ? "text-critical-ink" : undefined,
    });
  }
  if (spoolBytes !== null) {
    stats.push({ label: "spool", value: `${(spoolBytes / MIB).toFixed(1)} MiB` });
  }
  if (diskFree !== null) {
    stats.push({ label: "disk free", value: `${(diskFree / GIB).toFixed(1)} GiB` });
  }
  if (stats.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-line px-4 py-2.5">
      {stats.map((stat) => (
        <span key={stat.label} className="flex items-baseline gap-1.5 text-xs">
          <span className={`font-medium tabular-nums ${stat.tone ?? "text-ink"}`}>{stat.value}</span>
          <span className="text-[11px] text-ink-2">{stat.label}</span>
        </span>
      ))}
      {diskFree !== null && diskFree < LOW_DISK_BYTES ? (
        <Tip label={HELP["admin.recorder.lowDisk"]}>
          <span className="rounded-full border border-critical px-2 py-0.5 text-[11px] text-critical-ink">
            low disk
          </span>
        </Tip>
      ) : null}
    </div>
  );
}

function TunerRow({
  tuner,
  siteName,
  canAdmin,
  onEdit,
  spectrumOpen,
  onToggleSpectrum,
  spectrumPanel,
}: {
  tuner: Tuner;
  siteName: Map<number, string>;
  canAdmin: boolean;
  onEdit: () => void;
  spectrumOpen: boolean;
  onToggleSpectrum: () => void;
  spectrumPanel: ReactNode;
}) {
  const reported = tuner.reported ?? {};
  const desired = tuner.desired ?? {};
  const role = (reported.role as string | undefined) ?? desired.role ?? "auto";
  const focus =
    (typeof desired.site_id === "number" ? siteName.get(desired.site_id) : undefined) ??
    (reported.site as string | undefined) ??
    desired.site ??
    "—";
  const overflow = num(reported.overflow_per_s);

  return (
    <>
      <tr className="border-b border-line last:border-0 align-top">
        <td className="px-3 py-2">
          <div className="font-medium text-ink">{tuner.tuner_key}</div>
          <div className="text-[11px] text-ink-2">
            {(reported.backend as string) ?? "—"}
            {reported.serial ? ` · ${reported.serial}` : ""}
          </div>
        </td>
        <td className="px-3 py-2">
          <HardwareCell description={reported.description} />
        </td>
        <td className="px-3 py-2 tabular-nums text-ink-2">
          <div>{spanLabel(reported.center_hz, reported.span_hz)}</div>
          <BandCell reported={reported} desired={desired} />
        </td>
        <td className="px-3 py-2">
          <span className="rounded border border-line px-1.5 py-0.5 text-[11px] text-ink">{role}</span>
        </td>
        <td className="px-3 py-2 text-ink-2">{focus}</td>
        <td className="px-3 py-2">
          <RoleState role={role} reported={reported} />
        </td>
        <td className="px-3 py-2">
          <LevelCell rssi={num(reported.rssi_dbfs)} gain={tunerGain(reported)} />
        </td>
        <td className="px-3 py-2">
          {overflow === null ? (
            <span className="text-ink-2">—</span>
          ) : (
            <StatusChip
              status={overflow > 0 ? "degraded" : "healthy"}
              tip={overflow > 0 ? "The tuner is shedding grants it cannot keep up with" : "No overflow"}
              className="tabular-nums"
            />
          )}
        </td>
        <td className="px-3 py-2 text-right">
          <div className="flex items-center justify-end gap-1.5">
            {tuner.drift ? (
              <Tip label="The recorder has not yet applied the last change you made">
                <span className="rounded-full border border-warning px-2 py-0.5 text-[11px] text-ink">
                  pending
                </span>
              </Tip>
            ) : null}
            <Button
              size="sm"
              variant={spectrumOpen ? "secondary" : "ghost"}
              tip={
                canAdmin
                  ? "Show this tuner's live spectrum and waterfall"
                  : "Viewing a tuner's spectrum needs an admin account"
              }
              disabled={!canAdmin}
              onClick={onToggleSpectrum}
              aria-expanded={spectrumOpen}
            >
              <span className="flex items-center gap-1">
                <Activity size={13} aria-hidden="true" /> Spectrum
              </span>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              tip={canAdmin ? HELP["admin.recorder.edit"] : "Editing a tuner needs an admin account"}
              disabled={!canAdmin}
              onClick={onEdit}
            >
              Edit
            </Button>
          </div>
        </td>
      </tr>
      {spectrumPanel ? (
        <tr className="border-b border-line last:border-0 bg-surface-2">
          <td colSpan={9} className="p-0">
            {spectrumPanel}
          </td>
        </tr>
      ) : null}
    </>
  );
}

/**
 * What this tuner is restricted to, under its span: the strip, and the list in words.
 *
 * Absent for the ordinary case — a tuner recording anything inside its capture — because a
 * fully shaded rail on every row would say the same nothing on each of them. It appears
 * when there is a restriction to state, or a change to it the recorder has not taken up.
 */
function BandCell({
  reported,
  desired,
}: {
  reported: Record<string, unknown>;
  desired: Record<string, unknown>;
}) {
  const applied = bandList(reported.bands) ?? [];
  const wanted = bandList(desired.bands);
  const centerHz = num(reported.center_hz);
  const spanHz = num(reported.span_hz);
  const pending = wanted !== null && !sameBands(wanted, applied);
  if (applied.length === 0 && !pending) return null;
  return (
    <div className="mt-1 space-y-0.5">
      {centerHz !== null && spanHz !== null && spanHz > 0 ? (
        <TunerBandStrip
          centerHz={centerHz}
          spanHz={spanHz}
          applied={applied}
          desired={pending ? wanted : null}
        />
      ) : null}
      <div className="text-[11px] text-ink-2">
        {formatBands(applied)}
        {pending ? <span className="text-warning-ink"> → {formatBands(wanted ?? [])}</span> : null}
      </div>
    </div>
  );
}

/**
 * What the operator says is on this tuner's antenna run: the receiver and the antenna in
 * the cell, the rest — cable, preamp, height, notes — behind a tooltip, because they are
 * what you reach for when a level looks wrong, not what you scan a table for.
 */
function HardwareCell({ description }: { description: TunerDescription | null | undefined }) {
  if (!description) return <span className="text-ink-2">—</span>;
  const receiver = text(description.receiver);
  const antenna = text(description.antenna);
  const cable = text(description.cable);
  const amplifier = text(description.amplifier);
  const notes = text(description.notes);
  const height = num(description.height_m);
  if (!receiver && !antenna && !cable && !amplifier && !notes && height === null) {
    return <span className="text-ink-2">—</span>;
  }

  const detail: string[] = [];
  if (cable) detail.push(`Cable: ${cable}`);
  if (amplifier) detail.push(`Amplifier: ${amplifier}`);
  if (height !== null) detail.push(`Height: ${formatNumber(height)} m`);
  if (notes) detail.push(notes);

  // The first line of the notes, under the antenna. The tooltip below still carries the
  // whole description, but a note only a hover reveals is a note nobody finds — and these
  // are where the "never tune this antenna to 800" sentences live. One line, clamped, with
  // the full text on the element's own `title` so the rest is a hover away rather than gone.
  const firstNote = notes
    ? (notes
        .split("\n")
        .find((line) => line.trim() !== "")
        ?.trim() ?? null)
    : null;

  const cell = (
    <span className="block max-w-[14rem]">
      <span className="block truncate text-ink">{receiver ?? "—"}</span>
      {antenna ? <span className="block truncate text-[11px] text-ink-2">{antenna}</span> : null}
      {firstNote ? (
        <span className="block truncate text-[11px] text-muted" title={notes ?? undefined}>
          {firstNote}
        </span>
      ) : null}
    </span>
  );
  return detail.length > 0 ? (
    <Tip
      label={
        <span className="block space-y-0.5">
          {detail.map((line) => (
            <span key={line} className="block">
              {line}
            </span>
          ))}
        </span>
      }
    >
      <span className="cursor-help border-b border-dotted border-line-strong">{cell}</span>
    </Tip>
  ) : (
    cell
  );
}

/**
 * The tuner's wideband level, as a bar against a fixed −110…0 dBFS scale with the gain it
 * was measured at underneath. The gain line is not decoration: the figure is ADC-referenced,
 * so the same antenna reads 20 dB apart at two gain settings and the number alone would be
 * unreadable.
 */
function LevelCell({ rssi, gain }: { rssi: number | null; gain: TunerGain | null | undefined }) {
  const summary = gainSummary(gain);
  if (rssi === null) {
    return (
      <span className="block text-[11px] text-ink-2">
        <span className="block">—</span>
        {summary ? <span className="block">{summary}</span> : null}
      </span>
    );
  }
  const clamped = Math.min(RSSI_CEILING, Math.max(RSSI_FLOOR, rssi));
  const fraction = (clamped - RSSI_FLOOR) / (RSSI_CEILING - RSSI_FLOOR);
  const band = levelBand(rssi);
  return (
    <span className="block w-28">
      <Tip label={LEVEL_TIP[band]}>
        <span className="block">
          <span className="flex items-center gap-1.5">
            {/* Decoration: the reading itself is the number beside it, which a screen
                reader gets in full from the column header and the dBFS unit there. */}
            <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-line" aria-hidden="true">
              <span
                className={`block h-full ${LEVEL_FILL[band]}`}
                style={{ width: `${(fraction * 100).toFixed(1)}%` }}
              />
            </span>
            <span
              className={`tabular-nums text-[11px] ${band === "hot" ? "text-warning-ink" : "text-ink-2"}`}
            >
              {rssi.toFixed(1)}
            </span>
          </span>
        </span>
      </Tip>
      {summary ? <span className="mt-0.5 block text-[11px] text-muted">{summary}</span> : null}
    </span>
  );
}

/** The per-role detail: control channel + NAC + TSBK/s, voice pool, or data channel. */
function RoleState({ role, reported }: { role: string; reported: Record<string, unknown> }) {
  const locked = reported.locked === true;
  const lock = (
    <span className={locked ? "text-good-ink" : "text-ink-2"}>{locked ? "locked" : "searching"}</span>
  );
  if (role === "control") {
    const nac = num(reported.nac);
    return (
      <span className="tabular-nums">
        {lock} · {MHz(num(reported.control_hz))} MHz
        {nac !== null ? ` · NAC 0x${nac.toString(16).toUpperCase()}` : ""}
        {num(reported.tsbk_rate) !== null ? ` · ${(reported.tsbk_rate as number).toFixed(1)} TSBK/s` : ""}
        <FreqError hz={num(reported.freq_error_hz)} />
      </span>
    );
  }
  if (role === "voice") {
    const inUse = num(reported.pool_in_use);
    const size = num(reported.pool_size);
    return (
      <span className="tabular-nums">
        {lock}
        {inUse !== null ? ` · pool ${inUse}${size !== null ? `/${size}` : ""}` : ""}
        <FreqError hz={num(reported.freq_error_hz)} />
      </span>
    );
  }
  if (role === "data") {
    return (
      <span className="tabular-nums">
        {lock} · {MHz(num(reported.center_hz))} MHz
        <FreqError hz={num(reported.freq_error_hz)} />
      </span>
    );
  }
  return (
    <span className="tabular-nums">
      {lock}
      <FreqError hz={num(reported.freq_error_hz)} />
    </span>
  );
}

/**
 * How far off the control channel the tuner is tracking, in hertz. Only the tuner that is
 * following a control channel measures one, so it is absent far more often than present —
 * and a sign matters, because a drifting reference walks one way.
 */
function FreqError({ hz }: { hz: number | null }) {
  if (hz === null) return null;
  return (
    <Tip label={HELP["admin.recorder.freqError"]}>
      <span className="ml-1 cursor-help text-ink-2">
        · Δf {hz > 0 ? "+" : ""}
        {hz.toFixed(0)} Hz
      </span>
    </Tip>
  );
}
