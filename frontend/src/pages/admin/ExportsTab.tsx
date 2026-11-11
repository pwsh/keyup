import { AlertTriangle, ChevronDown, ChevronRight, PlugZap, Radio, RotateCcw } from "lucide-react";
import { type ReactNode, useCallback, useMemo, useState } from "react";
import {
  useCurrentUser,
  useExportStatus,
  useRetryExport,
  useSaveSettings,
  useSettings,
  useTestBroadcastifyFeed,
  useTestExport,
} from "../../api/hooks";
import type {
  BroadcastifyFeedStatus,
  ExportLedgerRow,
  ExportService,
  ExportServiceStatus,
  ExportStatus,
  ExportTestResult,
  SettingDefinition,
} from "../../api/types";
import { Button } from "../../components/ui/Button";
import { Card, CardHeader } from "../../components/ui/Card";
import { StatusChip } from "../../components/ui/Chip";
import { Dialog } from "../../components/ui/Dialog";
import { inputClass, TextArea } from "../../components/ui/Field";
import { Select } from "../../components/ui/Select";
import { SkeletonText } from "../../components/ui/Skeleton";
import { EmptyState } from "../../components/ui/States";
import { Switch } from "../../components/ui/Switch";
import { HELP } from "../../help/help";
import { formatBytes, formatDateTime, formatNumber, formatRelative } from "../../lib/format";
import { mutationErrorToast, toast } from "../../state/toasts";

/**
 * The two *per-call upload* services, in the order they are shown. Broadcastify sells
 * two products and this is only one of them: "Broadcastify Calls" is the per-call
 * upload, and the live audio stream — "Broadcastify Feed" — is a different section
 * with different credentials, rendered above these by {@link FeedCard}.
 */
const SERVICES: {
  id: ExportService;
  label: string;
  blurb: string;
  enableKey: string;
  prefix: string[];
  /** The order the fields are shown in; anything not listed follows, in API order. */
  order?: string[];
}[] = [
  {
    id: "broadcastify",
    label: "Broadcastify Calls (per-call upload)",
    blurb:
      "Uploads each recording to the Broadcastify Calls archive: register the call, then PUT the audio to the single-use URL that comes back. The API Key and System ID are from the Calls dashboard and belong to one environment — development and production credentials are issued separately and each endpoint refuses the other's. Neither is the feed's mount password.",
    enableKey: "export_broadcastify_enabled",
    prefix: ["broadcastify_", "export_broadcastify_"],
    // Who does the uploading first — it decides whether the rest of this card describes
    // what *this* host sends or what it hands to the recorders — then Broadcastify's own
    // workflow order: which environment, who you are, which system, what a conventional
    // slot would be, then how the audio is encoded.
    order: [
      "broadcastify_upload_at",
      "broadcastify_environment",
      "broadcastify_api_key",
      "broadcastify_system_id",
      "broadcastify_slot",
      "broadcastify_encoding",
      "broadcastify_mp3_bitrate_kbps",
      "broadcastify_skew_limit_s",
      "broadcastify_url",
      "broadcastify_dev_url",
      "broadcastify_dev_api_key",
      "broadcastify_dev_system_id",
    ],
  },
  {
    id: "openmhz",
    label: "OpenMHz",
    blurb: "Uploads each recording to OpenMHz with the system's API key and shortName.",
    enableKey: "export_openmhz_enabled",
    prefix: ["openmhz_", "export_openmhz_"],
  },
];

/**
 * The Broadcastify **Feed** fields, in the order Broadcastify's own *Feed Technical
 * Details (Advanced)* panel lists them, and labelled with its words. An operator is
 * copying five values off that page; the form they land in should read like it.
 */
const FEED_KEYS = [
  "broadcastify_feed_server",
  "broadcastify_feed_port",
  "broadcastify_feed_mount",
  "broadcastify_feed_password",
  "broadcastify_feed_username",
  "broadcastify_feed_bitrate_kbps",
  "broadcastify_feed_sample_rate",
  "broadcastify_feed_max_backlog_s",
  "broadcastify_feed_silence",
];

const FEED_ENABLE_KEY = "export_broadcastify_feed_enabled";

/**
 * The knobs that belong to *both* services. Everything else in the Export group is
 * either one service's own configuration or its enable switch, which is rendered as
 * the card's header rather than as a field.
 */
const SHARED_KEYS = [
  "export_ntp_server",
  "export_target_lufs",
  "export_include_encrypted",
  "export_min_duration_s",
  "export_poll_seconds",
  "export_min_interval_s",
  "export_timeout_s",
  "export_batch_size",
  "export_max_age_minutes",
  "export_max_attempts",
];

/** Which ledger statuses count as trouble, for the tone of the count. */
const BAD_STATUSES = new Set(["failed"]);

/**
 * Where each card remembers whether its settings are open, per browser.
 *
 * The status of a service is what an operator comes to this page for — is it on, is the
 * queue moving, when did it last upload — and the form under it is what they came for
 * once, on the day they set it up. So the numbers are always shown and the fields start
 * folded away, and the one thing worth remembering is which cards *this* operator keeps
 * open. It is a per-browser convenience, never anything the server needs to know.
 */
const OPEN_KEY = (card: string) => `keyup.exports.open.${card}`;

/** The Calls system list is a picker rather than a text box; see {@link SystemsField}. */
const CALLS_SYSTEMS_KEY = "export_broadcastify_systems";

/**
 * Remember one card's open/closed state across visits, and never let it hide an edit.
 *
 * A card holding an unsaved change is forced open however it was left: the sticky bar
 * says "3 unsaved changes" and every one of them has to be somewhere the operator can
 * see, or Save becomes a button that writes things they cannot read.
 */
function useCardOpen(card: string, dirty: boolean): [boolean, (next: boolean) => void] {
  const [open, setOpen] = useState(() => {
    try {
      return window.localStorage.getItem(OPEN_KEY(card)) === "true";
    } catch {
      // Private windows and blocked site data throw on read; a card that cannot remember
      // is a card that starts closed, which is the default anyway.
      return false;
    }
  });
  const set = useCallback(
    (next: boolean) => {
      setOpen(next);
      try {
        window.localStorage.setItem(OPEN_KEY(card), String(next));
      } catch {
        /* nothing to do: the preference simply is not remembered */
      }
    },
    [card],
  );
  return [open || dirty, set];
}

/**
 * The "Settings" disclosure every card's form sits behind.
 *
 * The fields stay **mounted** when it is closed rather than being unmounted: a half-typed
 * credential must survive folding the card away, and every key in the Export group has to
 * be reachable from this page (see the "exactly one home" tests). `hidden` is the whole
 * of the collapse.
 */
function Settings({
  card,
  open,
  onOpenChange,
  count,
  dirtyCount,
  children,
}: {
  card: string;
  open: boolean;
  onOpenChange: (next: boolean) => void;
  count: number;
  dirtyCount: number;
  children: ReactNode;
}) {
  const id = `export-settings-${card}`;
  return (
    <div className="border-t border-line">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => onOpenChange(!open)}
        className="flex w-full items-center gap-2 px-4 py-2 text-left text-xs font-medium text-ink-2 hover:text-ink"
      >
        {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
        Settings
        <span className="text-[11px] font-normal text-muted">
          {count} field{count === 1 ? "" : "s"}
        </span>
        {dirtyCount > 0 ? (
          <span className="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-semibold text-warning">
            {dirtyCount} unsaved
          </span>
        ) : null}
      </button>
      <div id={id} hidden={!open} className="grid gap-3 p-4 pt-0 sm:grid-cols-2">
        {children}
      </div>
    </div>
  );
}

/**
 * Admin → Exports: the whole of Broadcastify Calls and OpenMHz in one page.
 *
 * The fields are the runtime settings themselves (`GET /admin/settings`, group
 * "Export"), rendered by `kind` exactly as the Settings tab renders them, so a knob
 * added to `SETTINGS_SPEC` appears here without a second edit. What this page adds
 * over that generic form is the part a list of key/value rows cannot say: which
 * service each knob belongs to, how that service's queue is actually doing, and a
 * confirm step in front of the one switch that starts publishing to somebody else's
 * archive.
 */
export function ExportsTab() {
  const { canWrite } = useCurrentUser();
  const status = useExportStatus();
  const settings = useSettings();
  const save = useSaveSettings();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState<{ service: ExportService; label: string } | null>(null);
  const [confirmingFeed, setConfirmingFeed] = useState(false);

  const byKey = useMemo(() => {
    const map = new Map<string, SettingDefinition>();
    for (const row of settings.data ?? []) map.set(row.key, row);
    return map;
  }, [settings.data]);

  const dirty = Object.keys(draft).length > 0;
  const value = (key: string) => draft[key] ?? byKey.get(key)?.value ?? "";
  const isOn = (key: string) => value(key).toLowerCase() === "true";
  /** The unsaved values belonging to one card — what its Test button is handed. */
  const draftFor = (keys: string[]): Record<string, string> =>
    Object.fromEntries(keys.filter((key) => key in draft).map((key) => [key, draft[key] as string]));

  // Broadcastify Calls publishes trunked systems only. With no trunked system in the
  // archive there is nothing the switch could ever send, so it is offered disabled with
  // the reason rather than armed and then silently uploading nothing.
  const trunkedSystems = status.data?.trunked_systems ?? [];
  const noTrunkedSystem = status.isSuccess && trunkedSystems.length === 0;

  const commit = (values: Record<string, string>, message: string) =>
    save.mutate(values, {
      onSuccess: () => {
        toast(message, { tone: "success" });
        setDraft((current) => {
          const next = { ...current };
          for (const key of Object.keys(values)) delete next[key];
          return next;
        });
        void status.refetch();
      },
      onError: (error) => mutationErrorToast(error, "Could not save the export settings"),
    });

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-start gap-3 p-4 text-xs leading-relaxed text-ink-2">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-warning" aria-hidden="true" />
          <div>
            <p className="font-medium text-ink">Run exactly one uploader per system.</p>
            <p className="mt-1">
              If sdrtrunk or trunk-recorder is already feeding this system to a service, turning the same
              service on here posts every call twice. Stop the other uploader first, confirm it has stopped,
              then flip one switch and watch it before flipping the second. Turning a switch on also starts
              the clock: only calls that arrive <em>after</em> that moment are ever uploaded, so nothing
              already in the archive is republished.
            </p>
          </div>
        </div>
      </Card>

      <FeedCard
        canWrite={canWrite}
        enabled={isOn(FEED_ENABLE_KEY)}
        view={status.data?.broadcastify_feed}
        fields={FEED_KEYS.map((key) => byKey.get(key)).filter((row): row is SettingDefinition => !!row)}
        drafted={draftFor(FEED_KEYS)}
        value={value}
        onChange={(key, next) => setDraft({ ...draft, [key]: next })}
        onToggle={(next) => {
          if (next) {
            setConfirmingFeed(true);
            return;
          }
          commit({ [FEED_ENABLE_KEY]: "false" }, "Broadcastify Feed stopped");
        }}
      />

      {SERVICES.map((service) => {
        const view = status.data?.services.find((row) => row.service === service.id);
        const keys = (settings.data ?? [])
          .filter(
            (row) =>
              row.group === "Export" &&
              row.key !== service.enableKey &&
              !SHARED_KEYS.includes(row.key) &&
              // The feed is its own product and its own section above.
              row.key !== FEED_ENABLE_KEY &&
              !row.key.startsWith("broadcastify_feed_") &&
              service.prefix.some((prefix) => row.key.startsWith(prefix)),
          )
          .map((row) => row.key)
          // Broadcastify's fields read in the order its own workflow does; everything
          // unlisted keeps the API's order behind them (Array.prototype.sort is stable).
          .sort((a, b) => {
            const order = service.order ?? [];
            const rank = (key: string) => {
              const at = order.indexOf(key);
              return at === -1 ? order.length : at;
            };
            return rank(a) - rank(b);
          });
        return (
          <ServiceCard
            key={service.id}
            id={service.id}
            label={service.label}
            blurb={service.blurb}
            enabled={isOn(service.enableKey)}
            canWrite={canWrite}
            view={view}
            fields={keys.map((key) => byKey.get(key)).filter((row): row is SettingDefinition => !!row)}
            drafted={draftFor(keys)}
            trunkedSystems={trunkedSystems}
            blocked={service.id === "broadcastify" && noTrunkedSystem}
            value={value}
            onChange={(key, next) => setDraft({ ...draft, [key]: next })}
            onToggle={(next) => {
              if (next) {
                setConfirming({ service: service.id, label: service.label });
                return;
              }
              commit({ [service.enableKey]: "false" }, `${service.label} uploads stopped`);
            }}
          />
        );
      })}

      <SharedCard
        fields={SHARED_KEYS.map((key) => byKey.get(key)).filter((row): row is SettingDefinition => !!row)}
        drafted={draftFor(SHARED_KEYS)}
        canWrite={canWrite}
        value={value}
        onChange={(key, next) => setDraft({ ...draft, [key]: next })}
      />

      <RecentUploads rows={status.data?.recent ?? []} loading={status.isLoading} canWrite={canWrite} />

      {/* The save bar. It follows the page rather than sitting at the bottom of it,
          because the fields it saves are spread over four cards and a form whose Save
          is somewhere below the fold is a form that gets half-saved. It appears only
          when there is something to save, and it says how much. */}
      {dirty ? (
        <div className="sticky bottom-0 z-20 -mx-4 mt-2 border-t border-line bg-page/95 px-4 py-3 backdrop-blur md:-mx-6 md:px-6">
          <div className="flex items-center gap-3 text-xs">
            <span className="text-ink">
              {Object.keys(draft).length} unsaved change
              {Object.keys(draft).length === 1 ? "" : "s"}
            </span>
            <span className="truncate font-mono text-[10px] text-muted">
              {Object.keys(draft).sort().join(", ")}
            </span>
            <span className="ml-auto flex items-center gap-2">
              <Button
                tip={HELP["admin.exports.discard"]}
                size="sm"
                disabled={save.isPending}
                onClick={() => setDraft({})}
              >
                Discard
              </Button>
              <Button
                tip={canWrite ? HELP["admin.exports.save"] : HELP["app.readOnly"]}
                size="sm"
                variant="primary"
                disabled={!canWrite || save.isPending}
                onClick={() => commit(draft, "Export settings saved")}
              >
                Save changes
              </Button>
            </span>
          </div>
        </div>
      ) : null}

      <Dialog
        open={confirmingFeed}
        onOpenChange={(open) => !open && setConfirmingFeed(false)}
        title="Start streaming to the Broadcastify Feed?"
        description="Live calls will play on the feed's public stream from this moment on."
        footer={
          <>
            <Button tip="Leave the feed switched off" onClick={() => setConfirmingFeed(false)}>
              Cancel
            </Button>
            <Button
              tip="Open the mount and start streaming"
              variant="danger"
              onClick={() => {
                commit({ [FEED_ENABLE_KEY]: "true" }, "Broadcastify Feed started");
                setConfirmingFeed(false);
              }}
            >
              Start streaming
            </Button>
          </>
        }
      >
        <p className="text-xs leading-relaxed text-ink-2">
          An Icecast mount takes <em>one</em> source at a time. If another broadcasting client — the one
          currently feeding this mount — is still running, this will simply be refused, and if it stops while
          both are configured the two will fight over the mount. Stop the other client first. Only calls that
          start after this moment are played; turning the switch back off closes the mount.
        </p>
      </Dialog>

      <Dialog
        open={confirming !== null}
        onOpenChange={(open) => !open && setConfirming(null)}
        title={confirming ? `Start uploading to ${confirming.label}?` : ""}
        description="Calls recorded from this moment on will be published to somebody else's public archive."
        footer={
          <>
            <Button tip="Leave the service switched off" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button
              tip="Turn the uploader on"
              variant="danger"
              onClick={() => {
                const target = SERVICES.find((row) => row.id === confirming?.service);
                if (target) commit({ [target.enableKey]: "true" }, `${target.label} uploads started`);
                setConfirming(null);
              }}
            >
              Turn it on
            </Button>
          </>
        }
      >
        <p className="text-xs leading-relaxed text-ink-2">
          Check first that no other uploader — sdrtrunk, trunk-recorder — is already sending this system to{" "}
          {confirming?.label}. Two uploaders post every call twice, and the duplicate is visible to everyone
          listening. Only calls that start after this moment are uploaded; turning the switch back off stops
          new uploads and clears the clock.
        </p>
      </Dialog>
    </div>
  );
}

/**
 * Broadcastify **Feed** — the live audio stream.
 *
 * The five fields are named the way Broadcastify's feed management page names them,
 * under the heading it uses, because that is where an operator is copying them from:
 * "These feed technical details are provided for users who want to configure their own
 * broadcasting clients." Everything else here is about the one property a stream has
 * that an upload queue does not: whether the mount is currently holding our source.
 */
function FeedCard({
  canWrite,
  enabled,
  view,
  fields,
  drafted,
  value,
  onChange,
  onToggle,
}: {
  canWrite: boolean;
  enabled: boolean;
  view: BroadcastifyFeedStatus | undefined;
  fields: SettingDefinition[];
  drafted: Record<string, string>;
  value: (key: string) => string;
  onChange: (key: string, next: string) => void;
  onToggle: (next: boolean) => void;
}) {
  const feedId = value("bcfy_feed_id").trim();
  const test = useTestBroadcastifyFeed();
  const [result, setResult] = useState<ExportTestResult | null>(null);
  const [open, setOpen] = useCardOpen("feed", Object.keys(drafted).length > 0);

  return (
    <Card>
      <CardHeader
        title="Broadcastify Feed (live audio stream)"
        hint={HELP["admin.exports.feed"]}
        actions={
          <div className="flex items-center gap-3">
            <Button
              tip={HELP["admin.exports.feed.test"]}
              size="sm"
              disabled={test.isPending}
              onClick={() =>
                // The mount is probed with what is on the screen, unsaved values and all:
                // an operator presses this to find out whether the password they have just
                // pasted works, and testing the stored one answers a different question.
                test.mutate(drafted, {
                  onSuccess: (data) => setResult(data),
                  onError: (error) => mutationErrorToast(error, "Could not test the mount"),
                })
              }
            >
              <Radio size={14} aria-hidden="true" />
              Test mount
            </Button>
            <Switch
              checked={enabled}
              disabled={!canWrite}
              onCheckedChange={onToggle}
              label={enabled ? "streaming" : "off"}
              tip={HELP["admin.exports.feed.enable"]}
            />
          </div>
        }
      />

      <p className="border-b border-line px-4 py-2 text-[11px] leading-snug text-muted">
        The live stream on the feed's own page — a continuous Icecast source, not per-call uploads. The five
        fields below are the <em>Feed Technical Details (Advanced)</em> from the feed's management page:
        “These feed technical details are provided for users who want to configure their own broadcasting
        clients.” Keyup is one of those clients.
      </p>

      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2 text-[11px] text-ink-2">
        <span>Broadcastify</span>
        <span className="ml-auto flex items-center gap-3">
          <a
            className="underline hover:text-ink"
            href="https://www.broadcastify.com/manage/"
            target="_blank"
            rel="noreferrer"
          >
            Feed management
          </a>
          {feedId ? (
            <a
              className="underline hover:text-ink"
              href={`https://www.broadcastify.com/listen/feed/${feedId}`}
              target="_blank"
              rel="noreferrer"
            >
              Feed page
            </a>
          ) : null}
        </span>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-b border-line px-4 py-3 text-xs sm:grid-cols-4">
        <Stat label="Configured">
          <StatusChip
            status={view?.configured ? "ready" : "missing"}
            tip={
              view?.configured
                ? "The Server, Mount and Password are all set"
                : "Server, Mount or Password is blank; nothing will be streamed however the switch is set"
            }
          />
        </Stat>
        <Stat label="Mount">
          <StatusChip
            status={view?.connected ? "ready" : "missing"}
            tip={
              view?.connected
                ? "The mount is holding our source right now"
                : "No source connection — either the feed is off, or the mount refused it"
            }
          />
        </Stat>
        <Stat label="Streaming since">{view?.since ? formatDateTime(view.since) : "—"}</Stat>
        <Stat label="Switched on at">{view?.enabled_at ? formatDateTime(view.enabled_at) : "—"}</Stat>
        <Stat label="Calls played">{formatNumber(view?.calls_played ?? 0)}</Stat>
        <Stat label="Dropped (behind live)">
          <span className={(view?.dropped ?? 0) > 0 ? "text-warning" : undefined}>
            {formatNumber(view?.dropped ?? 0)}
          </span>
        </Stat>
        <Stat label="Queued audio">{`${formatNumber(view?.queued_s ?? 0)} s`}</Stat>
        <Stat label="Sent">{formatBytes(view?.bytes_sent ?? 0)}</Stat>
        <div className="col-span-2 sm:col-span-4">
          <dt className="text-ink-2">Last error</dt>
          <dd className="truncate text-ink" title={view?.last_error ?? undefined}>
            {view?.last_error ? `${view.last_error} (${formatRelative(view.last_error_at ?? null)})` : "none"}
          </dd>
        </div>
      </dl>

      {result ? (
        <p
          className={`border-b border-line px-4 py-2 text-[11px] leading-snug ${
            result.ok ? "text-ink-2" : "text-critical"
          }`}
        >
          <strong>{result.ok ? "Mount accepted the source" : "Problem"}:</strong> {result.detail} —{" "}
          <em>{result.checked}</em>
          {result.used_draft ? <DraftNote keys={result.draft_keys} /> : null}
        </p>
      ) : null}

      <Settings
        card="feed"
        open={open}
        onOpenChange={setOpen}
        count={fields.length}
        dirtyCount={Object.keys(drafted).length}
      >
        {fields.map((setting) => (
          <SettingField
            key={setting.key}
            setting={setting}
            value={value(setting.key)}
            disabled={!canWrite}
            onChange={(next) => onChange(setting.key, next)}
          />
        ))}
      </Settings>

      <p className="border-t border-line px-4 py-2 text-[11px] leading-snug text-muted">
        Which talkgroups the feed plays is the <strong>Broadcastify</strong> selection below — one set of
        checkboxes for both Broadcastify products.
      </p>
    </Card>
  );
}

function ServiceCard({
  id,
  label,
  blurb,
  enabled,
  canWrite,
  view,
  fields,
  drafted,
  trunkedSystems,
  blocked,
  value,
  onChange,
  onToggle,
}: {
  id: ExportService;
  label: string;
  blurb: string;
  enabled: boolean;
  canWrite: boolean;
  view: ExportServiceStatus | undefined;
  fields: SettingDefinition[];
  drafted: Record<string, string>;
  trunkedSystems: ExportStatus["trunked_systems"];
  /** Calls with no trunked system to publish: the switch is offered, disabled, with why. */
  blocked: boolean;
  value: (key: string) => string;
  onChange: (key: string, next: string) => void;
  onToggle: (next: boolean) => void;
}) {
  const test = useTestExport();
  const [result, setResult] = useState<ExportTestResult | null>(null);
  const [open, setOpen] = useCardOpen(id, Object.keys(drafted).length > 0);

  return (
    <Card>
      <CardHeader
        title={label}
        hint={HELP["admin.exports.service"]}
        actions={
          <div className="flex items-center gap-3">
            <Button
              tip={HELP["admin.exports.test"]}
              size="sm"
              disabled={test.isPending}
              onClick={() =>
                test.mutate(
                  { service: id, overrides: drafted },
                  {
                    onSuccess: (data) => setResult(data),
                    onError: (error) => mutationErrorToast(error, "Could not test the configuration"),
                  },
                )
              }
            >
              <PlugZap size={14} aria-hidden="true" />
              Test credentials
            </Button>
            <Switch
              checked={enabled}
              disabled={!canWrite || blocked}
              onCheckedChange={onToggle}
              label={enabled ? "uploading" : blocked ? "unavailable" : "off"}
              tip={blocked ? HELP["admin.exports.notTrunked"] : HELP["admin.exports.enable"]}
            />
          </div>
        }
      />

      {view?.environment ? <EnvironmentBar view={view} /> : null}
      {view && !view.environment && (view.status_url || view.manage_url) ? <LinksBar view={view} /> : null}

      {blocked ? (
        <p className="border-b border-line bg-warning/10 px-4 py-2 text-[11px] leading-snug text-warning">
          <strong>No trunked system to publish.</strong> Broadcastify Calls files every upload under a system
          id issued for a trunked system, so nothing here can be sent from a conventional archive. Mark a
          system trunked on the Systems list — or fix its protocol — and this switch becomes available.
        </p>
      ) : null}

      <p className="border-b border-line px-4 py-2 text-[11px] leading-snug text-muted">{blurb}</p>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-b border-line px-4 py-3 text-xs sm:grid-cols-4">
        <Stat label="Configured">
          <StatusChip
            status={view?.configured ? "ready" : "missing"}
            tip={
              view?.configured
                ? "The credentials this service needs are set"
                : "A credential is missing; nothing will be sent however the switch is set"
            }
          />
        </Stat>
        <Stat label="Switched on at">{view?.enabled_at ? formatDateTime(view.enabled_at) : "—"}</Stat>
        <Stat label="Talkgroups">{view?.talkgroup_mode ?? "all"}</Stat>
        <Stat label="Systems">{view && view.systems.length > 0 ? view.systems.join(", ") : "all"}</Stat>
        {id === "broadcastify" ? (
          <Stat label="Trunked systems">
            {trunkedSystems.length > 0 ? (
              trunkedSystems.map((row) => row.name).join(", ")
            ) : (
              <span className="text-warning">none</span>
            )}
          </Stat>
        ) : null}
        {view?.environment ? (
          <Stat label="Host clock">
            <ClockReading view={view} />
          </Stat>
        ) : null}
        {id === "broadcastify" ? (
          <Stat label="Uploaded by">{view?.upload_at === "recorder" ? "recorders" : "keyup"}</Stat>
        ) : null}
        <Stat label="Queue depth">{formatNumber(view?.queue_depth ?? 0)}</Stat>
        <Stat label="Last upload">{formatRelative(view?.last_upload_at ?? null)}</Stat>
        <Stat label="Uploaded (24 h)">{formatNumber(view?.counts_24h?.uploaded ?? 0)}</Stat>
        <Stat label="Failed (24 h)">
          <span className={(view?.counts_24h?.failed ?? 0) > 0 ? "text-critical" : undefined}>
            {formatNumber(view?.counts_24h?.failed ?? 0)}
          </span>
        </Stat>
        {id === "broadcastify" ? (
          // A duplicate is neither an upload nor a failure — it is the service saying it
          // already holds the call — and on a busy archive it is the largest number here.
          // Without it the card reads as "nothing is being uploaded" with no explanation.
          <>
            <Stat label="Duplicates (24 h)">
              <span title={`${formatNumber(view?.duplicates ?? 0)} all time`}>
                {formatNumber(view?.duplicates_24h ?? 0)}
              </span>
            </Stat>
            {/*
              The other half of that sentence: a duplicate is somebody else publishing
              first, a late skip is this deployment not reaching the archive inside its
              skew window. Both leave a skipped row and only this pair of numbers tells
              an operator which of the two is happening.
            */}
            {/*
              Recorder-direct upload, in two numbers. "By recorders" is how many of the
              day's uploads a recorder sent and reported back; "Claimed (24 h)" is how
              many calls this host stood down on because a recorder had claimed them
              first. The pair is the answer to "is it actually running": claims with no
              recorder uploads behind them means the recorders are winning the claim and
              then not uploading.
            */}
            <Stat label="By recorders (24 h)">{formatNumber(view?.by_recorder_24h ?? 0)}</Stat>
            <Stat label="Claimed (24 h)">
              <span title="Calls a recorder claimed first, so this host did not send them">
                {formatNumber(view?.claimed_24h ?? 0)}
              </span>
            </Stat>
            <Stat label="Too late (24 h)">
              <span
                className={(view?.late_24h ?? 0) > 0 ? "text-warning" : undefined}
                title="Calls already past the Broadcastify skew window when their turn came; nothing was sent"
              >
                {formatNumber(view?.late_24h ?? 0)}
              </span>
            </Stat>
          </>
        ) : null}
        {/*
          Both cards, unlike the Broadcastify-only block above: this duplicate is ours, not
          the service's. Two recorders record every call and reconciliation can move the
          primary after the first copy has already gone out; this is how many second copies
          were refused before anything was sent (docs/EXPORTERS.md, "One upload per
          transmission"). It should track the reconciliation swap rate, and a zero here on a
          two-recorder deployment means the guard is not running.
        */}
        <Stat label="Duplicates avoided (24 h)">
          <span title="Second copies of a transmission this deployment had already uploaded; nothing was sent">
            {formatNumber(view?.duplicate_uploads_24h ?? 0)}
          </span>
        </Stat>
        <ResponsesLine responses={view?.responses_24h} />
        <div className="col-span-2 sm:col-span-4">
          <dt className="text-ink-2">Last error</dt>
          <dd className="truncate text-ink" title={view?.last_error ?? undefined}>
            {view?.last_error ? `${view.last_error} (${formatRelative(view.last_error_at ?? null)})` : "none"}
          </dd>
        </div>
      </dl>

      {result ? (
        <div
          className={`border-b border-line px-4 py-2 text-[11px] leading-snug ${
            result.ok ? "text-ink-2" : "text-critical"
          }`}
        >
          <p>
            <strong>{result.ok ? "Credentials accepted" : "Problem"}:</strong> {result.detail} —{" "}
            <em>{result.checked}</em>
          </p>
          {result.used_draft ? <DraftNote keys={result.draft_keys} /> : null}
          {result.environment || result.clock_source ? (
            <p className="mt-1 text-muted">
              {result.environment ? <>environment {result.environment}</> : null}
              {result.environment && result.clock_source ? " · " : null}
              {result.clock_source ? (
                <>
                  host clock {formatOffset(result.clock_offset_s)} via {result.clock_source}
                  {result.clock_ok === false ? " — out of tolerance" : null}
                </>
              ) : null}
            </p>
          ) : null}
        </div>
      ) : null}

      <Settings
        card={id}
        open={open}
        onOpenChange={setOpen}
        count={fields.length}
        dirtyCount={Object.keys(drafted).length}
      >
        {fields.map((setting) =>
          // The Calls system list is the one field with a *closed* set of valid answers,
          // and offering a conventional system in it would be offering a system whose
          // calls the gate then refuses. So it is a picker over the trunked ones.
          setting.key === CALLS_SYSTEMS_KEY ? (
            <SystemsField
              key={setting.key}
              setting={setting}
              value={value(setting.key)}
              disabled={!canWrite}
              systems={trunkedSystems}
              onChange={(next) => onChange(setting.key, next)}
            />
          ) : (
            <SettingField
              key={setting.key}
              setting={setting}
              value={value(setting.key)}
              disabled={!canWrite}
              onChange={(next) => onChange(setting.key, next)}
            />
          ),
        )}
      </Settings>
      {id === "broadcastify" ? (
        <p className="border-t border-line px-4 py-2 text-[11px] leading-snug text-muted">
          The talkgroup selection above applies to <strong>both Broadcastify products</strong>: what you
          exclude here is excluded from the Calls uploads and from the live Feed alike.
        </p>
      ) : null}
    </Card>
  );
}

/**
 * The knobs both services share, folded away like every other form on this page.
 */
function SharedCard({
  fields,
  drafted,
  canWrite,
  value,
  onChange,
}: {
  fields: SettingDefinition[];
  drafted: Record<string, string>;
  canWrite: boolean;
  value: (key: string) => string;
  onChange: (key: string, next: string) => void;
}) {
  const [open, setOpen] = useCardOpen("shared", Object.keys(drafted).length > 0);
  return (
    <Card>
      <CardHeader title="Rate and queue" hint={HELP["admin.exports.shared"]} />
      <Settings
        card="shared"
        open={open}
        onOpenChange={setOpen}
        count={fields.length}
        dirtyCount={Object.keys(drafted).length}
      >
        {fields.map((row) => (
          <SettingField
            key={row.key}
            setting={row}
            value={value(row.key)}
            disabled={!canWrite}
            onChange={(next) => onChange(row.key, next)}
          />
        ))}
      </Settings>
    </Card>
  );
}

/** Said on a test result: these values were the ones on the screen, not the stored ones. */
function DraftNote({ keys }: { keys: string[] }) {
  return (
    <span className="ml-1 text-muted">
      (tested with the unsaved {keys.length === 1 ? "value" : "values"} on this card
      {keys.length > 0 ? `: ${keys.join(", ")}` : ""} — nothing was saved)
    </span>
  );
}

/**
 * `export_broadcastify_systems` as a picker rather than a text box.
 *
 * The setting is a comma-separated list of local `systems.id`, and every other field on
 * this page is free text because its valid answers live on somebody else's server. This
 * one's do not: they are rows in this database, and only the **trunked** ones are
 * publishable at all. Typing an id here that Calls would refuse is a mistake the page can
 * simply not offer — and an empty selection keeps its meaning, "every system", which is
 * why it is a set of checkboxes rather than a required choice.
 *
 * The label carries the key in the same `font-mono` shape {@link SettingField} uses: this
 * page is the only home of the Export group, and a field that dropped the key would drop
 * out of the check that says so.
 */
function SystemsField({
  setting,
  value,
  disabled,
  systems,
  onChange,
}: {
  setting: SettingDefinition;
  value: string;
  disabled: boolean;
  systems: ExportStatus["trunked_systems"];
  onChange: (next: string) => void;
}) {
  const chosen = new Set(
    value
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean),
  );
  const toggle = (id: number, on: boolean) => {
    const next = new Set(chosen);
    if (on) next.add(String(id));
    else next.delete(String(id));
    onChange([...next].join(","));
  };
  return (
    <div className="min-w-0" data-setting={setting.key}>
      {/* The group's own label: the checkboxes below carry their own, so this one names
          the set rather than a control — and still shows the key, which is what says this
          page is the Export group's only home. */}
      <p className="mb-1 flex items-center gap-1 text-xs font-medium text-ink-2">
        {setting.label}
        <span className="font-mono text-[10px] text-muted">{setting.key}</span>
      </p>
      {systems.length === 0 ? (
        <p className="text-[11px] text-warning">
          No trunked system in this archive, so there is nothing to choose from.
        </p>
      ) : (
        <div className="flex flex-col gap-1">
          {systems.map((system) => (
            <label key={system.id} className="flex items-center gap-2 text-xs text-ink">
              <input
                type="checkbox"
                disabled={disabled}
                checked={chosen.has(String(system.id))}
                onChange={(event) => toggle(system.id, event.target.checked)}
              />
              {system.name}
              <span className="font-mono text-[10px] text-muted">#{system.id}</span>
            </label>
          ))}
        </div>
      )}
      <p className="mt-1 text-[11px] leading-snug text-muted">
        {chosen.size === 0 ? "Nothing ticked: every trunked system is published. " : ""}
        {setting.description}
      </p>
    </div>
  );
}

/**
 * Which set of credentials this card is talking about, and where to watch it.
 *
 * Rendered above the blurb rather than as one more field in the form, because it is the
 * one thing on this card that changes what every other field *means*: Broadcastify issues
 * development and production credentials separately and each endpoint refuses the other's,
 * so an API key read under the wrong environment is a wasted afternoon. The badge is loud
 * for the same reason — a card that is quietly pointed at the sandbox looks exactly like
 * one that is publishing.
 */
/** The service's own pages, for a card without an environment bar to carry them. */
function LinksBar({ view }: { view: ExportServiceStatus }) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2 text-[11px] text-ink-2">
      <span>{view.service === "openmhz" ? "OpenMHz" : view.service}</span>
      <span className="ml-auto flex items-center gap-3">
        {view.status_url ? (
          <a className="underline hover:text-ink" href={view.status_url} target="_blank" rel="noreferrer">
            System page
          </a>
        ) : null}
        {view.manage_url ? (
          <a className="underline hover:text-ink" href={view.manage_url} target="_blank" rel="noreferrer">
            Account management
          </a>
        ) : null}
      </span>
    </div>
  );
}

function EnvironmentBar({ view }: { view: ExportServiceStatus }) {
  const development = view.environment !== "production";
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2 text-[11px]">
      <span className="text-ink-2">Environment</span>
      <span
        className={`rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide ${
          development ? "bg-warning/20 text-warning" : "bg-surface-2 text-ink"
        }`}
      >
        {development ? "development" : "production"}
      </span>
      {development ? (
        <span className="text-warning">uploads go to the calls-dev sandbox, not the public archive</span>
      ) : null}
      <span className="ml-auto flex items-center gap-3">
        {view.status_url ? (
          <a className="underline hover:text-ink" href={view.status_url} target="_blank" rel="noreferrer">
            Status page
          </a>
        ) : null}
        {view.manage_url ? (
          <a className="underline hover:text-ink" href={view.manage_url} target="_blank" rel="noreferrer">
            Node management
          </a>
        ) : null}
        {view.encoding ? <span className="text-muted">encoding {view.encoding}</span> : null}
      </span>
    </div>
  );
}

/** A signed offset in seconds, or an em dash when there is nothing to show. */
function formatOffset(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return "—";
  return `${seconds >= 0 ? "+" : ""}${seconds.toFixed(3)} s`;
}

/**
 * Step 1 of Broadcastify's workflow, on the card.
 *
 * Three states, and the third is the one worth being careful about: `clock_ok: null` means
 * the offset could not be *measured*, which is not a failure and never pauses an upload.
 * Showing it as a problem would send an operator hunting for a clock fault when the real
 * answer is "the NTP server did not answer".
 */
function ClockReading({ view }: { view: ExportServiceStatus }) {
  if (view.clock_ok === null || view.clock_ok === undefined) {
    return (
      <span className="text-muted" title="The offset could not be measured; nothing is paused">
        not measured
      </span>
    );
  }
  const bad = !view.clock_ok;
  return (
    <span
      className={bad ? "text-warning" : undefined}
      title={
        bad
          ? "More than 2 s from the reference. Past 5 s the Calls uploads pause: `ts` is part of the tuple Broadcastify deduplicates on."
          : "Within 2 s of the reference"
      }
    >
      {formatOffset(view.clock_offset_s)}
      {view.clock_source ? <span className="text-muted"> ({view.clock_source})</span> : null}
    </span>
  );
}

/**
 * The last 24 hours of answers, one `class: count` pair each, biggest first.
 *
 * The service's own words, bucketed server-side so the per-call tail — the upload id, the
 * skew in seconds — does not give every row its own entry (docs/EXPORTERS.md,
 * "Responses"). Renders nothing at all when nothing has been answered: an empty row would
 * read as "no responses" where the truth is "no attempts".
 */
function ResponsesLine({ responses }: { responses?: Record<string, number> }) {
  const pairs = Object.entries(responses ?? {}).sort((a, b) => b[1] - a[1]);
  if (pairs.length === 0) return null;
  return (
    <div className="col-span-2 sm:col-span-4">
      <dt className="text-ink-2">Responses (24 h)</dt>
      <dd className="truncate text-ink" title={pairs.map(([name, n]) => `${name}: ${n}`).join(", ")}>
        {pairs.map(([name, count], index) => (
          <span key={name}>
            {index > 0 ? <span className="text-muted"> · </span> : null}
            <span className="text-ink-2">{name}:</span> {formatNumber(count)}
          </span>
        ))}
      </dd>
    </div>
  );
}

function Stat({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-ink-2">{label}</dt>
      <dd className="truncate text-ink">{children}</dd>
    </div>
  );
}

/**
 * One runtime setting, rendered by its `kind` — the same five shapes the Settings tab
 * uses, kept here rather than shared because this page groups them by service and that
 * one groups them by `group`.
 */
function SettingField({
  setting,
  value,
  disabled,
  onChange,
}: {
  setting: SettingDefinition;
  value: string;
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  const id = `export-setting-${setting.key}`;
  return (
    <div className="min-w-0">
      <label htmlFor={id} className="mb-1 flex items-center gap-1 text-xs font-medium text-ink-2">
        {setting.label}
        <span className="font-mono text-[10px] text-muted">{setting.key}</span>
      </label>
      {setting.kind === "bool" ? (
        <Switch
          checked={value.toLowerCase() === "true"}
          disabled={disabled}
          onCheckedChange={(next) => onChange(next ? "true" : "false")}
          label={value.toLowerCase() === "true" ? "on" : "off"}
          tip={setting.description}
        />
      ) : setting.kind === "enum" ? (
        <Select
          value={value}
          onValueChange={onChange}
          options={(setting.options ?? []).map((option) => ({ value: option, label: option }))}
          tip={setting.description}
          ariaLabel={setting.label}
          disabled={disabled}
          className="w-full"
        />
      ) : setting.kind === "secret" ? (
        // Write-only, exactly as the Settings tab handles it: the API never returns the
        // stored value, so an empty box means "unchanged", not "empty".
        <input
          id={id}
          type="password"
          autoComplete="new-password"
          disabled={disabled}
          placeholder={setting.is_default ? "not set" : "set — type to replace"}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className={inputClass}
        />
      ) : setting.kind === "text" ? (
        <TextArea
          id={id}
          rows={2}
          disabled={disabled}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          id={id}
          type={setting.kind === "string" ? "text" : "number"}
          step={setting.kind === "float" ? "0.01" : "1"}
          disabled={disabled}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className={inputClass}
        />
      )}
      <p className="mt-1 text-[11px] leading-snug text-muted">{setting.description}</p>
    </div>
  );
}

function RecentUploads({
  rows,
  loading,
  canWrite,
}: {
  rows: ExportLedgerRow[];
  loading: boolean;
  canWrite: boolean;
}) {
  const retry = useRetryExport();
  return (
    <Card>
      <CardHeader title="Recent uploads" hint={HELP["admin.exports.recent"]} />
      {loading ? (
        <SkeletonText lines={6} className="p-4" />
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing has been exported"
          hint="No ledger rows yet — both services ship switched off."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-ink-2">
              <tr className="border-b border-line">
                <th className="px-3 py-2 font-medium">Call</th>
                <th className="px-3 py-2 font-medium">Talkgroup</th>
                <th className="px-3 py-2 font-medium">Service</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 text-right font-medium">Attempts</th>
                <th className="px-3 py-2 font-medium">When</th>
                <th className="px-3 py-2 font-medium">Detail</th>
                <th className="px-3 py-2">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-b border-line/60">
                  <td className="px-3 py-1.5 tabular-nums text-ink">{row.call_id}</td>
                  <td className="px-3 py-1.5 text-ink">{row.talkgroup_alias ?? row.talkgroup ?? "—"}</td>
                  <td className="px-3 py-1.5 text-ink-2">{row.service}</td>
                  <td className="px-3 py-1.5">
                    {/* The service's own line, where there is one: a `skipped` row that
                        says "already have it" and one we declined to send look identical
                        without it. */}
                    <StatusChip
                      status={row.status}
                      tip={
                        row.response ??
                        (BAD_STATUSES.has(row.status) ? "Parked after the last attempt" : row.status)
                      }
                    />
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">{row.attempts}</td>
                  <td className="px-3 py-1.5 text-ink-2">{formatRelative(row.updated_at)}</td>
                  <td
                    className="max-w-[24rem] truncate px-3 py-1.5 text-ink-2"
                    title={row.last_error ?? row.skip_reason ?? ""}
                  >
                    {row.last_error ?? row.skip_reason ?? "—"}
                  </td>
                  <td className="px-3 py-1.5 text-right">
                    {row.status === "failed" || row.status === "skipped" ? (
                      <Button
                        tip={canWrite ? HELP["admin.exports.retry"] : HELP["app.readOnly"]}
                        size="iconSm"
                        variant="ghost"
                        disabled={!canWrite || retry.isPending}
                        aria-label={`Retry export ${row.id}`}
                        onClick={() =>
                          retry.mutate(
                            { service: row.service, id: row.id },
                            {
                              onSuccess: () => toast("Queued for another attempt", { tone: "success" }),
                              onError: (error) => mutationErrorToast(error, "Could not retry that upload"),
                            },
                          )
                        }
                      >
                        <RotateCcw size={14} aria-hidden="true" />
                      </Button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
