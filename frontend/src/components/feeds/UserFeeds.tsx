/**
 * The feeds somebody published, and the controls that manage them (docs/FEEDS.md).
 *
 * Extracted from `FeedsPanel` so the Feeds page and the Search landing card render the
 * same rows rather than two tables that drift. Every row holds a **credential** — the URL
 * carries the feed's token — so the row shows the *name* and copies the URL: it never
 * prints one and never makes one a link, because a link is the one thing a reader will
 * middle-click into a chat window. Rotate and Delete are the two irreversible controls,
 * and both say so in their tooltip rather than behind a confirm dialog nobody reads.
 */
import { Copy, RefreshCw, Trash2 } from "lucide-react";
import { useDeleteFeed, useRotateFeed, useUpdateFeed } from "../../api/hooks";
import type { SyndicationFeed } from "../../api/types";
import { HELP } from "../../help/help";
import { copyAndToast } from "../../lib/clipboard";
import { formatRelative } from "../../lib/format";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Switch } from "../ui/Switch";
import { Tip } from "../ui/Tooltip";

/** How a feed publishes, in the words the Feeds page and docs/FEEDS.md both use. */
export function publishingRule(maxItems: number, includeAudio: boolean): string {
  return [
    `Newest first, up to ${maxItems} items.`,
    "A call appears once its transcript is final and its audio is processed.",
    includeAudio ? "Audio included." : "No audio.",
  ].join(" ");
}

export function UserFeedRow({
  feed,
  canWrite,
  describe = false,
}: {
  feed: SyndicationFeed;
  canWrite: boolean;
  /** Print the filter summary in full and say how the feed publishes — the Feeds page. */
  describe?: boolean;
}) {
  const update = useUpdateFeed();
  const rotate = useRotateFeed();
  const remove = useDeleteFeed();

  const copy = (label: "RSS" | "Atom", url: string) =>
    copyAndToast(url, { label: `${label} URL`, body: "Treat it as a password." });

  return (
    <li className="flex flex-wrap items-center gap-2 px-4 py-2">
      <div className="min-w-0 flex-1 basis-56">
        <p className="truncate text-sm text-ink">{feed.name}</p>
        <p className={describe ? "text-[11px] text-muted" : "truncate text-[11px] text-muted"}>
          {feed.summary}
        </p>
        {describe ? (
          <p className="mt-0.5 text-[11px] text-muted">
            {publishingRule(feed.max_items, feed.include_audio)}
          </p>
        ) : null}
      </div>

      <Tip label={HELP["search.feed.items"]}>
        <span tabIndex={0} className="text-[11px] tabular-nums text-ink-2">
          {feed.max_items} items
        </span>
      </Tip>
      <Tip label={HELP["search.feed.fetched"]}>
        <span tabIndex={0} className="text-[11px] tabular-nums text-muted">
          {feed.last_fetched_at ? `Fetched ${formatRelative(feed.last_fetched_at)}` : "Never fetched"}
          {feed.fetch_count ? `, ${feed.fetch_count} ${feed.fetch_count === 1 ? "time" : "times"}` : ""}
        </span>
      </Tip>

      <Switch
        checked={feed.include_audio}
        disabled={!canWrite}
        onCheckedChange={(include_audio) => update.mutate({ id: feed.id, patch: { include_audio } })}
        label="Audio"
        tip={HELP["search.feed.audio"]}
      />
      <Switch
        checked={feed.enabled}
        disabled={!canWrite}
        onCheckedChange={(enabled) => update.mutate({ id: feed.id, patch: { enabled } })}
        label="On"
        tip={HELP["search.feed.enabled"]}
      />

      <Button
        tip={HELP["search.feed.url"]}
        size="sm"
        aria-label={`Copy the RSS URL for ${feed.name}`}
        onClick={() => copy("RSS", feed.rss_url)}
      >
        <Copy size={13} aria-hidden="true" /> RSS
      </Button>
      <Button
        tip={HELP["search.feed.url"]}
        size="sm"
        aria-label={`Copy the Atom URL for ${feed.name}`}
        onClick={() => copy("Atom", feed.atom_url)}
      >
        <Copy size={13} aria-hidden="true" /> Atom
      </Button>
      <Button
        tip={canWrite ? HELP["search.feed.rotate"] : HELP["app.readOnly"]}
        size="iconSm"
        variant="ghost"
        disabled={!canWrite}
        aria-label={`Rotate the token for ${feed.name}`}
        onClick={() =>
          rotate.mutate(feed.id, {
            onSuccess: () =>
              toast("New feed URL", { tone: "success", body: "The old URL has stopped working." }),
            onError: () => toast("Could not rotate the token", { tone: "error" }),
          })
        }
      >
        <RefreshCw size={14} aria-hidden="true" />
      </Button>
      <Button
        tip={canWrite ? HELP["search.feed.delete"] : HELP["app.readOnly"]}
        size="iconSm"
        variant="ghost"
        disabled={!canWrite}
        aria-label={`Delete the feed ${feed.name}`}
        onClick={() => remove.mutate(feed.id)}
      >
        <Trash2 size={14} aria-hidden="true" />
      </Button>
    </li>
  );
}

/** The whole table. The caller has already decided there is something to show. */
export function UserFeedList({
  feeds,
  canWrite,
  describe = false,
}: {
  feeds: SyndicationFeed[];
  canWrite: boolean;
  describe?: boolean;
}) {
  return (
    <ul className="divide-y divide-line">
      {feeds.map((feed) => (
        <UserFeedRow key={feed.id} feed={feed} canWrite={canWrite} describe={describe} />
      ))}
    </ul>
  );
}
