/**
 * Publishing the current filter set as an RSS/Atom feed (docs/FEEDS.md).
 *
 * The dialog has one job the rest of the app does not: it hands back a **credential**.
 * A feed URL contains the feed's token, and anybody holding it can read every call the
 * filters match — so the URLs appear only after the feed exists, behind copy buttons,
 * under a warning that says so in as many words. They are never rendered as a link,
 * because a link is the one thing a reader will middle-click into a chat window.
 */
import { Copy, Rss } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { useCreateFeed } from "../../api/hooks";
import type { CallFilterParams, SyndicationFeed } from "../../api/types";
import { HELP } from "../../help/help";
import { copyAndToast } from "../../lib/clipboard";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, FieldGrid, TextInput } from "../ui/Field";
import { Switch } from "../ui/Switch";

/** The default name, so nobody has to invent one: the first few filters, in words. */
export function suggestedName(filters: CallFilterParams): string {
  if (filters.q) return `Search: ${filters.q}`;
  if (filters.talkgroup?.length) return `Talkgroups ${filters.talkgroup.slice(0, 3).join(", ")}`;
  if (filters.agency?.length) return `Agency ${filters.agency.slice(0, 3).join(", ")}`;
  if (filters.tag?.length) return `Tagged ${filters.tag.slice(0, 3).join(", ")}`;
  if (filters.alert) return "Pages";
  if (filters.emergency) return "Emergency traffic";
  return "All calls";
}

function CopyRow({ label, url }: { label: string; url: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-12 shrink-0 text-xs font-medium text-muted">{label}</span>
      <code
        data-testid={`feed-url-${label.toLowerCase()}`}
        className="min-w-0 flex-1 truncate rounded-md border border-line bg-surface-2 px-2 py-1 text-[11px] text-ink-2"
      >
        {url}
      </code>
      <Button
        tip={HELP["search.feed.url"]}
        size="iconSm"
        aria-label={`Copy the ${label} URL`}
        onClick={() => copyAndToast(url, { label: `${label} URL`, body: "Treat it as a password." })}
      >
        <Copy size={14} aria-hidden="true" />
      </Button>
    </div>
  );
}

/**
 * The "Feed" button and its dialog. `filters` is the *current* view, so whatever the rail
 * is showing is what the feed will publish.
 */
export function FeedDialog({ filters, canWrite }: { filters: CallFilterParams; canWrite: boolean }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [includeAudio, setIncludeAudio] = useState(false);
  const [maxItems, setMaxItems] = useState(50);
  const [created, setCreated] = useState<SyndicationFeed | null>(null);
  const createFeed = useCreateFeed();

  const start = () => {
    setCreated(null);
    setName(suggestedName(filters));
    setIncludeAudio(false);
    setMaxItems(50);
    setOpen(true);
  };

  const submit = () => {
    createFeed.mutate(
      {
        name: name.trim() || suggestedName(filters),
        query: filters as unknown as Record<string, unknown>,
        include_audio: includeAudio,
        max_items: maxItems,
      },
      {
        onSuccess: (feed) => setCreated(feed),
        onError: () => toast("Could not create the feed", { tone: "error" }),
      },
    );
  };

  return (
    <>
      <Button
        tip={canWrite ? HELP["calls.feed"] : HELP["app.readOnly"]}
        size="sm"
        disabled={!canWrite}
        onClick={start}
      >
        <Rss size={14} aria-hidden="true" /> Feed
      </Button>

      <Dialog
        open={open}
        onOpenChange={setOpen}
        title={created ? "Your feed is ready" : "Publish this search as a feed"}
        description={
          created
            ? "Paste one of these into a reader. Anybody who has the URL can read the feed."
            : "New calls matching the filters you are looking at now will turn up in any RSS or Atom reader, with the transcript and a link back."
        }
        footer={
          created ? (
            <>
              {/* Where the feed now lives: renaming, rotating and turning it off all
                  happen there, and it is the page to come back to for the URL. */}
              <Link
                to="/feeds"
                onClick={() => setOpen(false)}
                className="inline-flex h-8 items-center rounded-md border border-line bg-surface px-2.5 text-sm font-medium text-ink hover:bg-surface-2"
              >
                Open Feeds page
              </Link>
              <Button tip="Close" size="sm" variant="primary" onClick={() => setOpen(false)}>
                Done
              </Button>
            </>
          ) : (
            <>
              <Button tip="Close without publishing" size="sm" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button
                tip={HELP["search.feed"]}
                size="sm"
                variant="primary"
                disabled={createFeed.isPending}
                onClick={submit}
              >
                Create feed
              </Button>
            </>
          )
        }
      >
        {created ? (
          <div className="space-y-3">
            <p className="rounded-md border border-warning bg-warning-soft px-3 py-2 text-xs text-ink">
              <strong>The URL is the password.</strong> It carries this feed's token, so anybody who has it
              can read every call the feed matches — even when the archive itself is closed. Keep it out of
              chats and screenshots, and rotate it on the Feeds page if it gets out.
            </p>
            <CopyRow label="RSS" url={created.rss_url} />
            <CopyRow label="Atom" url={created.atom_url} />
            <p className="text-[11px] text-muted">{created.summary}</p>
          </div>
        ) : (
          <div className="space-y-3">
            <Field label="Name" help="search.feed">
              {(id) => (
                <TextInput
                  id={id}
                  value={name}
                  placeholder="Working fires"
                  onChange={(event) => setName(event.target.value)}
                />
              )}
            </Field>
            <FieldGrid>
              <Field label="Items per fetch" help="search.feed.items">
                {(id) => (
                  <TextInput
                    id={id}
                    type="number"
                    min={1}
                    max={200}
                    value={maxItems}
                    onChange={(event) => setMaxItems(Number(event.target.value) || 50)}
                  />
                )}
              </Field>
              <div className="flex items-end">
                <Switch
                  checked={includeAudio}
                  onCheckedChange={setIncludeAudio}
                  label="Include audio"
                  tip={HELP["search.feed.audio"]}
                />
              </div>
            </FieldGrid>
          </div>
        )}
      </Dialog>
    </>
  );
}
