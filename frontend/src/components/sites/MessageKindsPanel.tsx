import { useMemo, useState } from "react";
import { useCurrentUser, useSaveSiteMessageKind, useSiteMessageKinds } from "../../api/hooks";
import type { SiteMessageKind } from "../../api/types";
import { HELP } from "../../help/help";
import { formatNumber, formatRelative } from "../../lib/format";
import { Card, CardHeader } from "../ui/Card";
import { Chip } from "../ui/Chip";
import { InlineSwitch, InlineText } from "../ui/InlineCell";
import { SkeletonText } from "../ui/Skeleton";
import { EmptyState } from "../ui/States";

/** How recently a kind must have first appeared to still be worth pointing at. */
const NEW_FOR_DAYS = 7;

function isNew(row: SiteMessageKind): boolean {
  const first = Date.parse(row.first_seen_at);
  return Number.isFinite(first) && Date.now() - first < NEW_FOR_DAYS * 86_400_000;
}

/**
 * Every `mfid:opcode` this site's control channel has ever transmitted.
 *
 * The Frequencies panel above answers "what is this tower using"; this one answers
 * "what is it *saying*", and specifically whether it has ever said it before. That
 * distinction is the whole reason the table exists: the message census
 * (`/message-mix`) is a rolling histogram and cannot tell a kind that appeared this
 * morning from one that has been there for a year — which is exactly the thing worth
 * knowing about a network that quietly turns a feature on
 * (docs/UNKNOWN-DATA-PLAN.md phase 5).
 *
 * Naming a kind is an admin act and the only editable thing here: everything else is
 * what the air did. A named kind keeps its row — the count and the exemplar are still
 * the evidence — it just stops being news, and drops out of the unknown-only view.
 */
export function MessageKindsPanel({ siteId }: { siteId: number | null }) {
  const { isAdmin } = useCurrentUser();
  const kinds = useSiteMessageKinds(siteId);
  const save = useSaveSiteMessageKind(siteId);
  const [unknownOnly, setUnknownOnly] = useState(false);
  const all = useMemo(() => kinds.data ?? [], [kinds.data]);
  const rows = unknownOnly ? all.filter((row) => !row.known) : all;
  const unknown = all.filter((row) => !row.known).length;

  return (
    <Card>
      <CardHeader
        title="Message kinds"
        hint={HELP["sites.messageKinds"]}
        actions={
          all.length > 0 ? (
            <Chip
              label="Unnamed only"
              tip={HELP["sites.messageKinds.unknownOnly"]}
              active={unknownOnly}
              count={unknown}
              onClick={() => setUnknownOnly((on) => !on)}
            />
          ) : null
        }
      />
      {kinds.isLoading ? (
        <SkeletonText lines={5} className="p-4" />
      ) : rows.length === 0 ? (
        <EmptyState
          title={unknownOnly ? "Every kind here has a name" : "No message kinds recorded"}
          hint={
            unknownOnly
              ? "Nothing this site transmits is still unidentified."
              : "The recorder has not posted an observation window for this site yet, or the registry has not been seeded (keyup sites seed-message-kinds)."
          }
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">
              Every control-channel message kind this site has transmitted, how often, when it was first and
              last heard, and the hex body of the ones the recorder cannot decode
            </caption>
            <thead>
              <tr className="text-xs text-ink-2">
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Kind
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 text-right font-semibold">
                  Count
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  First seen
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Last seen
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Exemplar
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Known
                </th>
                <th scope="col" className="border-b border-line px-3 py-2 font-semibold">
                  Label
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key} className="border-b border-line/60">
                  <td className="px-3 py-1.5 font-mono tabular-nums text-ink">
                    <span className="flex items-center gap-2">
                      {row.key}
                      {isNew(row) ? (
                        <span className="rounded-full border border-accent bg-accent-soft px-1.5 py-0.5 font-sans text-[11px] font-semibold text-accent-ink">
                          new
                        </span>
                      ) : null}
                    </span>
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-ink-2">
                    {formatNumber(row.count)}
                  </td>
                  <td className="px-3 py-1.5 text-ink-2">{formatRelative(row.first_seen_at)}</td>
                  <td className="px-3 py-1.5 text-ink-2">{formatRelative(row.last_seen_at)}</td>
                  <td className="px-3 py-1.5 font-mono text-[11px] text-ink-2">
                    {row.exemplar ?? "—"}
                    {row.exemplar_previous ? (
                      <span className="block text-ink-2 line-through opacity-70">
                        {row.exemplar_previous}
                      </span>
                    ) : null}
                  </td>
                  <td className="px-3 py-1.5">
                    <InlineSwitch
                      value={row.known}
                      label={`${row.key} is a known message kind`}
                      tip={HELP["sites.messageKinds.known"]}
                      disabled={!isAdmin || save.isPending}
                      onSave={(known) =>
                        save.mutateAsync({ mfid: row.mfid, opcode: row.opcode, patch: { known } })
                      }
                    />
                  </td>
                  <td className="px-3 py-1.5">
                    <InlineText
                      value={row.label}
                      label={`Label for ${row.key}`}
                      tip={HELP["sites.messageKinds.label"]}
                      hint={isAdmin ? "Name this message kind." : undefined}
                      emptyAsNull
                      disabled={!isAdmin}
                      onSave={(label) =>
                        save.mutateAsync({ mfid: row.mfid, opcode: row.opcode, patch: { label } })
                      }
                    />
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
