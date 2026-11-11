/**
 * The Search landing page's feeds card (docs/FEEDS.md).
 *
 * It used to list every built-in feed, which on a deployment with sixteen talkgroup
 * categories was sixteen rows of a landing page nobody came to for feeds. The built-in
 * half is now one line pointing at `/feeds`, where each one is described properly; what
 * stays here is the half that belongs to the person reading it — the feeds they published,
 * with the same rows the Feeds page shows.
 */
import { Rss } from "lucide-react";
import { Link } from "react-router";
import { useFeeds } from "../../api/hooks";
import { HELP } from "../../help/help";
import { UserFeedList } from "../feeds/UserFeeds";
import { Card, CardHeader } from "../ui/Card";
import { Tip } from "../ui/Tooltip";

export function FeedsPanel({ canWrite }: { canWrite: boolean }) {
  const feeds = useFeeds();
  const rows = feeds.data ?? [];
  return (
    <Card>
      <CardHeader title="Feeds" hint={HELP["search.feeds"]} />
      <Tip label={HELP["feeds.page"]}>
        <p className="flex items-center gap-2 px-4 py-2 text-xs text-ink-2">
          <Rss size={13} aria-hidden="true" className="shrink-0 text-muted" />
          <span>
            Built-in feeds for every category —{" "}
            <Link to="/feeds" className="text-accent-ink hover:underline">
              see the Feeds page
            </Link>
          </span>
        </p>
      </Tip>
      {rows.length > 0 ? (
        <>
          <p className="border-t border-line bg-surface-2 px-4 py-1 text-xs font-medium text-muted">
            Your feeds
          </p>
          <UserFeedList feeds={rows} canWrite={canWrite} />
        </>
      ) : null}
    </Card>
  );
}
