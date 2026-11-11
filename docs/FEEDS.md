# Syndication feeds (RSS and Atom)

A **feed** is one saved `GET /calls` filter, published as RSS 2.0 and Atom 1.0. Point a
reader, a podcast app, a Slack/Discord RSS integration or a cron job at the URL and it sees
every new call that matches — with the transcript as the item body and a link back to the
call's page in Keyup.

It is the same filter language as everything else in the archive (docs/ARCHITECTURE.md
§4.1), so anything you can narrow the Calls page down to, you can subscribe to: one
talkgroup, one agency's channels, "pages only", "matching *structure fire*", weekday
afternoons, or the lot. And for the obvious slices there is nothing to build at all: the
**built-in feeds** below are already there, one per talkgroup category, at URLs that hold
no secret.

## Built-in feeds

Some feeds nobody should have to build. These exist on every deployment:

```
{PUBLIC_BASE_URL}/api/feeds/preset/all.rss      every call in the archive
{PUBLIC_BASE_URL}/api/feeds/preset/fire.rss     everything filed under `fire`
{PUBLIC_BASE_URL}/api/feeds/preset/law.atom     ...and so on, `.rss` or `.atom`
```

The slug is `all`, or a **talkgroup category** — the `category` column in the directory,
the same buckets the Calls filter's category chips use (docs/TALKGROUPS.md). The list is
derived from the directory at request time, so it holds exactly the categories this
deployment actually files a talkgroup under: a category nothing is in is a `404` rather
than a feed that would publish nothing for ever. `GET /api/feeds/presets` names them all,
with a talkgroup count, the week's call volume, the busiest channels in it and both URLs —
which is what the **Feeds** page in the app prints, so somebody can tell what is in a feed
before subscribing to it. Those figures cost one grouped query per preset and are cached
for ten minutes.

They differ from a saved feed in one way that matters:

* **There is no token, and the URL is not a credential.** A built-in feed publishes a
  slice of the archive the archive's own read policy already governs. While
  `AUTH_REQUIRE_READ` is off — the default — anybody can fetch one, exactly as anybody can
  open the Calls page. Turn it on and these need a sign-in like every other read: they are
  deliberately **not** in the self-authenticating exemption that the token URLs use, and a
  fetch without an account is a `401`. Put another way: a built-in feed is not a way past
  the door, which is why it can be handed out freely while the door is open.
* **Enclosures come and go with that policy.** While reads are open each item carries a
  plain `/api/calls/{id}/audio` enclosure, so the feed works in a podcast app. When reads
  are closed the documents drop their enclosures rather than publishing audio URLs that
  would answer `401` — the items, the links and the transcripts are all still there.
* Nothing to rotate, nothing to leak, nothing to delete. They cannot be renamed either:
  the slug is the subscription, and it is stable for ever.

Otherwise they are the same thing — same renderer, same item shape, same readiness rule,
same `Cache-Control: max-age=60`, 50 items. Build a saved feed instead when you want a
filter narrower than a whole category, or a feed whose URL is the credential.

## An item appears only when the record is finished

A call row is written the moment its audio lands, and *then* it is transcribed, and (where
the deployment processes audio) normalised. A feed holds the call back until both queues
have let go of it:

* `transcript_status` has reached a terminal value — `done`, `skipped`, `failed` or
  `empty`. `skipped` and `failed` count: the archive has decided there will be no text,
  and that is a finished record, not a pending one.
* no `audio_jobs` row for the call is still `queued` or `running`, so the file behind the
  enclosure is the final one.

This is not an option and there is no flag for it. An item is **permanent**: the `guid` is
the call's own permalink, so a reader that fetched during the gap would have kept an entry
reading *Transcript pending*, with an enclosure pointing at a file about to be rewritten,
and nothing would ever have replaced it. The words landed a minute later and no subscriber
saw them.

**The wait is capped.** A call ingested more than `FEED_READY_MAX_WAIT_S` ago (900 s,
fifteen minutes, by default; `0` turns the cap off) is published whatever its status. A
stuck speech or audio worker used to hold back every item of every feed until somebody
noticed; an item that says *Transcript pending* is a worse record than a finished one but a
far better one than none. The cap is measured from ingest, not from the call's start, so a
backlog import of old recordings still waits for its transcripts.

What it costs is latency, not completeness — a call shows up in a reader a minute or two
after it was recorded. It costs nothing at all to *ordering*, because `pubDate` (Atom:
`published`/`updated`) is the call's own start time and not the moment it became ready, so
a reader that sorts by date files it where it belongs. A reader that instead shows things
in the order it first saw them will interleave a slow transcription slightly late.

## The URL is the secret

A feed reader has no account, no cookie and no way to sign in, so the credential has to
travel in the URL:

```
http://keyup.example/api/feeds/yML3PvhCY5FG44x49tg5ltnGOTr3Wxc88qy7YeGy8I4.rss
http://keyup.example/api/feeds/yML3PvhCY5FG44x49tg5ltnGOTr3Wxc88qy7YeGy8I4.atom
```

Treat that URL the way you would treat a password.

* Anybody holding it can read every call the filter matches, for as long as it works — even
  when `AUTH_REQUIRE_READ` has closed the rest of the archive.
* An unknown token, a **disabled** feed and a deleted one all answer a flat `404`. Nothing
  distinguishes them, on purpose: confirming that a URL *used to* be real is the one fact
  turning a feed off was meant to take back.
* **Rotate** mints a new token and the old URL stops working at once — which also means
  every subscriber has to be handed the new one.
* Nothing in the logs contains a token.
* The response carries `X-Robots-Tag: noindex`, so a feed that ends up behind a public
  proxy is at least not indexed.

If a feed URL gets out, rotate it. If you are not sure whether it got out, rotate it.

## Building one from a search

Filter the Calls page until it is showing what you want — talkgroups, agencies, text query,
duration, tones, whatever — then **Feed** in the toolbar. Give it a name, decide whether to
attach audio, and the dialog hands back the two URLs with copy buttons.

What gets stored is the filter *as the server parsed it*, not the address bar verbatim:
paging, the column list and anything else that lives in the URL are dropped, and the feed's
summary line ("talkgroups 1101 · matching "structure fire" · with audio") is generated
from what will actually apply. Existing feeds are listed on the **Feeds** page (and, in
short, on the Search landing page), where each row can be renamed, enabled and disabled,
rotated or deleted.

`max_items` is how many entries a document carries — 50 by default, 200 at most. A reader
polls every few minutes and keeps its own history, so a bigger number buys almost nothing
and costs a database scan per poll.

## What an item contains

| | |
|---|---|
| title | `EC EVENT 6 · 14:17:02 · 0.2s` — channel, local time, length. A unit call reads `E1 → E2 · …` |
| link / guid / id | `{PUBLIC_BASE_URL}/calls/{id}` — the call's page, a permalink |
| pubDate / updated | the call's start time |
| description / content | the transcript, then one line of metadata (radio, agency, site, quality, emergency, encrypted) |
| category | the talkgroup alias and the agency code |
| dc:creator / author | the radio that keyed up |

A human correction is preferred over the engine's text. Where there is no transcript the
body says so — *No transcript*, which by the time an item is published means the call was
skipped, failed or came back empty rather than that it is still waiting — and a transcript
the archive has flagged as an STT hallucination is **not** printed: it says *Rejected as hallucination*
instead. Republishing a known hallucination into somebody's reader would launder it into a
quotable line.

Channel metadata: the title is the feed's name, the description is the filter summary, and
there is a `lastBuildDate`, a `ttl` of 5 minutes and an `atom:link rel="self"`. Responses
are `Cache-Control: max-age=60`.

## Audio enclosures

Turn on **Include audio** and each item gets an `<enclosure>` (RSS) or
`<link rel="enclosure">` (Atom) pointing at the recording, with its real byte length — which
makes the feed playable in a podcast app.

The enclosure URL carries the feed's token:

```
{PUBLIC_BASE_URL}/api/calls/{id}/audio?feed={token}
```

`?feed=` is **not** a read credential for the archive. It is checked per request against
exactly what the feed document would publish — the feed's own filters and the readiness
gate above — so:

* a call the feed does not match, or has not published yet, is a `404`, identical to an
  unknown token;
* a feed with **Include audio** off opens no recording at all (same `404`);
* disabling the feed closes its enclosures in the same breath as its documents;
* rotating the token invalidates every enclosure URL already published.

A feed over one talkgroup can therefore never become a key to the whole archive. Calls
whose audio has been purged or archived carry no enclosure at all.

## Reader tips

* **Podcast apps** want the RSS URL and `include_audio` on. Most refuse to add a feed with
  no enclosures.
* **Only the loud stuff**: filter to `alert` (pages) or `emergency` before making the feed —
  a feed over a busy dispatch channel is a firehose, and a reader will not thank you.
* **No transcript yet?** There is no such item: a call is not published until the archive
  has finished with it (above), so nothing in a feed says *Transcript pending* any more.
  What you get instead is a delay of a minute or two behind the radio. If you want only
  calls that produced words — rather than every finished call, including the ones that
  produced none — filter the feed to `transcript_status=done`.
* **Slack/Discord** RSS integrations poll on their own schedule and will happily flood a
  channel; keep `max_items` small and the filter narrow.
* The `/api/feeds` management routes need an account. Only the two document URLs and the
  enclosure are public.

## API

| | |
|---|---|
| `GET /api/feeds/{token}.rss` | the document, RSS 2.0 — public, token-authenticated |
| `GET /api/feeds/{token}.atom` | the document, Atom 1.0 — public, token-authenticated |
| `GET /api/feeds/presets` | the built-in feeds: `{slug, name, description, query, talkgroups, calls_7d, top_talkgroups[{tgid, alias, calls_7d}], rss_url, atom_url}` — the last three describe a week of traffic and are cached for ten minutes |
| `GET /api/feeds/preset/{slug}.rss` | a built-in feed, RSS 2.0 — no token, follows the archive's read policy |
| `GET /api/feeds/preset/{slug}.atom` | the same, Atom 1.0 |
| `GET /api/feeds` | the feeds you own (an admin sees all, with the owner). A feed made with an API token is owned by that token, not shared by every token; one whose token was deleted is an admin's alone |
| `POST /api/feeds` | `{name, query, enabled, include_audio, max_items}` → the feed plus `rss_url`/`atom_url` |
| `PATCH /api/feeds/{id}` | the same fields; never changes the token |
| `POST /api/feeds/{id}/rotate` | new token, new URLs |
| `DELETE /api/feeds/{id}` | gone, and its URLs with it |

`query` is a `GET /calls` filter object — the same shape a saved search stores. It is parsed
on the way in and the parsed form is echoed back as `query`, so the UI can show what will
actually apply.
