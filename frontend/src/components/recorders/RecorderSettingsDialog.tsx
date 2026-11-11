import { Eye, EyeOff, Plus, RefreshCw, X } from "lucide-react";
import { useState } from "react";
import { errorDetail } from "../../api/client";
import { useGenerateRecorderLinkKey, useRecorderLinkKey, useSaveRecorderPolicy } from "../../api/hooks";
import type { Recorder, RecorderDesiredLink, RecorderLinkInput, RecorderPolicyInput } from "../../api/types";
import { HELP } from "../../help/help";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, FieldGrid, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { Switch } from "../ui/Switch";
import { Tip } from "../ui/Tooltip";
import { StationSection } from "./ReportedFacts";
import { Restarts, rejectedFor, Section } from "./TunerEditDialog";

/**
 * Everything keyup may tell a recorder *as a whole*, in one dialog — the instance-level twin
 * of {@link TunerEditDialog}, and deliberately built out of the same parts.
 *
 * Three rules run through it, and they are the tuner dialog's rules one level up:
 *
 * * **Every setting is three-valued, and the third value is the important one.** "Leave it
 *   to the recorder's own config" is not the absence of a choice, it is a choice: a recorder
 *   keyup has never been told anything about is running on its own TOML, and taking a
 *   setting over is a decision with a cost — the operator can no longer fix it on the box.
 *   So each control offers *managed on / managed off / leave to config* rather than a
 *   switch that has already quietly taken it over by rendering.
 * * **The recorder's echo is the authority on what is true.** What keyup wants is what this
 *   form holds; what the recorder has *applied* comes back in `recorder_applied`, and the
 *   two differing is what the pending badges say. A saved setting is not an applied one
 *   until the recorder's next poll.
 * * **Refusals are shown where the value is.** `recorder_rejected` is the only trace a
 *   refusal leaves — the recorder acknowledges the version whether or not it could use
 *   every field — so each reason is rendered under the control it belongs to.
 *
 * The shared link key is the one thing here that is not simply a value: it is a secret the
 * page must be able to *set* and to *copy to the other recorder* without ever carrying it in
 * an ordinary list. So the dialog shows a fingerprint by default, fetches the key only when
 * the operator presses reveal, and omits `key` from the save unless it actually has a new
 * one — which is what tells the API to keep the stored one.
 */
export function RecorderSettingsDialog({ recorder, onClose }: { recorder: Recorder; onClose: () => void }) {
  const save = useSaveRecorderPolicy();
  const generate = useGenerateRecorderLinkKey();
  const applied = recorder.recorder_applied ?? null;
  const rejected = recorder.recorder_rejected ?? null;
  const restartOn = new Set(recorder.recorder_capabilities?.restart_on ?? []);
  const managedLink = recorder.desired_link;

  const [instanceId, setInstanceId] = useState(recorder.instance_id);
  const [spoolOnly, setSpoolOnly] = useState<Tri>(tri(recorder.desired_spool_only));
  const [archive, setArchive] = useState<Tri>(tri(recorder.desired_event_log_archive));

  const [linkManaged, setLinkManaged] = useState(managedLink !== null);
  const [enabled, setEnabled] = useState(managedLink?.enabled ?? false);
  const [bind, setBind] = useState(managedLink?.bind ?? "0.0.0.0:9110");
  const [peers, setPeers] = useState<string[]>(managedLink?.peers ?? []);
  const [hello, setHello] = useState(String(managedLink?.hello_interval_s ?? 5));
  const [accept, setAccept] = useState(managedLink?.accept_remote_grants ?? false);
  const [publish, setPublish] = useState(managedLink?.publish_grants ?? false);
  const [defer, setDefer] = useState(managedLink?.defer_to_claims ?? false);

  // The key: masked until asked for, and *typed* only after that. `keyTouched` is what
  // decides whether `key` goes in the body at all — an untouched key is left out, which is
  // how the API is told to keep the stored one.
  const [revealed, setRevealed] = useState(false);
  const [keyValue, setKeyValue] = useState("");
  const [keyTouched, setKeyTouched] = useState(false);
  const secret = useRecorderLinkKey(recorder.id, revealed && !keyTouched);
  const shownKey = keyTouched ? keyValue : (secret.data?.key ?? "");

  const onSave = () => {
    const body: RecorderPolicyInput = {};
    if (instanceId.trim() !== recorder.instance_id) body.instance_id = instanceId.trim();
    if (spoolOnly !== tri(recorder.desired_spool_only)) body.spool_only = fromTri(spoolOnly);
    if (archive !== tri(recorder.desired_event_log_archive)) {
      body.event_log_archive = fromTri(archive);
    }

    if (!linkManaged) {
      // Only a *change* is written: an unmanaged link left unmanaged must not send a null
      // that the API would read as a write and bump the version for.
      if (managedLink !== null) body.link = null;
    } else {
      const interval = Number(hello);
      if (!Number.isInteger(interval)) {
        toast("The hello interval must be a whole number of seconds", { tone: "error" });
        return;
      }
      const cleaned = peers.map((peer) => peer.trim()).filter((peer) => peer !== "");
      const link: RecorderLinkInput = {
        enabled,
        bind: bind.trim(),
        peers: cleaned,
        hello_interval_s: interval,
        accept_remote_grants: accept,
        publish_grants: publish,
        defer_to_claims: defer,
      };
      if (keyTouched) link.key = keyValue.trim();
      else if (!managedLink?.key_set) {
        toast("The link needs a shared key", {
          tone: "error",
          body: "Generate one and paste the same value into every recorder in the mesh.",
        });
        return;
      }
      // Sent only when it is actually different from what is stored. The API would compare
      // too and refuse to bump for a no-op — but the body is also what an operator sees in a
      // network log, and "opening the dialog rewrote the link" is a claim not worth making.
      if (keyTouched || managedLink === null || !sameLink(managedLink, link)) body.link = link;
    }

    if (Object.keys(body).length === 0) {
      onClose();
      return;
    }

    save.mutate(
      { recorderId: recorder.id, policy: body },
      {
        onSuccess: () => {
          toast("Recorder settings saved", {
            tone: "success",
            body: "The recorder applies them on its next poll.",
          });
          onClose();
        },
        // Staying open on a refusal is the point: the API answers `field: reason`, and the
        // operator needs the form in front of them to fix the field it names.
        onError: (error) =>
          toast("Could not save the recorder settings", {
            tone: "error",
            body: errorDetail(error),
          }),
      },
    );
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={`Settings for ${recorder.label ?? recorder.instance_id}`}
      description="What keyup wants of this recorder itself, rather than of one of its tuners."
      className="w-[min(45rem,calc(100vw-2rem))]"
      footer={
        <>
          <Button variant="ghost" tip="Discard these changes" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            tip={HELP["admin.recorder.settings.save"]}
            disabled={save.isPending}
            onClick={onSave}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <StationSection station={recorder.station} />

        <Section
          title="Instance"
          hint={
            <>
              The name this recorder files everything under. Everything already filed under{" "}
              <span className="text-ink">{recorder.instance_id}</span> — its events, observations and archived
              logs — is rewritten to the new name in one transaction.{" "}
              <strong className="font-medium text-ink">Change it on the recorder too</strong>: one that keeps
              posting the old name simply creates a second row under it.
            </>
          }
        >
          <FieldGrid>
            <Field
              label="Instance name"
              help="admin.recorder.settings.instance"
              hint={rejectedFor(rejected, "instance_id")}
            >
              {(id) => (
                <TextInput
                  id={id}
                  value={instanceId}
                  onChange={(event) => setInstanceId(event.target.value)}
                />
              )}
            </Field>
          </FieldGrid>
        </Section>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Section title="Uplink hold" hint="Whether the recorder ships its calls or keeps them spooled.">
            <TriField
              label="Upload hold"
              help="admin.recorder.settings.spoolOnly"
              value={spoolOnly}
              onChange={setSpoolOnly}
              onLabel="Hold calls in the spool"
              offLabel="Upload calls"
              applied={applied?.spool_only}
              pending={recorder.recorder_pending}
              desired={fromTri(spoolOnly)}
              rejected={rejectedFor(rejected, "spool_only")}
              restarts={<Restarts on={restartOn} field="spool_only" />}
            />
          </Section>
          <Section title="Event log" hint="Whether the recorder's daily log is archived here.">
            <TriField
              label="Event-log archive"
              help="admin.recorder.settings.archive"
              value={archive}
              onChange={setArchive}
              onLabel="Ship the daily log here"
              offLabel="Keep it on the recorder"
              applied={applied?.event_log_archive}
              pending={recorder.recorder_pending}
              desired={fromTri(archive)}
              rejected={rejectedFor(rejected, "event_log_archive")}
              restarts={<Restarts on={restartOn} field="event_log_archive" />}
            />
          </Section>
        </div>

        <Section
          title="Link"
          hint="The mesh this recorder forms with the others hearing the same system, so two boxes do not both record one grant."
        >
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
            <Switch
              checked={linkManaged}
              onCheckedChange={setLinkManaged}
              label={
                <Restarts on={restartOn} field="link">
                  Keyup manages the link
                </Restarts>
              }
              tip={HELP["admin.recorder.settings.linkManaged"]}
            />
            {linkManaged ? (
              <>
                <Switch
                  checked={enabled}
                  onCheckedChange={setEnabled}
                  label="Link enabled"
                  tip={HELP["admin.recorder.settings.linkEnabled"]}
                />
                <Switch
                  checked={accept}
                  onCheckedChange={setAccept}
                  label="Accept remote grants"
                  tip={HELP["admin.recorder.settings.linkAccept"]}
                />
                <Switch
                  checked={publish}
                  onCheckedChange={setPublish}
                  label="Publish grants"
                  tip={HELP["admin.recorder.settings.linkPublish"]}
                />
                <Switch
                  checked={defer}
                  onCheckedChange={setDefer}
                  label="Defer to claims"
                  tip={HELP["admin.recorder.settings.linkDefer"]}
                />
              </>
            ) : null}
            {rejectedFor(rejected, "link")}
          </div>
          {!linkManaged ? (
            <p className="text-[11px] text-ink-2" data-testid="link-unmanaged">
              The recorder stays on whatever its own configuration says. Turning this on takes the whole block
              over — bind address, peers and key together, because half a link is not one.
            </p>
          ) : (
            <>
              <Field
                label="Shared key"
                help="admin.recorder.settings.linkKey"
                hint={
                  <>
                    Every recorder in the mesh needs the <em>same</em> key.
                    {managedLink?.key_fingerprint ? (
                      <>
                        {" "}
                        Stored fingerprint{" "}
                        <span className="font-mono text-ink">{managedLink.key_fingerprint}</span>.
                      </>
                    ) : null}
                  </>
                }
              >
                {(id) => (
                  <div className="flex flex-wrap items-center gap-2">
                    <TextInput
                      id={id}
                      className="w-full min-w-48 flex-1 font-mono sm:w-auto"
                      type={revealed || keyTouched ? "text" : "password"}
                      value={revealed || keyTouched ? shownKey : maskFor(managedLink?.key_set)}
                      readOnly={!revealed && !keyTouched}
                      placeholder={managedLink?.key_set ? "" : "generate one →"}
                      onChange={(event) => {
                        setKeyTouched(true);
                        setKeyValue(event.target.value);
                      }}
                    />
                    <Button
                      size="sm"
                      variant="ghost"
                      tip={HELP["admin.recorder.settings.linkKeyReveal"]}
                      disabled={!managedLink?.key_set && !keyTouched}
                      onClick={() => setRevealed((was) => !was)}
                    >
                      {revealed ? (
                        <EyeOff size={13} aria-hidden="true" />
                      ) : (
                        <Eye size={13} aria-hidden="true" />
                      )}
                      <span className="ml-1">{revealed ? "Hide" : "Reveal"}</span>
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      tip={HELP["admin.recorder.settings.linkKeyGenerate"]}
                      disabled={generate.isPending}
                      onClick={() =>
                        generate.mutate(
                          { recorderId: recorder.id },
                          {
                            onSuccess: (result) => {
                              setKeyTouched(true);
                              setRevealed(true);
                              setKeyValue(result.key);
                              toast("Key generated", {
                                tone: "success",
                                body: "Nothing is saved yet — paste the same key into every recorder in the mesh.",
                              });
                            },
                            onError: (error) =>
                              toast("Could not generate a key", {
                                tone: "error",
                                body: errorDetail(error),
                              }),
                          },
                        )
                      }
                    >
                      <RefreshCw size={13} aria-hidden="true" />
                      <span className="ml-1">Generate</span>
                    </Button>
                  </div>
                )}
              </Field>
              <PeersEditor peers={peers} onChange={setPeers} />
              <AppliedLink recorder={recorder} pending={recorder.recorder_pending} />
              <Section title="Advanced" collapsible defaultOpen={false}>
                <FieldGrid>
                  <Field
                    label="Bind address"
                    help="admin.recorder.settings.linkBind"
                    hint="host:port — a hostname, an IPv4 address, or an IPv6 one in brackets."
                  >
                    {(id) => (
                      <TextInput
                        id={id}
                        value={bind}
                        placeholder="0.0.0.0:9110"
                        onChange={(event) => setBind(event.target.value)}
                      />
                    )}
                  </Field>
                  <Field
                    label="Hello interval (s)"
                    help="admin.recorder.settings.linkHello"
                    hint="Between 1 and 60 seconds."
                  >
                    {(id) => (
                      <TextInput
                        id={id}
                        inputMode="numeric"
                        className="tabular-nums"
                        value={hello}
                        onChange={(event) => setHello(event.target.value)}
                      />
                    )}
                  </Field>
                </FieldGrid>
              </Section>
            </>
          )}
        </Section>

        {rejected && Object.keys(rejected).length > 0 ? (
          <p className="text-[11px] text-warning-ink" data-testid="recorder-rejected-summary">
            The recorder refused {Object.keys(rejected).length} setting
            {Object.keys(rejected).length === 1 ? "" : "s"} of the last change it applied. The reasons are
            under the fields above.
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ tri-state ------- */

/** Managed on, managed off, or left to the recorder's own configuration. */
type Tri = "on" | "off" | "unmanaged";

const tri = (value: boolean | null | undefined): Tri =>
  value === null || value === undefined ? "unmanaged" : value ? "on" : "off";

const fromTri = (value: Tri): boolean | null => (value === "unmanaged" ? null : value === "on");

/**
 * Whether the form's link is the one already stored — everything but the key, which the
 * form does not hold unless the operator changed it and which is compared by having been
 * touched instead.
 */
function sameLink(stored: RecorderDesiredLink, wanted: RecorderLinkInput): boolean {
  return (
    stored.enabled === wanted.enabled &&
    stored.bind === wanted.bind &&
    stored.hello_interval_s === wanted.hello_interval_s &&
    stored.accept_remote_grants === wanted.accept_remote_grants &&
    stored.publish_grants === wanted.publish_grants &&
    stored.defer_to_claims === wanted.defer_to_claims &&
    stored.peers.length === wanted.peers.length &&
    stored.peers.every((peer, index) => peer === wanted.peers[index])
  );
}

/**
 * One three-valued setting, with what the recorder says it actually applied beside it.
 *
 * A `Select` rather than a switch plus a checkbox, because the three states are one
 * decision and not two: "hold the uploads" and "keyup does not have an opinion about the
 * uploads" are alternatives, and a UI that made the second a modifier of the first would
 * invite the reading that an unmanaged setting is really an off one.
 */
function TriField({
  label,
  help,
  value,
  onChange,
  onLabel,
  offLabel,
  applied,
  desired,
  pending,
  rejected,
  restarts,
}: {
  label: string;
  help: Parameters<typeof Field>[0]["help"];
  value: Tri;
  onChange: (value: Tri) => void;
  onLabel: string;
  offLabel: string;
  applied: boolean | null | undefined;
  desired: boolean | null;
  pending: boolean;
  rejected: React.ReactNode;
  restarts: React.ReactNode;
}) {
  // Pending is per field, not per block: the recorder lags on *everything* while its version
  // lags, but only a field whose applied value differs from the desired one has anything to
  // say about it — the rest are already where they are going.
  const drifting = pending && desired !== null && (applied ?? null) !== desired;
  return (
    <Field
      label={label}
      badge={
        <span className="inline-flex items-center gap-1.5">
          {restarts}
          {drifting ? (
            <Tip label="Saved here; the recorder takes it up on its next poll">
              <span className="rounded-full border border-warning px-1.5 py-0.5 text-[10px] font-normal text-ink">
                pending → {desired ? onLabel : offLabel}
              </span>
            </Tip>
          ) : null}
        </span>
      }
      help={help}
      hint={
        <>
          {applied === null || applied === undefined ? (
            <>The recorder has not said what it applied.</>
          ) : (
            <>
              Applied on the recorder: <span className="text-ink">{applied ? onLabel : offLabel}</span>.
            </>
          )}
          {rejected}
        </>
      }
    >
      {() => (
        <Select
          value={value}
          onValueChange={(next) => onChange(next as Tri)}
          options={[
            { value: "on", label: onLabel },
            { value: "off", label: offLabel },
            { value: "unmanaged", label: "Leave it to the recorder's config" },
          ]}
          ariaLabel={label}
          tip={HELP[help]}
          className="w-full"
        />
      )}
    </Field>
  );
}

/* ---------------------------------------------------------------------- peers ------- */

/** The peer list, one address a row. Small on purpose: a mesh is two or three boxes. */
function PeersEditor({ peers, onChange }: { peers: string[]; onChange: (peers: string[]) => void }) {
  return (
    <Field
      label="Peers"
      help="admin.recorder.settings.linkPeers"
      hint="Each as host:port — the address that recorder binds, as this one reaches it. An enabled link needs at least one."
    >
      {() => (
        <div className="space-y-2">
          {peers.map((peer, index) => (
            // The index is the identity here, and has to be: the rows are edited in place and
            // two of them may legitimately hold the same half-typed text for a keystroke.
            // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional and edited in place
            <div key={index} className="flex items-center gap-2">
              <TextInput
                aria-label={`Peer ${index + 1}`}
                className="min-w-0 flex-1"
                value={peer}
                placeholder="signal:9110"
                onChange={(event) => {
                  const next = [...peers];
                  next[index] = event.target.value;
                  onChange(next);
                }}
              />
              <Button
                size="iconSm"
                variant="ghost"
                aria-label={`Remove peer ${index + 1}`}
                tip="Remove this peer"
                onClick={() => onChange(peers.filter((_, at) => at !== index))}
              >
                <X size={13} aria-hidden="true" />
              </Button>
            </div>
          ))}
          <Button size="sm" variant="ghost" tip="Add another peer" onClick={() => onChange([...peers, ""])}>
            <Plus size={13} aria-hidden="true" />
            <span className="ml-1">Add peer</span>
          </Button>
        </div>
      )}
    </Field>
  );
}

/* -------------------------------------------------------------------- applied ------- */

/**
 * What the recorder says its link is actually set to.
 *
 * Worth its own line rather than folded into each input: the link is applied as a unit, so
 * "the recorder is running a different link from the one on screen" is one fact and not
 * seven. The key fingerprint is the interesting half — a mesh whose members' fingerprints
 * differ is a mesh that will never come up, and no other view can say so.
 */
function AppliedLink({ recorder, pending }: { recorder: Recorder; pending: boolean }) {
  const link = recorder.recorder_applied?.link ?? null;
  if (!link) {
    return (
      <p className="text-[11px] text-ink-2" data-testid="link-applied">
        The recorder has not said what link it applied.
      </p>
    );
  }
  const peers = Array.isArray(link.peers) ? link.peers.length : 0;
  return (
    <p className="text-[11px] text-ink-2" data-testid="link-applied">
      Applied on the recorder: {link.enabled ? "on" : "off"}, bound to{" "}
      <span className="text-ink">{String(link.bind ?? "—")}</span>, {peers} peer
      {peers === 1 ? "" : "s"}
      {link.key_fingerprint ? (
        <Tip label={HELP["admin.recorder.settings.fingerprint"]}>
          <span className="ml-1">
            , key <span className="font-mono text-ink">{String(link.key_fingerprint)}</span>
          </span>
        </Tip>
      ) : null}
      .
      {pending ? (
        <span className="ml-2 rounded-full border border-warning px-1.5 py-0.5 text-[10px] text-ink">
          pending
        </span>
      ) : null}
    </p>
  );
}

/** Dots standing in for a stored key, so the field is not empty when there is one. */
const maskFor = (keySet: boolean | undefined): string => (keySet ? "••••••••••••••••" : "");
