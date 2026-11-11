import { Bell, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { useAlerts, useCurrentUser, useDeleteAlert, useNotifications, useSaveAlert } from "../api/hooks";
import { ALERT_CHANNELS, ALERT_KINDS, type AlertChannel, type AlertKind, type AlertRule } from "../api/types";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { Chip, StatusChip } from "../components/ui/Chip";
import { Dialog } from "../components/ui/Dialog";
import { Field, TextInput } from "../components/ui/Field";
import { Select } from "../components/ui/Select";
import { SkeletonText } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Switch } from "../components/ui/Switch";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { formatDateTime, formatRelative } from "../lib/format";
import { toast } from "../state/toasts";

const EMPTY: AlertRule = {
  id: 0,
  name: "",
  kind: "keyword",
  pattern: "",
  talkgroup_ids: [],
  radio_ids: [],
  channels: ["ntfy"],
  target: "",
  cooldown_s: 300,
  enabled: true,
  last_fired_at: null,
  fire_count: 0,
  created_by: null,
  created_at: "",
  filters: {},
  channel_config: {},
};

const KIND_HINT: Record<AlertKind, string> = {
  talkgroup: "Fires on any call on the chosen channels.",
  radio: "Fires whenever one of the chosen radios keys up.",
  keyword: "Fires when a transcript contains any of these comma-separated words or phrases.",
  regex: "Fires when a transcript matches this regular expression.",
  channel_audit:
    "Fires once when a site starts using a frequency RadioReference does not document, or announces a neighboring tower Keyup does not know. Needs no pattern.",
  new_kind:
    "Fires when a site's control channel transmits a message kind (mfid:opcode) it never has before. Needs no pattern; at most one alert per kind per day.",
  exemplar_changed:
    "Fires when the undecoded bytes of a message a site sends constantly — a beacon — stop being the bytes they were. Needs no pattern; at most one alert per kind per day.",
  disk_low:
    "Fires when free disk space drops under the warning threshold, and again under the critical one (Admin → Settings → Retention). Needs no pattern; once per crossing.",
  health:
    "Fires once when a hot recorder goes silent, calls stop arriving, live transcription stalls or the database backup is overdue, and once more when it recovers (thresholds in Admin → Settings → Health). Pattern: optionally the checks to hear — recorder_silent, ingest_lag, stt_backlog, backup_stale; blank = all.",
  emergency:
    "Fires on a call whose radio sent the P25 emergency flag. Pattern (optional): talkgroup numbers and categories to watch, a leading minus to exclude — e.g. law, fire, -1507. Blank = every emergency.",
};

const PLACEHOLDER: Partial<Record<AlertKind, string>> = {
  regex: "\\b(pursuit|10-80)\\b",
  health: "recorder_silent, ingest_lag",
  emergency: "law, fire, -1507",
};

/** Rules that watch incoming calls, and the log of what they have sent. */
export function Alerts() {
  const { canWrite } = useCurrentUser();
  const alerts = useAlerts();
  const notifications = useNotifications();
  const saveAlert = useSaveAlert();
  const deleteAlert = useDeleteAlert();
  const [editing, setEditing] = useState<AlertRule | null>(null);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Alerts"
        description="Tell Keyup what to watch for and where to send it. Every rule has a cooldown, so one busy incident is not fifty notifications."
        actions={
          <Button
            tip={canWrite ? HELP["alerts.create"] : HELP["app.readOnly"]}
            size="sm"
            variant="primary"
            disabled={!canWrite}
            onClick={() => setEditing({ ...EMPTY })}
          >
            <Plus size={14} aria-hidden="true" /> New rule
          </Button>
        }
      />

      {/* With no rules yet nothing in here takes focus, so the scroll box itself must. */}
      <section
        className="min-h-0 flex-1 space-y-4 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6"
        aria-label="Alert rules"
        tabIndex={0}
      >
        <Card>
          <CardHeader title="Rules" hint={HELP["alerts.table"]} />
          {alerts.error ? (
            <ErrorState error={alerts.error} onRetry={() => void alerts.refetch()} />
          ) : alerts.isLoading ? (
            <SkeletonText lines={5} className="p-4" />
          ) : (alerts.data ?? []).length === 0 ? (
            <EmptyState
              title="No alert rules yet"
              hint="A rule can watch a channel, a radio, a set of keywords, or a regular expression."
              icon={<Bell size={28} />}
            />
          ) : (
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Alert rules with their triggers, delivery and cooldowns</caption>
              <thead>
                <tr className="text-xs text-ink-2">
                  <Th help="alerts.table.name">Name</Th>
                  <Th help="alerts.table.kind">Kind</Th>
                  <Th help="alerts.form.pattern">Pattern</Th>
                  <Th help="alerts.table.channels">Channels</Th>
                  <Th help="alerts.table.cooldown">Cooldown</Th>
                  <Th help="alerts.table.lastFired">Last fired</Th>
                  <Th help="alerts.table.enabled">On</Th>
                  <th scope="col" className="px-3 py-2">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {(alerts.data ?? []).map((rule) => (
                  <tr key={rule.id} className="border-t border-line hover:bg-surface-2">
                    <td className="px-3 py-2 font-medium text-ink">{rule.name}</td>
                    <td className="px-3 py-2 text-ink-2">{rule.kind}</td>
                    <td className="max-w-64 truncate px-3 py-2 font-mono text-[11px] text-ink-2">
                      {rule.pattern}
                    </td>
                    <td className="px-3 py-2">
                      <span className="flex flex-wrap gap-1">
                        {rule.channels.map((channel) => (
                          <StatusChip key={channel} status={channel} tip={HELP["alerts.table.channels"]} />
                        ))}
                      </span>
                    </td>
                    <td className="px-3 py-2 tabular-nums text-ink-2">{rule.cooldown_s}s</td>
                    <td className="px-3 py-2 text-xs text-ink-2">
                      {rule.last_fired_at ? formatRelative(rule.last_fired_at) : "never"}
                      <span className="ml-1 text-muted">({rule.fire_count})</span>
                    </td>
                    <td className="px-3 py-2">
                      <Switch
                        checked={rule.enabled}
                        disabled={!canWrite}
                        onCheckedChange={(next) =>
                          saveAlert.mutate({
                            id: rule.id,
                            rule: { ...rule, enabled: next },
                          })
                        }
                        label={null}
                        tip={HELP["alerts.table.enabled"]}
                      />
                    </td>
                    <td className="px-3 py-2 text-right">
                      <span className="inline-flex gap-0.5">
                        <Button
                          tip={canWrite ? HELP["alerts.edit"] : HELP["app.readOnly"]}
                          size="iconSm"
                          variant="ghost"
                          disabled={!canWrite}
                          aria-label={`Edit ${rule.name}`}
                          onClick={() => setEditing(rule)}
                        >
                          <Pencil size={14} aria-hidden="true" />
                        </Button>
                        <Button
                          tip={canWrite ? HELP["alerts.delete"] : HELP["app.readOnly"]}
                          size="iconSm"
                          variant="ghost"
                          disabled={!canWrite}
                          aria-label={`Delete ${rule.name}`}
                          onClick={() =>
                            deleteAlert.mutate(rule.id, {
                              onSuccess: () => toast("Rule deleted", { tone: "success" }),
                            })
                          }
                        >
                          <Trash2 size={14} aria-hidden="true" />
                        </Button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card>
          <CardHeader title="Notification log" hint={HELP["alerts.notifications"]} />
          {notifications.isLoading ? (
            <SkeletonText lines={5} className="p-4" />
          ) : (notifications.data?.items ?? []).length === 0 ? (
            <EmptyState title="Nothing has been sent yet" />
          ) : (
            <ul className="divide-y divide-line">
              {(notifications.data?.items ?? []).map((record) => (
                <li key={record.id} className="flex flex-wrap items-center gap-2 px-4 py-2 text-xs">
                  <StatusChip status={record.status} tip={HELP["alerts.notifications.status"]} />
                  <span className="font-medium text-ink">{record.rule_name}</span>
                  <span className="text-muted">{record.channel}</span>
                  {record.call_id ? (
                    <Tip label={HELP["alerts.notifications.call"]}>
                      <Link to={`/calls/${record.call_id}`} className="text-accent-ink hover:underline">
                        call {record.call_id}
                      </Link>
                    </Tip>
                  ) : null}
                  <span className="min-w-0 flex-1 truncate text-ink-2">
                    {record.error ?? record.excerpt ?? ""}
                  </span>
                  <span className="shrink-0 tabular-nums text-muted">
                    {formatDateTime(record.created_at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </section>

      <AlertDialog
        rule={editing}
        open={editing !== null}
        onOpenChange={(open) => !open && setEditing(null)}
        onSave={(rule) =>
          saveAlert.mutate(
            { ...(rule.id ? { id: rule.id } : {}), rule },
            {
              onSuccess: () => {
                toast("Rule saved", { tone: "success" });
                setEditing(null);
              },
              onError: () => toast("Could not save the rule", { tone: "error" }),
            },
          )
        }
      />
    </div>
  );
}

function Th({ help, children }: { help: keyof typeof HELP; children: string }) {
  return (
    <th scope="col" className="px-3 py-2 font-semibold">
      <Tip label={HELP[help]}>
        <span tabIndex={0}>{children}</span>
      </Tip>
    </th>
  );
}

function AlertDialog({
  rule,
  open,
  onOpenChange,
  onSave,
}: {
  rule: AlertRule | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (rule: AlertRule) => void;
}) {
  const [draft, setDraft] = useState<AlertRule>(rule ?? EMPTY);
  // Re-seed whenever a different rule is opened.
  const [seededFor, setSeededFor] = useState<number | null>(rule?.id ?? null);
  if (rule && seededFor !== rule.id) {
    setSeededFor(rule.id);
    setDraft(rule);
  }
  if (!rule) return null;

  const set = <K extends keyof AlertRule>(key: K, value: AlertRule[K]) =>
    setDraft((previous) => ({ ...previous, [key]: value }));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={rule.id ? `Edit ${rule.name}` : "New alert rule"}
      description="A rule matches incoming calls and delivers a notification, at most once per cooldown."
      footer={
        <>
          <Button tip="Close without saving" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["alerts.form.save"]}
            size="sm"
            variant="primary"
            disabled={!draft.name.trim()}
            onClick={() => onSave(draft)}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name" help="alerts.form.name" className="sm:col-span-2">
          {(id) => (
            <TextInput
              id={id}
              value={draft.name}
              placeholder="Structure fire keyword"
              onChange={(event) => set("name", event.target.value)}
            />
          )}
        </Field>

        <Field label="Kind" help="alerts.form.kind">
          {() => (
            <Select
              value={draft.kind}
              onValueChange={(value) => set("kind", value as AlertKind)}
              options={ALERT_KINDS.map((kind) => ({ value: kind, label: kind }))}
              tip={HELP["alerts.form.kind"]}
              ariaLabel="Alert kind"
              className="w-full"
            />
          )}
        </Field>

        <Field label="Cooldown (seconds)" help="alerts.form.cooldown">
          {(id) => (
            <TextInput
              id={id}
              type="number"
              min={0}
              value={draft.cooldown_s}
              onChange={(event) => set("cooldown_s", Number(event.target.value))}
            />
          )}
        </Field>

        <Field
          label="Pattern"
          help="alerts.form.pattern"
          hint={KIND_HINT[draft.kind]}
          className="sm:col-span-2"
        >
          {(id) => (
            <TextInput
              id={id}
              value={draft.pattern ?? ""}
              className="font-mono text-xs"
              placeholder={PLACEHOLDER[draft.kind] ?? "structure fire, second alarm"}
              onChange={(event) => set("pattern", event.target.value)}
            />
          )}
        </Field>

        <div className="sm:col-span-2">
          <p className="mb-1 text-xs font-medium text-ink-2">Channels</p>
          <div className="flex flex-wrap gap-1.5">
            {ALERT_CHANNELS.map((channel) => (
              <Chip
                key={channel}
                label={channel}
                tip={HELP["alerts.form.channels"]}
                active={draft.channels.includes(channel)}
                onClick={() =>
                  set(
                    "channels",
                    draft.channels.includes(channel)
                      ? draft.channels.filter((value) => value !== channel)
                      : ([...draft.channels, channel] as AlertChannel[]),
                  )
                }
              />
            ))}
          </div>
        </div>

        <Field label="Deliver to" help="alerts.form.target" className="sm:col-span-2">
          {(id) => (
            <TextInput
              id={id}
              value={draft.target}
              placeholder="https://ntfy.sh/keyup-fire"
              onChange={(event) => set("target", event.target.value)}
            />
          )}
        </Field>

        <div className="sm:col-span-2">
          <Switch
            checked={draft.enabled}
            onCheckedChange={(next) => set("enabled", next)}
            label="Start sending as soon as it is saved"
            tip={HELP["alerts.form.enabled"]}
          />
        </div>
      </div>
    </Dialog>
  );
}
