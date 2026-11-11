/**
 * Every feed this archive publishes, and what each one contains (docs/FEEDS.md).
 *
 * The Search page used to carry the whole list in a card, which on a deployment with
 * sixteen talkgroup categories was sixteen names and nothing that said what was in them.
 * A feed is a subscription somebody hands to software and then forgets about, so the thing
 * worth writing down is not the URL but the *contents*: which channels, how busy, in what
 * order, when an item appears, and whether the audio comes with it.
 *
 * The wording of the explainer is taken from docs/FEEDS.md deliberately, so the page and
 * the documentation say the same thing rather than two nearly-identical things.
 */
import { Copy, ExternalLink, Rss } from "lucide-react";
import { Link } from "react-router";
import { useCurrentUser, useFeedPresets, useFeeds } from "../api/hooks";
import type { FeedPreset } from "../api/types";
import { EmbedCard } from "../components/feeds/EmbedCard";
import { UserFeedList } from "../components/feeds/UserFeeds";
import { PageHeader } from "../components/layout/PageHeader";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { SkeletonRows } from "../components/ui/Skeleton";
import { EmptyState, ErrorState } from "../components/ui/States";
import { Tip } from "../components/ui/Tooltip";
import { HELP } from "../help/help";
import { copyAndToast } from "../lib/clipboard";
import { formatNumber } from "../lib/format";

/**
 * What a built-in feed publishes per fetch. Presets are not configurable — the server
 * builds them with `max_items=50` and enclosures on — so this is a statement about the
 * server's own defaults rather than a setting this page could get wrong.
 */
const PRESET_MAX_ITEMS = 50;

/** The one preset that is not a talkgroup category, and says so in plain words. */
const ALL_SLUG = "all";

/**
 * `EC EVENT 6 (62)` — a busy channel and its week, or the tgid where the directory has no
 * alias for it. The type comes from the preset itself: nested objects arrive from the
 * generated schema with optional fields, and pretending otherwise here would only move the
 * `undefined` somewhere it is not handled.
 */
function channelLabel(tg: FeedPreset["top_talkgroups"][number]): string {
  return `${tg.alias ?? tg.tgid} (${formatNumber(tg.calls_7d ?? 0)})`;
}

/**
 * One built-in feed, described.
 *
 * The copy buttons are enabled for everybody: a built-in feed's URL holds no secret and
 * subscribing to one needs no account. **Preview** opens the RSS document itself, which is
 * the only honest way to answer "what will my reader actually see".
 */
export function PresetCard({ preset }: { preset: FeedPreset }) {
  const copy = (label: "RSS" | "Atom", url: string) =>
    copyAndToast(url, { label: `${label} URL`, body: "Safe to share — it holds no password." });

  const contents =
    preset.slug === ALL_SLUG
      ? `Every call in the archive — all ${formatNumber(preset.talkgroups)} talkgroups`
      : `${formatNumber(preset.talkgroups)} ${preset.talkgroups === 1 ? "talkgroup" : "talkgroups"}`;

  return (
    <Card className="flex h-full flex-col p-4">
      <h3 className="text-sm font-semibold text-ink">{preset.name}</h3>
      <p className="mt-1 text-xs text-ink-2">{preset.description}</p>

      <Tip label={HELP["feeds.builtin.contents"]}>
        <p tabIndex={0} className="mt-2 flex flex-wrap gap-x-3 text-[11px] text-muted">
          <span>{contents}</span>
          <span className="tabular-nums">{formatNumber(preset.calls_7d)} calls in the last 7 days</span>
        </p>
      </Tip>
      {preset.top_talkgroups.length > 0 ? (
        <Tip label={HELP["feeds.builtin.busiest"]}>
          <p tabIndex={0} className="mt-0.5 text-[11px] text-muted">
            Busiest: {preset.top_talkgroups.map(channelLabel).join(", ")}
          </p>
        </Tip>
      ) : null}
      <Tip label={HELP["feeds.publishing"]}>
        <p tabIndex={0} className="mt-2 text-[11px] text-muted">
          Newest first, up to {PRESET_MAX_ITEMS} items. A call appears once its transcript is final and its
          audio is processed. Audio is included while the archive is open to guests.
        </p>
      </Tip>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          tip={HELP["feeds.builtin.url"]}
          size="sm"
          aria-label={`Copy the RSS URL for ${preset.name}`}
          onClick={() => copy("RSS", preset.rss_url)}
        >
          <Copy size={13} aria-hidden="true" /> RSS
        </Button>
        <Button
          tip={HELP["feeds.builtin.url"]}
          size="sm"
          aria-label={`Copy the Atom URL for ${preset.name}`}
          onClick={() => copy("Atom", preset.atom_url)}
        >
          <Copy size={13} aria-hidden="true" /> Atom
        </Button>
        <Tip label={HELP["feeds.preview"]}>
          <a
            href={preset.rss_url}
            target="_blank"
            rel="noreferrer"
            aria-label={`Preview the ${preset.name} feed`}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-transparent px-2.5 text-sm font-medium text-ink-2 hover:bg-surface-2 hover:text-ink"
          >
            <ExternalLink size={13} aria-hidden="true" /> Preview
          </a>
        </Tip>
      </div>
    </Card>
  );
}

/** What RSS and Atom are for here, and the rules that apply to every feed on the page. */
function HowFeedsWork() {
  return (
    <Card>
      <CardHeader title="How feeds work" hint={HELP["feeds.how"]} />
      <div className="max-w-[70ch] space-y-2 px-4 py-3 text-xs leading-relaxed text-ink-2">
        <p>
          A feed is one saved search, published as RSS 2.0 and Atom 1.0. Point a reader, a podcast app, a
          Slack or Discord RSS integration or a cron job at the URL and it sees every new call that matches —
          the transcript as the item body, and a link back to the call's page here.
        </p>
        <p>
          <strong className="text-ink">An item appears only when the record is finished.</strong> A call is
          written the moment its audio lands and is transcribed afterwards, so a feed holds it back until the
          transcript has reached a terminal state and no audio pass is still queued. What that costs is a
          minute or two of delay, not completeness: the item still carries the call's own start time, so a
          reader that sorts by date files it where it belongs.
        </p>
        <p>
          <strong className="text-ink">Built-in feeds hold no secret.</strong> Their URLs are stable and safe
          to hand out; what they publish is governed by the archive's own read policy, exactly as the Calls
          page is. Close the archive to guests and they need a sign-in like every other read, and their
          documents drop the audio enclosures rather than publishing links that would be refused.
        </p>
        <p>
          <strong className="text-ink">A feed you publish is different: the URL is the password.</strong> It
          carries the feed's token, so anybody holding it can read every call the filters match — even when
          the archive itself is closed. Keep it out of chats and screenshots, and rotate it if it gets out.
          Rotating mints a new URL and the old one stops working at once, which also means every subscriber
          has to be handed the new one.
        </p>
        <p>
          <strong className="text-ink">Audio.</strong> With enclosures on, each item carries the recording
          with its real byte length, which is what makes a feed playable in a podcast app — most podcast apps
          refuse a feed that has none. On a feed you published the enclosure is checked against that feed's
          own filters on every request, so a feed over one talkgroup can never become a key to the archive.
        </p>
        <p>
          Documents are cached for 60 seconds and ask readers for a 5-minute poll interval. Every feed carries
          the newest 50 items by default, 200 at most; a reader keeps its own history, so a bigger number buys
          almost nothing.{" "}
          <Link to="/help" className="text-accent-ink hover:underline">
            The help page
          </Link>{" "}
          explains each control.
        </p>
      </div>
    </Card>
  );
}

export function Feeds() {
  const presets = useFeedPresets();
  const feeds = useFeeds();
  const { canWrite, user } = useCurrentUser();
  const built = presets.data ?? [];
  const mine = feeds.data ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto scrollbar-thin">
      <PageHeader
        title="Feeds"
        description="Subscribe to the archive in any RSS or Atom reader — a built-in feed for every talkgroup category, or a search of your own published as one."
      />

      <div className="flex flex-col gap-4 p-4 md:p-6">
        <Card>
          <CardHeader title="Built-in feeds" hint={HELP["feeds.builtin"]} />
          <div className="p-4">
            {presets.error ? (
              <ErrorState error={presets.error} onRetry={() => void presets.refetch()} />
            ) : presets.isLoading ? (
              <SkeletonRows rows={4} label="Loading feeds" />
            ) : built.length === 0 ? (
              <EmptyState
                title="No built-in feeds yet"
                hint="They are derived from the talkgroup directory, so one appears as soon as a talkgroup is filed under a category."
                icon={<Rss size={28} />}
              />
            ) : (
              <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                {built.map((preset) => (
                  <li key={preset.slug} className="min-w-0">
                    <PresetCard preset={preset} />
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>

        {presets.isLoading || presets.error ? null : <EmbedCard presets={built} />}

        <Card>
          <CardHeader title="Your feeds" hint={HELP["feeds.mine"]} />
          {user === null ? (
            <p className="max-w-[70ch] px-4 py-3 text-xs text-ink-2">
              Saving a search as a feed needs an account — its URL is a credential, so it belongs to somebody.
              The built-in feeds above need none.{" "}
              <Link to="/login" className="text-accent-ink hover:underline">
                Sign in
              </Link>{" "}
              to publish one from the{" "}
              <Link to="/search" className="text-accent-ink hover:underline">
                Search
              </Link>{" "}
              page.
            </p>
          ) : mine.length === 0 ? (
            <p className="max-w-[70ch] px-4 py-3 text-xs text-ink-2">
              You have not published one yet. Filter the{" "}
              <Link to="/calls" className="text-accent-ink hover:underline">
                Calls
              </Link>{" "}
              or{" "}
              <Link to="/search" className="text-accent-ink hover:underline">
                Search
              </Link>{" "}
              page down to what you want and press <strong className="text-ink">Feed</strong>.
            </p>
          ) : (
            <UserFeedList feeds={mine} canWrite={canWrite} describe={true} />
          )}
        </Card>

        <HowFeedsWork />
      </div>
    </div>
  );
}
