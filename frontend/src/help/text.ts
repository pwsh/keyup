/**
 * Every tooltip in Keyup, in one typed registry — the text itself.
 *
 * This module is ~94 KB of source that only matters once someone hovers, focuses or
 * reads a tooltip, so nothing in the app shell imports it statically: `help.ts` hands out
 * placeholders (`HELP["x"]`) and fetches this chunk on idle or on the first tooltip, and
 * `Tip` swaps the text in when it shows. Only the `/help` page imports it directly.
 *
 * Rules for writing one: a single plain-English sentence (occasionally two) that
 * says **what the control does** and, where it is not obvious, **when you would
 * use it**. No jargon the page has not already introduced, no "click here", and
 * no restating the visible label. Ids are `area.group.control`; the leading
 * segment is the page the control lives on, and `/help` groups by it.
 *
 * The registry is the contract the `Tip`, `Button`, `Chip` and `InfoDot`
 * components consume. `help.test.ts` checks that every id referenced anywhere in
 * `src/` exists here, that nothing here is unused, and that every entry is a real
 * sentence rather than a placeholder.
 */

export const HELP_TEXT = {
  /* ------------------------------------------------------------ app shell */
  "app.nav.toggle":
    "Collapse the menu to a rail of icons to give the page more width, or expand it to show every page name. Shortcut: [",
  "app.nav.calls": "Browse and filter every recorded call, with a player that follows your selection",
  "app.nav.live": "Watch calls arrive as they happen, with optional automatic playback",
  "app.nav.search": "Full-text search across transcripts, notes and aliases, with highlighted matches",
  "app.nav.talkgroups": "Every talkgroup the recorder has heard — volume, category and playback policy",
  "app.nav.radios": "Every radio and unit heard on the system, and which talkgroups each one uses",
  "app.nav.units": "The units a callsign names — engines, ambulances, stations — and the radios on each",
  "app.nav.personnel": "The people who carry the radios, and which radio each one is holding",
  "app.nav.sites":
    "Every tower site the recorder has heard, and how what it announces compares with RadioReference",
  "app.nav.stats": "Volume, activity patterns, transcription coverage and recorder uptime",
  "app.nav.feeds":
    "The RSS and Atom feeds this archive publishes, what each one contains, and the feeds you have published yourself",
  "app.nav.transcripts":
    "A text-only live view of transcriptions with time and talkgroup, linking to each call; opens in a new tab and can be embedded in another page",
  "app.nav.alerts":
    "Rules that notify you when a talkgroup, radio or phrase turns up, and what they have sent",
  "app.nav.admin":
    "Ingest health, the transcription queue, STT backends, retention, settings, tokens and users",
  "app.nav.activity":
    "The live operational log of the pipeline: calls accepted, duplicates, transcriptions and errors",
  "app.nav.feedstats":
    "Listener statistics and archive listings for the Broadcastify feed this system streams",
  "app.nav.help": "What every filter, control, chart and status badge on this site means",
  "app.search.global":
    "Search transcripts from anywhere in the app; Enter opens the full search page with your query",
  "app.search.clear": "Empty the search box and drop the query from the results",
  "app.theme.toggle": "Switch between the light and dark theme; your choice is remembered on this device",
  "app.connection.live": "Live updates are on: new calls appear the moment they are recorded",
  "app.connection.connecting": "Live updates are reconnecting; this happens on its own",
  "app.connection.closed":
    "Live updates are off. Open the Live page or turn on Follow new calls to start them",
  "app.connection.following": "The player is following it: each new call plays as it arrives.",
  "app.connection.mock": "Running on built-in sample data — live updates are simulated, not a real receiver",
  "app.ingestLag":
    "How long ago the newest call in the archive ended. A few minutes is normal; hours means the recorder or the ingest has stopped",
  "app.mockBadge":
    "The API is not reachable, so every number on screen comes from built-in sample data rather than the real archive",
  "app.apiDocs": "Interactive documentation for the JSON API behind this site (OpenAPI / Swagger)",
  "app.account.signIn": "Sign in to edit notes and tags, run jobs and change settings",
  "app.account.signOut": "End this session; browsing stays available, editing does not",
  "app.readOnly": "You are signed out, so this is read-only. Sign in to make changes",
  "app.adminOnly": "This one is for administrators only, because it destroys or moves data",
  "app.skipToContent": "Jump straight past the navigation to the page content",
  "app.list.count": "How many rows you are looking at out of every row that matches the search and filters",
  "app.list.prev": "Show the previous page of this list",
  "app.list.next": "Show the next page of this list",
  "app.inlineEdit":
    "Click the value to change it, or focus it and press Enter. It saves when you leave the box or press Enter; Escape puts it back",
  "app.bulk.select": "Tick this row to include it in the next bulk edit",
  "app.bulk.selectPage": "Tick or clear every row on the page you are looking at",
  "app.bulk.selected": "How many rows the next bulk edit would change, across every page you have ticked on",
  "app.bulk.edit": "Open the panel that writes one set of values onto every ticked row at once",
  "app.bulk.clear": "Untick everything and leave the rows as they are",
  "app.bulk.selectAllLoaded":
    "Tick every row on this page. Rows on other pages are not touched — you can page and keep ticking",
  "app.bulk.field":
    "Leave unchanged writes nothing for this setting; Set to writes the value you choose onto every ticked row; Clear empties it on all of them",
  "app.bulk.apply": "Write the settings you chose onto every ticked row, in one go",
  "app.bulk.cancel": "Close the panel without changing anything; your selection is kept",
  "app.bulk.limit":
    "One bulk edit covers at most 500 rows. Narrow the search or do it in two passes if you need more",

  /* ------------------------------------------------------------- calls */
  "calls.filter.panel":
    "Narrow the archive down; every filter is kept in the address bar so the view is shareable",
  "calls.filter.q":
    "Full-text search across transcripts, notes and aliases. Quote a phrase to require it exactly, and prefix a word with - to exclude it",
  "calls.filter.dateFrom": "Only calls on or after this date, in local time",
  "calls.filter.dateTo": "Only calls on or before this date, in local time",
  "calls.filter.preset.today": "Jump the date range to today so far",
  "calls.filter.preset.yesterday": "Jump the date range to all of yesterday",
  "calls.filter.preset.24h": "The last 24 hours, rolling — the usual view for catching up",
  "calls.filter.preset.7d": "The last seven days, for spotting a pattern rather than one incident",
  "calls.filter.preset.30d": "The last 30 days, rolling — the Search page's default",
  "calls.filter.preset.1y": "The last 365 days, rolling",
  "calls.filter.preset.all": "Everything in the archive, with no date limit",
  "calls.filter.timeOfDay":
    "Keep only calls whose start time falls in this part of the day, on every day in the range. Set the end earlier than the start to cover an overnight window",
  "calls.filter.dow": "Keep only calls on these days of the week — weekday against weekend traffic",
  "calls.filter.talkgroup":
    "Limit to these channels. Patched traffic counts too, so a patch involving a selected talkgroup still shows up.",
  "calls.filter.radio": "Limit to calls keyed up by these radios",
  "calls.filter.unit": "Limit to these apparatus or unit numbers, whichever radio was carrying them",
  "calls.filter.agency": "Limit to the talkgroups mapped to these agencies",
  "calls.filter.category": "Limit to these kinds of channel — law, fire, EMS, public works and so on",
  "calls.filter.callType": "Group traffic, patched supergroups, or one-to-one individual calls",
  "calls.filter.kind":
    "Who the traffic was addressed to: a talkgroup, or one other radio. Off shows both, which is what a timeline should",
  "calls.filter.emergency":
    "Keep only calls a radio flagged as an emergency. Recordings from a plain sdrtrunk file carry no such flag and never match",
  "calls.filter.encrypted":
    "Keep only encrypted calls. They have no audio — the row is kept because it still shows who held the channel and when",
  "player.processed":
    "Play the processed copy — trimmed, levelled, denoised, with any page tones turned down. Turn it off to hear the recorder's own file exactly as it was stored; the original is never modified either way",
  "player.processedUnavailable":
    "This call has no processed copy yet, so the recorder's own file is what plays",
  "calls.filter.alert":
    "Keep only pages — calls that open with a two-tone or alert sequence. This is what the audio pass heard, not what a radio asserted, so it is not the same set as Emergency and the two rarely overlap",
  "calls.filter.tone":
    "Keep every call with a sustained tone in it — pages, single alert beeps, warbles. Wider than Pages only",
  "calls.filter.duration":
    "Keep only calls in this length range. Raising the floor above a second hides squelch tails, which carry no speech",
  "calls.filter.transcriptStatus": "Where each call stands in the transcription pipeline",
  "calls.filter.quality":
    "How much to trust the transcript: good, suspect (odd scores or very short audio), or noise (the model hallucinated)",
  "calls.filter.priority":
    "How the call reached the archive: live means a recorder pushed it as it happened; backlog and bulk mean an archive migration pushed it long afterwards. Calls picked up off disk carry no class and match none of these",
  "calls.filter.tags": "Keep only calls carrying one of these labels you have added",
  "calls.filter.starred": "Keep only the calls you have starred",
  "calls.filter.includeDuplicates":
    "Show the second copy of calls the recorder wrote twice. Off by default, because the duplicate says nothing new",
  "calls.feed":
    "Publish these filters as an RSS/Atom feed, so new matching calls turn up in a reader with their transcripts and links back",
  "calls.filter.reset": "Clear every filter and go back to the default view",
  "calls.filter.apply": "Apply the filters and close this sheet",
  "calls.filter.open": "Open the filters",
  "calls.filter.close": "Close the filters and go back to the results",
  "calls.filter.activeCount": "How many filters are narrowing this view right now",

  "calls.table.time": "When the call started, in local time. Sort to put the newest or the oldest first",
  "calls.table.talkgroup": "The channel the call was on, with its numeric talkgroup id",
  "calls.table.radio": "The radio that keyed up, shown by unit number where one is known",
  "calls.table.duration": "How long the transmission lasted",
  "calls.table.source": "The talker alias the radio sent, when it sent one",
  "calls.table.frequency": "The channel frequency the call was carried on",
  "calls.table.emergency": "Whether the radio flagged this transmission as an emergency",
  "calls.table.tone":
    "A bell marks a call with a sustained tone in it; a filled bell marks a page — alert tones in the first seconds. A crossed-out speaker marks a call that measured as silence",
  "calls.table.encrypted": "Whether the call was encrypted — kept for its metadata, with no audio to play",
  "calls.table.ingest": "Which recorder the call came from — sdrtrunk, trunk-recorder or a direct push",
  "calls.table.backfilled":
    "This recording was pushed by an archive migration long after it was recorded, not captured live. Everything about it is real; only its arrival was late",
  "calls.table.unitCall":
    "A unit-to-unit (private) call: one radio talking to one radio, on no talkgroup at all. The two radios take the place of the channel name",
  "calls.table.transcript": "The start of the transcript; search terms are highlighted",
  "calls.table.quality": "Transcript confidence at a glance — green good, amber suspect, red noise",
  "calls.table.actions": "Play, star, share or download this call",
  "calls.table.copy": "Copy a plain-text summary of this call — time, channel, radio, length and transcript",
  "calls.table.select": "Tick this call to include it in an export of just the ones you choose",
  "calls.table.selectAll": "Tick or clear every call currently loaded",
  "calls.table.row": "Select this call: it loads into the player and its transcript opens below",
  "calls.table.open": "Open the full detail page for this call",
  "calls.table.sort": "Sort by this column; click again to reverse the order",
  "calls.table.count": "How many calls match the current filters",
  "calls.table.loadMore": "Fetch the next page of results",

  "calls.live.live": "New calls are being added to this list as they are recorded",
  "calls.live.reconnecting": "The live connection dropped and is being retried; the list is not current",
  "calls.live.paused": "New calls are not being added to this list until you resume",
  "calls.live.pause": "Stop adding new calls to this list; the choice is remembered on this device",
  "calls.live.resume": "Start adding new calls to this list again as they are recorded",
  "calls.live.new": "Calls that arrived while you were reading further down — go up to the newest",

  "calls.facets.panel": "How the matching calls break down, and a quick way to narrow further",
  "calls.facets.talkgroup": "Calls per channel in the current results — click one to filter to it",
  "calls.facets.hour": "Calls per hour of the day in the current results",
  "calls.facets.clear": "Remove this facet from the filters",

  "calls.export.menu": "Take these results away as a file",
  "calls.export.selected": "Download only the calls you have ticked",
  "calls.export.filtered": "Download every call matching the current filters, not just the loaded page",
  "calls.export.clearSelection": "Untick every call and go back to exporting the whole filtered set",
  "calls.export.csv":
    "Download every matching call as a CSV spreadsheet — metadata and transcripts, no audio",
  "calls.export.json": "Download every matching call as JSON, for scripting against",
  "calls.export.m3u": "Download a playlist an external player can stream the matching calls from",
  "calls.export.bundle":
    "Queue a background job that zips the audio and metadata for every matching call; you get a download link when it finishes",
  "calls.export.progress": "How far the audio bundle has got",
  "calls.export.download": "Download the finished bundle",

  "calls.view.table": "Show the results as a dense table",
  "calls.view.cards": "Show the results as cards, one call each — easier on a narrow screen",
  "calls.view.sort": "Choose what the results are ordered by",

  "calls.toolbar.audioOnly":
    "Hide the metadata-only calls — encrypted traffic and anything aged past retention have no recording to play. Turn it off to see the whole archive",
  "calls.toolbar.quality":
    "Keep only calls at a transcript confidence, or hide the noise the engine almost certainly hallucinated",
  "calls.toolbar.columns": "Choose which columns the table shows; the choice is remembered on this device",
  "calls.toolbar.transcriptRow":
    "Give each call a second row with its full transcript, wrapped, instead of one truncated line",

  /* -------------------------------------------------------------- player */
  "player.play": "Play the selected call. Shortcut: space",
  "player.pause": "Pause playback. Shortcut: space",
  "player.prev": "Play the previous call in the list. Shortcut: k",
  "player.next": "Play the next call in the list. Shortcut: j",
  "player.seek": "Drag to move through the call; the transcript follows the playhead",
  "player.speed": "Play faster without changing pitch — 1.5x is comfortable for routine traffic",
  "player.volume": "Playback volume for this device",
  "player.mute": "Silence playback without losing your place",
  "player.continuous": "When a call ends, roll straight on to the next one in the list",
  "player.autoPlayNew": "Play new calls the moment they arrive, like a scanner left running",
  "player.audioBlocked":
    "This browser will not start audio until you ask it to. Phones grant that permission once, to one player, so tap here and everything after it — including calls arriving by live update — plays on its own",
  "player.live": "Follow new calls: calls matching your filters are added to the top as they are recorded",
  "player.normalize": "Level out loud tones and quiet talkers during playback; audio files are unchanged",
  "player.normalizeUnavailable":
    "Leveling needs the Web Audio API, which this browser does not offer; playback is unchanged",
  "player.download": "Save this call's MP3 to your device",
  "player.share": "Copy a link that opens this exact call for anyone who can reach this site",
  "player.close": "Close the player and clear the selection",
  "player.settings":
    "Continuous play, auto-play of new calls, normalizing, the processed copy and following live — plus speed and volume on smaller screens",
  "player.shortcuts": "Keyboard: space plays or pauses, j is the next call, k the previous one",
  "player.elapsed": "How far into the call playback has got, and how long the call is",
  "player.nowPlaying": "The call currently loaded in the player",

  /* --------------------------------------------------------- call detail */
  "call.header.talkgroup": "The channel this call was on; open it to see everything else heard there",
  "call.header.identity":
    "The unit this radio is assigned to and the person carrying it, from the roster rather than from the air",
  "call.header.radio": "The radio that keyed up; open it to see where else this unit has been heard",
  "call.header.time": "When the call started, in local time — the UTC instant is underneath",
  "call.header.timeSource":
    "How the start time was worked out. File-write times are accurate to about a second; pushed calls and trunk-recorder sidecars carry the true start",
  "call.header.duration": "How long the transmission lasted",
  "call.header.frequency": "The channel frequency the recorder was tuned to when it caught this call",
  "call.header.site": "Which system and site the call came through",
  "call.header.patch": "The talkgroups that were patched together for this transmission",
  "call.header.duplicate": "This is a second copy of a call already in the archive",
  "call.header.audioProfile":
    "Which filter chain produced the file you are hearing: a named audio profile, or standard processing, the chain every channel without a profile gets",
  "call.tune":
    "Open this call in the audio-profile editor, where a chain can be tried on it and compared against the original before anything is saved",
  "call.header.emergency": "The radio flagged this transmission as an emergency",
  "call.header.encrypted":
    "The traffic was encrypted, so there is no audio — the call is kept because it still shows the channel was in use",
  "call.header.audioType": "How the transmission was carried on the air: analog, digital, or digital TDMA",
  "call.duplicates":
    "The other recorders' copies of this same transmission. Keyup keeps the best one as the call you are looking at and hides the rest behind it",
  "call.duplicates.recorder": "Which recorder produced this copy",
  "call.duplicates.noAudio":
    "This copy has no recording — an encrypted call the recorder could describe but not record, or audio since purged",
  "call.duplicates.score":
    "What reconciliation scored this copy against the primary — audio first, then length, then how exact its timestamp and metadata are",
  "call.duplicates.makePrimary":
    "Make this copy the one the archive shows. The current one becomes the hidden copy",
  "call.sources":
    "Every radio that keyed up during this call, with how far into the recording it started — a trunked call is usually several units in turn",
  "call.sources.seek": "Jump playback to the moment this unit started talking",
  "call.sources.emergency": "This unit keyed up with its emergency button pressed",

  "call.waveform": "The call's audio level over time. Click anywhere to jump the playhead there",
  "call.transcript":
    "What the speech-to-text engine heard. The line under the playhead is highlighted as it plays",
  "call.transcript.segment": "Jump playback to the start of this line",
  "call.transcript.empty": "The engine returned no words for this call",
  "call.transcript.edit": "Correct the transcript by hand; the original stays on record",
  "call.transcript.save": "Save your corrected transcript",
  "call.transcript.cancel": "Discard your edits and keep the transcript as it was",
  "call.quality":
    "How much to trust this transcript, and why. Suspect and noise come from hallucination checks on the model's own scores",
  "call.tabs.transcript": "The primary transcript, synced to playback",
  "call.tabs.others": "Other transcripts for this call — re-runs, different engines or models",
  "call.tabs.metadata": "The raw file, the recorder's own metadata and the checksum the ingest read",
  "call.tabs.links": "Records in other systems that have been linked to this call",
  "call.otherTranscripts": "Every transcript ever produced for this call, newest first",
  "call.retranscribe":
    "Send this call back through the speech-to-text queue, optionally on a different backend",
  "call.notes": "Your own notes on this call; they are searchable alongside the transcript",
  "call.notes.save": "Save these notes; they become searchable alongside the transcript",
  "call.tags": "Short labels you can filter by later — press Enter to add one",
  "call.tags.remove": "Remove this label",
  "call.star": "Star this call so you can find it again from the starred filter",
  "call.copySummary": "Copy this call as plain text — time, channel, radio, length and the whole transcript",
  "call.conversation":
    "The other calls in this exchange — consecutive traffic on the same channel with only short gaps",
  "call.prev": "The previous call on this channel",
  "call.next": "The next call on this channel",
  "call.links": "Incidents in other systems matched to this call, with how confident the match is",
  "call.raw.path": "Where the recording sits under the audio root",
  "call.raw.sha256": "Checksum of the file, which is what exact-duplicate detection compares",
  "call.raw.id3": "The ID3 frames sdrtrunk wrote into the MP3, exactly as recorded",
  "call.raw.meta":
    "What the recorder itself wrote about this call, verbatim and unparsed — sdrtrunk's ID3 frames, or trunk-recorder's JSON sidecar",
  "call.actions":
    "Copy a link or a summary, open the audio file, and — when signed in — re-transcribe or delete",
  "call.copyLink": "Copy a direct link to this call",
  "call.openAudio": "Open the raw MP3 in a new tab",
  "call.delete": "Remove this call and its recording from the archive. This cannot be undone",

  /* -------------------------------------------------------------- search */
  "search.q": "Search transcripts, notes and aliases. Press Enter to run it",
  "search.syntax":
    "Quote a phrase to require it exactly, prefix a word with - to exclude it, and use OR between alternatives",
  "search.run": "Run the search over every transcript, note and alias in the archive",
  "search.results": "Matching calls, most relevant first, with the matched words highlighted",
  "search.range":
    "How far back to search. The last 30 days unless you pick otherwise; All time searches the whole archive",
  "search.loaded":
    "How many of the matching calls are loaded. More load as you scroll towards the end of the list",
  "search.cap":
    "The list stops at 1,000 calls. Narrow the date range, add a word or filter by talkgroup to reach the rest",
  "search.fuzzy":
    "Also match the ways the transcripts misspell your words — Whisper writes Milrose for Melrose. Each word of four or more letters (not numbers, not -excluded words) is widened to the closest spellings the archive actually contains",
  "search.fuzzy.notice":
    "The words searched for, as the index stores them (word stems), and the spellings from the archive each was widened to. The highlighted excerpts show the words as they were transcribed",
  "search.fuzzy.exact": "Search for exactly the words you typed again",
  "search.save": "Save this search — filters and all — so you can run it again from one click",
  "search.saved": "Searches you have saved; opening one restores its filters exactly",
  "search.saved.delete": "Delete this saved search",
  "search.suggest": "Aliases, unit numbers and frequent phrases that start the same way",
  "search.feed":
    "Publish the current filters as an RSS/Atom feed — new matching calls arrive in any reader, with the transcript and a link back",
  "search.feed.url":
    "The feed's address, and its only password: anybody with this URL can read every call it matches. Copy it into a reader; do not post it anywhere",
  "search.feed.audio":
    "Attach each recording as an enclosure so podcast apps can play the feed. The audio link works only for calls this feed's own filters match",
  "search.feed.items":
    "How many calls one fetch of the feed carries, newest first. 50 by default, 200 at most",
  "search.feed.enabled":
    "Turn the feed off without deleting it — its URL starts answering 404 immediately, and back on restores the same URL",
  "search.feed.rotate":
    "Mint a new token. Every copy of the old URL stops working at once, so every subscriber needs the new one",
  "search.feed.delete": "Delete this feed. Its URL stops working and cannot be brought back",
  "search.feed.fetched":
    "When a reader last fetched this feed, and how many times it has been fetched in all",
  "search.feeds":
    "Feeds you have published from a search. The URL of each one is a secret — treat it the way you would treat a password",
  /* --------------------------------------------------------------- feeds */
  "feeds.page":
    "Every feed this archive publishes, with what each one contains — the built-in ones for each kind of channel, and the feeds you have published yourself",
  "feeds.builtin":
    "Ready-made feeds: everything, or one kind of channel. Their URLs hold no password and are safe to share — they show whatever the archive already lets a reader see",
  "feeds.builtin.contents":
    "What this feed covers: how many talkgroups are filed under it, and how many calls it carried over the last seven days. The figures are recomputed every ten minutes",
  "feeds.builtin.busiest":
    "The channels that produced the most calls in this feed over the last seven days, busiest first — what you would actually hear if you subscribed",
  "feeds.builtin.url": "Copy this URL into a reader. It is not a secret and never needs rotating",
  "feeds.preview": "Open the RSS document itself in a new tab, to see exactly what a reader would fetch",
  "feeds.publishing":
    "How the feed publishes: newest first, up to 50 calls per fetch, and a call only once its transcript is final and audio processing has finished with it",
  "feeds.mine":
    "Feeds you have published from a search. The URL of each one is a secret — treat it the way you would treat a password",
  "feeds.how":
    "What RSS and Atom are for here, when an item appears, which URLs are secret, how audio enclosures behave and how long a document is cached",
  "feeds.embed":
    "A live list of transcripts any web page can show in an iframe: the time, talkgroup and words of each call as it is transcribed, each linking to the full call here. It shows only what a guest may already read",
  "feeds.embed.preset":
    "Which built-in feed the embed starts from. Emergency dispatch — law, fire, EMS, event and interop channels — is the default",
  "feeds.embed.talkgroups":
    "Narrow the feed to these talkgroup numbers, separated by commas. Pick All calls as the feed to show them whatever category they are filed under",
  "feeds.embed.minutes":
    "How much recent traffic the embed shows when it first loads, up to four hours. New calls are added live after that",
  "feeds.embed.theme": "Light or dark colours for the embed, or follow the visitor's own system setting",
  "feeds.embed.controls":
    "Show a small bar in the embed where visitors can switch feeds and add talkgroups. Turn it off for a clean, fixed list",
  "feeds.embed.compact": "One line per call instead of two, for a narrow sidebar or a busy feed",
  "feeds.embed.copy":
    "Copy the iframe snippet. Paste it into any web page and the list keeps itself up to date",
  "feeds.embed.preview": "Open the embed on its own in a new tab, exactly as a visitor would see it",

  /* ---------------------------------------------------------------- live */
  "live.toggle":
    "Follow new calls: keep this page receiving calls the moment they are recorded. Off stops live updates",
  "live.pause": "Pause live updates so you can read what is already in the list",
  "live.resume": "Resume live updates: new calls are added to the list again",
  "live.clear": "Empty the list and start collecting from now",
  "live.autoplay": "Play each new call as it arrives, like a scanner left running",
  "live.filterChip": "Narrow the live list to this channel; the filter also applies to what is played",
  "live.whoActive": "Radios heard in the last ten minutes, grouped by the channel they were on",
  "live.whoActive.radio": "Open this radio to see its full history",
  "live.feed": "Calls as they arrive, newest at the top",
  "live.quiet": "How long it has been since anything came through",

  /* --------------------------------------------------------- talkgroups */
  "talkgroups.search": "Find a channel by alias or talkgroup id",
  "talkgroups.table.alias": "The channel's name, either from the recorder's playlist or edited here",
  "talkgroups.table.tgid": "The numeric talkgroup id the radio system uses",
  "talkgroups.table.category": "What kind of channel this is — law, fire, EMS, public works and so on",
  "talkgroups.table.agency": "The agency this channel is mapped to, which is how agency filters resolve",
  "talkgroups.table.calls": "How many calls have been recorded on this channel",
  "talkgroups.table.spark": "Calls per hour over the last 24 hours",
  "talkgroups.table.lastHeard": "When this channel was last heard",
  "talkgroups.table.record":
    "Whether the audio of this channel's calls is kept. Off still transcribes and exports, then deletes the file, leaving metadata and transcript. With Transcribe also off and no exporter selecting the channel, the recorder skips it entirely",
  "talkgroups.table.transcribe": "Whether calls on this channel are sent for transcription",
  "talkgroups.table.priority":
    "Where this channel sits in the transcription queue: lower is worked first, 100 is the default",
  "talkgroups.table.color": "The color this channel is drawn in on charts and in the calls list",
  "talkgroups.table.serviceTag": "RadioReference's own name for what this channel is used for",
  "talkgroups.table.hidden": "Whether this channel is kept out of the default views",
  "talkgroups.table.exportBroadcastify":
    "Whether this channel may be carried on the Broadcastify live Feed. Read only when the Broadcastify talkgroup selection is allow or deny; in all it is ignored",
  "talkgroups.table.exportBcfyCalls":
    "Whether this channel may be re-published to Broadcastify Calls — the per-call archive, chosen separately from the Feed. Only trunked systems can be published there, so it is fixed off on a conventional one",
  "talkgroups.copyFlags":
    "Copy one export checkbox onto another across these channels — how an existing Broadcastify selection is carried over to the Calls flag without reticking it by hand",
  "talkgroups.table.exportOpenmhz":
    "Whether this channel may be re-published to OpenMHz. Read only when the OpenMHz talkgroup selection is allow or deny; in all it is ignored",
  "talkgroups.table.shared":
    "Whether this is a county-wide channel no single department owns — dispatch and interop, where everyone's traffic lands",
  "talkgroups.filter.system":
    "Show only the channels on one radio system, when this receiver hears more than one",
  "talkgroups.manageAgencies":
    "Open Admin → Agencies to add, rename or retire the agencies these channels are mapped to",
  "talkgroups.showHidden":
    "Also list the channels marked hidden. They are still recorded and searchable; hiding only keeps them out of the default lists",
  "talkgroups.edit": "Edit this channel's name, category, color and policies",
  "talkgroups.edit.alias": "The name shown everywhere in this app for this channel",
  "talkgroups.edit.category": "Group this channel with others of the same kind, for filtering and color",
  "talkgroups.edit.serviceTag":
    "RadioReference's own name for what this channel is used for, which is more specific than the category — fire dispatch as against fire tactical, say",
  "talkgroups.edit.agency":
    "Which agency owns this channel — used to answer 'which channels would this agency be on?'",
  "talkgroups.edit.priority":
    "Where this channel sits in the transcription queue: lower is worked first, 100 is the default. Live jobs run 1–499; a backlog re-run adds 500 to whatever you set",
  "talkgroups.edit.record":
    "Keep the audio of this channel's calls. Off still ingests, transcribes and exports; the file is deleted once those finish and the call, details and transcript stay. With Transcribe off too and no exporter selecting the channel, the recorder skips it",
  "talkgroups.edit.transcribe": "Turn transcription on or off for this channel entirely",
  "talkgroups.edit.minSeconds":
    "Calls shorter than this are never transcribed — they are almost always squelch tails",
  "talkgroups.edit.retention":
    "Keep this channel's calls for a different number of days than the global policy",
  "talkgroups.edit.audioProfile":
    "Process this channel's audio through a named filter chain instead of the global one — the fix for a channel with noise in a band of its own, such as rotor beat on a medevac talkgroup",
  "talkgroups.edit.color": "A color for this channel in charts and the calls list",
  "talkgroups.edit.hidden": "Hide this channel from the default views without deleting anything",
  "talkgroups.edit.shared":
    "Mark this as a shared county channel (dispatch, interop, EVENT). An agency filter skips it by default; a consumer asking for 'shared too' gets it alongside that agency's own channels",
  "talkgroups.edit.group": "The group the playlist files this channel under, shown under the alias",
  "talkgroups.edit.notes": "Your own notes about this channel",
  "talkgroups.edit.save": "Save these changes",
  "talkgroups.import": "Load aliases, groups and colors from an sdrtrunk playlist or a CSV",
  "talkgroups.import.file": "Choose the playlist XML or CSV to read",
  "talkgroups.import.run": "Import the file — existing channels are updated, new ones are created",
  "talkgroups.detail.stats": "How busy this channel is by hour, and who uses it most",
  "talkgroups.detail.calls": "Every call recorded on this channel",
  "talkgroups.detail.topRadios": "The radios heard most on this channel",
  "talkgroups.favorite": "Keep this channel at the top of your lists",
  "talkgroups.mute": "Stop this channel from playing automatically, without hiding it",

  /* ------------------------------------------------------------- roster */
  "roster.units.search": "Find a unit by callsign, name or notes",
  "roster.units.new": "Add a unit that does not exist yet, so radios can be assigned to it",
  "roster.units.import": "Load a unit roster from a CSV of callsign, name, agency, kind and notes",
  "roster.units.export": "Download every unit as a CSV you can edit and import back",
  "roster.units.edit": "Edit this unit's callsign, name, kind, agency and notes",
  "roster.units.edit.callsign":
    "What this unit is called on the air, such as E1 or A21. Renaming it relabels the radios assigned to it",
  "roster.units.edit.name": "The longer name for the callsign, such as Engine 1",
  "roster.units.edit.kind": "Whether this is a vehicle, a fixed station, a named crew, or something else",
  "roster.units.edit.agency": "Which agency this unit belongs to, so the same callsign can exist for each",
  "roster.units.edit.notes": "Your own notes about this unit",
  "roster.units.table.callsign": "The callsign as it is used on the air",
  "roster.units.table.name": "The longer name behind the callsign",
  "roster.units.table.kind": "Vehicle, station, team or other",
  "roster.units.table.agency": "The agency this unit belongs to",
  "roster.units.table.radios": "How many radios are assigned to this unit right now",
  "roster.personnel.search": "Find a person by name, rank or badge number",
  "roster.personnel.new": "Add somebody to the roster so a radio can be assigned to them",
  "roster.personnel.import": "Load a personnel roster from a CSV of name, rank, badge, agency and notes",
  "roster.personnel.export": "Download the whole roster as a CSV you can edit and import back",
  "roster.personnel.edit": "Edit this person's name, rank, badge, agency and roster status",
  "roster.personnel.edit.name": "The name shown wherever this person is credited",
  "roster.personnel.edit.rank": "What they are called on the air, such as Captain",
  "roster.personnel.edit.badge": "The number their agency knows them by",
  "roster.personnel.edit.agency": "Which agency this person belongs to",
  "roster.personnel.edit.active":
    "Whether they are still on the roster. Turning this off keeps their history but drops them from the pickers",
  "roster.personnel.edit.notes": "Your own notes about this person",
  "roster.personnel.active": "Show only the people currently on the roster, or everybody ever added",
  "roster.personnel.table.name": "The person's name",
  "roster.personnel.table.rank": "What they are called on the air",
  "roster.personnel.table.badge": "The number their agency knows them by",
  "roster.personnel.table.agency": "The agency this person belongs to",
  "roster.personnel.table.radios": "How many radios this person is holding right now",
  "roster.assign.card":
    "Who this radio belongs to: the unit it lives on and the person carrying it. Either can change without the other",
  "roster.assign.unit": "The unit this radio is assigned to; assigning one also relabels the radio",
  "roster.assign.person": "The person carrying this radio right now",
  "roster.assign.apply": "Assign the radio to what is selected, ending whatever it had of the same kind",
  "roster.assign.end": "End this assignment now. The record stays as history rather than being deleted",
  "roster.assign.history": "Every unit and person this radio has belonged to, current first",
  "roster.detail.radios": "The radios assigned right now, and how much traffic each has",
  "roster.detail.calls": "Recent transmissions from the radios currently assigned",

  /* ------------------------------------------------------------- radios */
  "radios.search": "Find a radio by unit number, alias or radio id",
  "radios.table.unit": "The apparatus or unit number this radio is assigned to",
  "radios.table.alias": "The name for this radio, from the playlist or edited here",
  "radios.table.system":
    "The trunked system this radio was heard on. One radio id can exist on two systems, as two different radios",
  "radios.table.radioId": "The radio's numeric id on the system",
  "radios.table.kind": "Whether this is a dispatch console, a vehicle mobile or a hand-held portable",
  "radios.table.agency": "The agency this radio belongs to",
  "radios.table.calls": "How many transmissions this radio has made",
  "radios.table.spark": "Transmissions per hour over the last 24 hours",
  "radios.table.lastHeard": "When this radio was last heard",
  "radios.table.topTalkgroup": "The channel this radio uses most",
  "radios.activity":
    "Show every radio, only those that have transmitted, or only those seen on the control channel but never heard keying up",
  "radios.seen":
    "Seen registering or affiliating on the control channel, but this radio has never keyed up a transmission",
  "radios.edit": "Edit this radio's unit number, alias, kind and agency",
  "radios.edit.unit": "The apparatus number, which is what links this radio to units in other systems",
  "radios.edit.alias": "The name shown everywhere in this app for this radio",
  "radios.edit.kind": "Console, mobile or portable — it changes nothing but makes lists readable",
  "radios.edit.agency": "Which agency this radio belongs to",
  "radios.edit.notes": "Your own notes about this radio",
  "radios.import": "Load radio aliases and unit numbers from a playlist or CSV",
  "radios.detail.affiliations": "The channels this radio has been heard on, most recent first",
  "radios.detail.history": "When this radio has been active over the last day",
  "radios.detail.calls": "Every transmission from this radio",
  "radios.detail.presence":
    "Where this radio is, from the control channel rather than from its calls — when it was last seen, when it last registered with a site, and which channels it is affiliated to",
  "radios.detail.presenceSource":
    "How we know: affiliation means the control channel said so, call means it was heard talking on that channel",
  "radios.detail.events":
    "What the control channel said about this radio — registering with a site, affiliating to a channel, asking for a data channel. Kept for 90 days",

  /* ------------------------------------------------------------ systems */
  "systems.table":
    "The radio systems every site, talkgroup and radio below belongs to, and how each one is mapped to RadioReference",
  "systems.edit": "Rename this system, describe it, or map it to its RadioReference entry",
  "systems.edit.name":
    "What this system is called. Recorders file calls under it by name, and it has to be unique",
  "systems.edit.trunked":
    "Whether this is a trunked system. Broadcastify Calls publishes trunked systems only, so a system marked conventional is never uploaded there — the Feed and OpenMHz are unaffected. Left on 'from the protocol' it is worked out from the decoder's own words",
  "systems.edit.description": "Your own note about who runs this system and what it covers",
  "systems.edit.rrSystemId":
    "RadioReference's own id for this system — the number in its database URL. It decides what the nightly sync pulls; clear it to unmap",
  "systems.edit.save": "Save the name, description and RadioReference mapping",
  "systems.edit.cancel": "Close without saving anything you have changed",
  "systems.merge":
    "Fold this whole system into another — the fix for a mislabeled recorder that created a second copy of one network",
  "systems.merge.target":
    "The system everything moves to. A site, talkgroup or radio it already has absorbs its twin; the rest move across as they are",
  "systems.merge.confirm":
    "Move every site, talkgroup, radio and call to the chosen system, then delete this one. No call is deleted, and this cannot be undone",
  "systems.merge.cancel": "Close without merging anything",
  "systems.delete": "Delete this system. Nothing belongs to it, so no recording is lost",
  "systems.delete.blocked":
    "This system cannot be deleted while anything still belongs to it — merge it into another one instead",
  "systems.delete.confirm": "Delete this system for good",
  "systems.delete.cancel": "Close without deleting anything; the system stays exactly as it is",

  /* -------------------------------------------------------------- sites */
  "sites.pick": "Choose which tower site to audit; the number beside it is how many findings it has",
  "sites.profile.expand":
    "Open everything known about this site — its identity, control channels, services, band plan, neighbours and traffic — and what it has never told us",
  "sites.profile.services":
    "The services this site's control channel says it is offering right now. Blank means nobody has decoded its service message here, not that it offers nothing",
  "sites.profile.unknowns":
    "The holes in this site's record: what it has never announced, what could not be resolved, and what nobody has named yet",
  "sites.frequencies":
    "Every frequency catalogued at this site, and whether calls heard on it with no talkgroup may be re-published",
  "sites.frequencies.exportBroadcastify":
    "Whether conventional calls on this frequency may be sent to Broadcastify Calls, when its talkgroup selection is allow or deny",
  "sites.frequencies.exportOpenmhz":
    "Whether conventional calls on this frequency may be sent to OpenMHz, when its talkgroup selection is allow or deny",
  "sites.messageKinds":
    "Every mfid:opcode this site's control channel has ever transmitted — what it says, not what it uses. A kind first heard in the last week is badged new",
  "sites.messageKinds.unknownOnly": "Show only the kinds nobody has named yet",
  "sites.messageKinds.known":
    "Tick once you know what this message is. A named kind stops raising the new-kind alert and leaves the unnamed list",
  "sites.messageKinds.label": "What this message kind is, in your own words. Admins only",
  "sites.edit": "Rename this site, note something about it, or map it to its RadioReference entry",
  "sites.edit.system":
    "Which system this site belongs to. A site name is unique only within its system, so two systems can each have a Springfield",
  "sites.edit.name": "What this site is called. It has to be unique within its own system",
  "sites.edit.notes":
    "Your own note about this site. Kept apart from the description, which the RadioReference sync writes",
  "sites.edit.rrSiteId":
    "RadioReference's own id for this site. Setting it overrides every matching heuristic; clear it to unmap",
  "sites.edit.save": "Save the name, note and RadioReference mapping",
  "sites.edit.cancel": "Close without saving anything you have changed",
  "sites.merge": "Fold this site into another one on the same system — the fix for one tower filed twice",
  "sites.merge.target":
    "The site everything moves to. Only sites on the same system are offered, because moving calls between systems is a re-filing",
  "sites.merge.confirm": "Move everything to the chosen site, then delete this one. This cannot be undone",
  "sites.merge.cancel": "Close without merging anything",
  "sites.delete": "Delete this site. Nothing references it, so no recording is lost",
  "sites.delete.blocked": "This site cannot be deleted while something still references it",
  "sites.delete.confirm": "Delete this site for good",
  "sites.delete.cancel": "Close without deleting anything; the site stays exactly as it is",
  "sites.audit.controlObserved":
    "The control channel this site is actually using right now, as the recorder last heard it",
  "sites.audit.controlAlternates":
    "The backup control channels the site advertises it would move to if the live one failed",
  "sites.audit.rrPrimary": "The control channel RadioReference documents for this site (its `use` is d)",
  "sites.audit.rrAlternates": "The alternate control channels RadioReference documents (their `use` is a)",
  "sites.audit.configured": "The control frequencies the recorder itself is configured to hunt for",
  "sites.audit.map":
    "Map this site's system to RadioReference, so there is something to compare what you hear against",
  "sites.audit.drift":
    "RadioReference and the air disagree about which frequency is the control channel here",
  "sites.audit.suggested":
    "The control frequency list to paste into the recorder, live control channel first",
  "sites.audit.copy": "Copy the suggested control frequencies in MHz, ready for the recorder's config",
  "sites.audit.adjacentUnknown":
    "This site announces a neighboring tower Keyup has no site for — nobody is recording it",
  "sites.audit.adjacentKnown": "The neighboring tower this announcement refers to is one Keyup knows",

  /* -------------------------------------------------------------- stats */
  "stats.tile.callsToday": "Calls recorded in the last 24 hours",
  "stats.tile.audioHours": "Total length of the audio recorded in the last 24 hours",
  "stats.tile.coverage":
    "Share of eligible calls that have a finished transcript. Calls too short to transcribe are not counted against it",
  "stats.tile.rtf":
    "Speech-to-text speed as a real-time factor: 0.1 means ten seconds of audio are transcribed every second. Under 1 keeps up with the recorder",
  "stats.tile.queue": "How many calls are waiting to be transcribed",
  "stats.tile.lag": "How long ago the newest call ended — the ingest's freshness",
  "stats.tile.gaps":
    "Stretches in the last day with no traffic at all, which usually means the recorder stopped",
  "stats.tile.talkgroups": "Distinct channels heard in the last 24 hours",
  "stats.tile.unitCalls":
    "Unit-to-unit (private) calls in the last 24 hours — one radio to one radio, on no talkgroup. Counted here because they are a subset of the total nobody would notice appear or stop",
  "stats.tile.radios": "Distinct radios heard in the last 24 hours",
  "stats.tile.disk":
    "Free space left on the volume holding the recordings. The footnote counts the calls whose audio was deleted in the last day because their channel has Record off — their metadata and transcripts are still here",
  "stats.heatmap": "How many calls fall in each hour of each day. Darker means busier, in both themes",
  "stats.heatmap.groupBy": "Break the heatmap down by day, or by channel across the whole range",
  "stats.byTalkgroup": "Which channels carry the most traffic over the current range",
  "stats.quality": "How the finished transcripts break down by confidence",
  "stats.topRadios": "The radios that key up most often",
  "stats.backends": "Each speech-to-text backend's share of the work and how fast it is running",
  "stats.gaps": "Periods with no recorded traffic, longest first",
  "stats.range": "The date range every chart on this page covers",
  "stats.chart.download": "Download this chart's numbers as a CSV",
  "stats.chart.table": "Show the chart's numbers as a table, so no value is only reachable through color",
  "stats.chart.png": "Save this chart as a PNG image",

  /* ------------------------------------------------------------- alerts */
  "alerts.table": "Rules that watch incoming calls and notify you when one matches",
  "alerts.table.name": "What this rule is called",
  "alerts.table.kind":
    "What the rule watches: a channel, a radio, a set of keywords, or a regular expression",
  "alerts.table.channels": "Where a match is delivered",
  "alerts.table.cooldown":
    "The rule stays quiet for this long after it fires, so one busy incident is not fifty alerts",
  "alerts.table.enabled": "Turn this rule on or off without deleting it",
  "alerts.table.lastFired": "When this rule last sent something",
  "alerts.create": "Add a rule that watches incoming calls and notifies you",
  "alerts.edit": "Change what this rule watches, where it delivers, or its cooldown",
  "alerts.delete": "Delete this rule permanently",
  "alerts.form.name": "A short name so you can recognize this rule in the list",
  "alerts.form.kind": "What triggers the rule",
  "alerts.form.pattern":
    "What to match: a comma-separated keyword list, or one regular expression when the kind is regex",
  "alerts.form.talkgroups": "Restrict the rule to these channels",
  "alerts.form.radios": "Restrict the rule to these radios",
  "alerts.form.channels": "How to deliver a match — a webhook, Discord, ntfy or email",
  "alerts.form.target": "Where to deliver it: the webhook URL, ntfy topic or email address",
  "alerts.form.cooldown": "Minimum seconds between two notifications from this rule",
  "alerts.form.enabled": "Start sending as soon as the rule is saved",
  "alerts.form.save": "Save the rule; it starts matching the next call that arrives",
  "alerts.notifications": "Everything the rules have delivered, and anything that failed to send",
  "alerts.notifications.status": "Whether this notification was delivered, skipped by a cooldown, or failed",
  "alerts.notifications.call": "Open the call that triggered this notification",

  /* -------------------------------------------------------------- admin */
  "admin.tab.ingest": "Whether the recorder's files are being picked up, and anything that failed",
  "admin.tab.transcription": "The speech-to-text queue: what is waiting, running and failing",
  "admin.tab.backends": "The speech-to-text servers, their health and their throughput",
  "admin.tab.audio": "How stored recordings are levelled, trimmed and de-toned for playback",
  "admin.audio.settings":
    "The processing chain, top to bottom. Changes apply to calls processed from now on; use Reprocess to bring the archive up to date",
  "admin.audio.reprocess":
    "Queue calls for the processing pass at the backlog band, behind anything arriving live. Without Force, only calls with no processed copy or an out-of-date one are queued, so this is safe to press twice",
  "admin.audio.preview":
    "Run the whole chain over one call with the settings as they stand and hear the result. Nothing is stored and the call is not changed",
  "admin.audio.profiles":
    "Named filter chains a talkgroup can be processed through instead of the global one. A profile replaces the denoiser and gate stages; the trim and the loudness tail are measured per call and stay",
  "admin.audio.profiles.new": "Build a new filter chain from the steps the server allows",
  "admin.audio.profiles.edit":
    "Open this chain in the editor. Editing it re-renders nothing on its own — press Apply afterwards to bring the assigned channels up to it",
  "admin.audio.profiles.delete":
    "Delete this profile. Refused while any talkgroup is still on it, because that would quietly move those channels back to the global chain",
  "admin.audio.profiles.apply":
    "Re-render the last 24 hours on every channel already using this profile — what to press after editing it",
  "admin.audio.profiles.builtin":
    "One of the three shipped profiles. Editable, so a different noise floor is yours to set, but not deletable",
  "admin.audio.profiles.name": "What this chain is called wherever a channel names its profile",
  "admin.audio.profiles.description": "What this chain is for, in a sentence, for whoever finds it next",
  "admin.audio.profiles.addStep":
    "Append a filter to the end of the chain, with the whitelist's own defaults already in it",
  "admin.audio.profiles.stepUp": "Move this filter earlier — the audio meets the steps in order",
  "admin.audio.profiles.stepDown": "Move this filter later — the audio meets the steps in order",
  "admin.audio.profiles.removeStep": "Take this filter out of the chain",
  "admin.audio.profiles.preview":
    "Render this chain over one call's original audio and hear it. Nothing is stored, and the profile need not exist yet",
  "admin.audio.profiles.ab":
    "Switch the player between the original recording and the same audio through this chain. Both stay loaded, so the comparison costs nothing",
  "admin.audio.profiles.save": "Store this chain. The server validates every number before it does",
  "admin.audio.profiles.cancel": "Close the editor without storing anything",
  "admin.tab.agencies":
    "The agencies a talkgroup, radio, unit or person can belong to: the seeded agency list plus the ones added here",
  "admin.agencies.add": "Add an agency the county's dispatch system has no code for",
  "admin.agencies.add.save":
    "Create the agency. It is marked local, so re-seeding the shared list leaves it alone",
  "admin.agencies.edit":
    "Change this agency's name, short name, kind or whether it is offered in the pickers",
  "admin.agencies.save":
    "Save the changes. Editing a seeded row also stops the next re-seed putting the old name back",
  "admin.agencies.delete": "Delete this agency. Only for a local one nothing is mapped to",
  "admin.agencies.delete.confirm": "Delete it for good. The code is free to be typed again afterwards",
  "admin.agencies.code":
    "The key every talkgroup, radio, unit and person points at. Capitals, digits, hyphen and underscore; it cannot be changed later",
  "admin.agencies.name":
    "The full name, as you want it to read in filters and on the roster. Edited in the row",
  "admin.agencies.name.seed":
    "Spelled by the seeded agency list, which keyup seed-agencies refreshes, so it is not edited in the row. The pencil overrides it and stops the next re-seed putting the old name back",
  "admin.agencies.shortName": "The abbreviation the tables show. Left empty, the code is used",
  "admin.agencies.kind": "What sort of agency it is, in the same six words the dispatch system uses",
  "admin.agencies.active":
    "Off takes it out of the pickers without touching anything mapped to it — the retirement that is not a delete",
  "admin.agencies.inactive":
    "Retired: it is no longer offered when mapping a channel, but its history is intact",
  "admin.agencies.source.seed":
    "Loaded from this deployment's agency list (usually the dispatch system's own codes), and refreshed by keyup seed-agencies",
  "admin.agencies.source.seedEdited":
    "From the seeded agency list, but renamed here — the seed now leaves it alone unless it is run with --force",
  "admin.agencies.source.local":
    "Added in Keyup for an agency the dispatch system has no code for. Yours to rename and delete; a re-seed never touches it",
  "admin.tab.retention": "How long recordings, audio and transcripts are kept",
  "admin.tab.settings":
    "Runtime settings, which take effect without a restart. Audio, Export and Feed stats are not here: they live on the Audio tab, the Exports tab and the Feed Stats page, beside the status they act on",
  "admin.tab.https":
    "The archive's own TLS certificate: whether HTTPS is served, for which hostnames, and how Let's Encrypt checks them — HTTP-01 on public ports 80/443, or DNS-01 answered with a Cloudflare token",
  "admin.https.settings":
    "The HTTPS configuration. Saving here renders a new proxy configuration and loads it into the running edge — no redeploy, and no dropped connection",
  "admin.https.status":
    "What the proxy is serving right now, beside what these settings ask for, and the certificate on the wire for each hostname",
  "admin.https.apply":
    "Push the settings into the running proxy again. Saving already does this; the button is for retrying after a failure, or after the proxy has restarted",
  "admin.https.testDns":
    "Ask Cloudflare whether this token is live and can see the zone your hostnames live in. Issues nothing and spends no rate limit",
  "admin.tab.exports":
    "Re-publishing this system's calls to Broadcastify Calls and OpenMHz, and the live Broadcastify Feed — the only place the Export settings are edited",
  "admin.exports.service":
    "One outbound service: its switch, its credentials, which talkgroups it may publish, and how its queue is doing",
  "admin.exports.enable":
    "Start or stop uploading to this service. Only calls recorded after it is switched on are ever sent, and running a second uploader on the same system posts every call twice",
  "admin.exports.test":
    "Check the configuration without publishing anything — the result says exactly what could be verified, which for Broadcastify is not the API key itself",
  "admin.exports.feed":
    "The Broadcastify *Feed* — the live audio stream on the feed's page, configured with the Server, Port, Mount, Password and Username from its Feed Technical Details (Advanced) panel",
  "admin.exports.feed.enable":
    "Open the mount and stream live calls to it. An Icecast mount takes one source at a time: stop your other broadcasting client first",
  "admin.exports.feed.test":
    "Open the mount, write about two seconds of silence, close it — the only check Icecast allows. No call audio is published",
  "admin.exports.shared": "Rate limits, retries and the rules both services share",
  "admin.exports.save": "Write the changed export settings; the worker picks them up on its next cycle",
  "admin.exports.discard": "Throw away the unsaved changes on this page and go back to what is stored",
  "admin.exports.details": "Show or hide this service's settings. The status above it is always shown",
  "admin.exports.notTrunked":
    "Broadcastify Calls publishes trunked systems only, and this archive has none marked trunked — mark one on the Systems list before turning this on",
  "admin.exports.systems":
    "Which systems Calls may publish. Only trunked systems are offered: a conventional one is refused whatever is ticked",
  "admin.exports.recent": "The last 50 ledger rows across both services, newest first",
  "admin.diskFree":
    "Free space under the audio root. Days to full is fitted over the last 24 h of the worker's five-minute samples; amber or red once the disk warning or critical threshold is crossed.",
  "admin.feedstats.link":
    "Open the Feed stats page: listeners, hot hours and archives for the Broadcastify live feed",
  "admin.feedstats.window": "How far back the heatmaps, the peak and the correlation are computed over",
  "admin.feedstats.probe":
    "Fetch the feed status once and show the raw response. Stores nothing; use it to confirm the field names",
  "admin.feedstats.pollnow": "Poll once and keep the sample, exactly as the scheduled poller would",
  "admin.feedstats.pollpage":
    "Fetch the feed's public listen page and fill in the last 24 hours from the sparkline it shows. Hours already polled here are never overwritten",
  "admin.feedstats.listeners": "Listeners at the most recent successful poll",
  "admin.feedstats.online": "Whether the last poll found the feed up. Nothing here restarts a feed",
  "admin.feedstats.peak": "The highest single listener reading in the window, and when it was taken",
  "admin.feedstats.calls":
    "Calls this system recorded in the same window, on the talkgroups the feed carries",
  "admin.feedstats.correlation":
    "How closely listener numbers track call volume, hour by hour. Association only — neither one causes the other",
  "admin.feedstats.chart": "Average and peak listeners per bucket, from the hourly rollup",
  "admin.feedstats.step": "Bucket the listener history by hour or by day",
  "admin.feedstats.archiveday": "Show one archive day, or every day that has been listed",
  "admin.feedstats.sync": "Fetch the last week of archive listings now, ignoring the switch and the interval",
  "admin.feedstats.save": "Write the changed feed settings; the poller picks them up on its next tick",
  "admin.feedstats.import":
    "Import one of the feed dashboard's CSV exports — Broadcastify → Manage → your feed → Statistics → export hourly listeners or uptime. Which of the two it is is read from the file; an imported hour never overwrites one polled here",
  "admin.feedstats.daily":
    "One bar per day's peak listeners and a line for its uptime, from the daily CSV export. The only listener history that reaches back months",
  "admin.feedstats.coverage":
    "Calls cover the whole window. Listener hours reach back to the first poll, plus what the public page and the dashboard's hourly export fill in; the daily export reaches back months. The feed API returns one instant's count and no history",
  "admin.feedstats.quietest":
    "The ten least busy cells of the grid — where maintenance costs the fewest listeners",
  "admin.feedstats.quietwindows":
    "The quietest runs of two hours or more, which is what a restart actually needs; they may wrap midnight",
  "admin.feedstats.backfill":
    "Also catch up every archived day Broadcastify lists but this database has never stored, oldest first",
  "admin.exports.retry":
    "Put this call back in the queue for another attempt. An already-uploaded call is never re-sent",
  "admin.tab.tokens": "API tokens for recorders and other systems",
  "admin.tab.users": "Who can sign in and change things",
  "admin.ingest.status": "Whether the folder watch is running, and how fresh the archive is",
  "admin.ingest.rescan": "Walk the whole audio root again now, in case something was missed",
  "admin.ingest.runs": "The last few ingest batches and what each one found",
  "admin.ingest.files": "Files the ingest could not import, with the reason",
  "admin.ingest.retry": "Try this file again now",
  "admin.ingest.filter": "Which files to list: only the failures, the duplicates, or everything",
  "admin.jobs.table": "Transcription jobs, newest first",
  "admin.jobs.status": "Which jobs to show",
  "admin.jobs.requeue": "Put the failed jobs back in the queue to be tried again",
  "admin.jobs.error": "Why this job failed the last time it ran",
  "admin.backends.health": "Whether the backend answered its last health check",
  "admin.backends.add":
    "Define a new transcription backend. The worker picks it up within a few seconds — no restart",
  "admin.backends.edit":
    "Change where this backend points, what model it uses and how much work it takes at once",
  "admin.backends.delete":
    "Remove this backend from the routing configuration. If any transcript names it the row is kept, disabled, so the history stays answerable",
  "admin.backends.probe":
    "Ask this backend right now whether it works, and time it transcribing a short recording from this archive",
  "admin.backends.version":
    "The configuration generation the database is at, and the one the worker's router was built from. They differ for a few seconds after a save",
  "admin.backends.source": "Whether this backend was seeded from STT_BACKENDS or written here",
  "admin.backends.kind":
    "Which engine adapter to use: a remote OpenAI-compatible server, Intel's OVMS, or an in-process local model",
  "admin.backends.baseurl": "The server's base URL, for the backends that talk to one",
  "admin.backends.apikey":
    "Sent with every request to this server. Stored and never shown again — leave it blank when editing to keep the one already saved",
  "admin.backends.model": "The model name the server knows this by, or the local model path",
  "admin.backends.device":
    "Where a local engine runs: cpu or cuda for faster-whisper, CPU/GPU/NPU/AUTO for OpenVINO",
  "admin.backends.computetype": "How faster-whisper quantizes the model: int8 is the usual choice on a CPU",
  "admin.backends.cputhreads": "How many CPU threads a local engine may use",
  "admin.backends.cachedir": "Where OpenVINO keeps its downloaded model and compiled blobs",
  "admin.backends.useprompt":
    "Send the auto-built domain prompt (aliases, unit numbers, street names) to this engine",
  "admin.backends.usehotwords":
    "Send the hotword list. Off by default: some engines read the list back instead of the audio",
  "admin.backends.concurrency": "How many transcriptions may run against this backend at once",
  "admin.backends.priority": "Which backend is tried first. Lower numbers win",
  "admin.backends.timeout":
    "How long one transcription may take on this backend before it is treated as failed",
  "admin.backends.notes": "Anything you want to remember about this backend. Not used by anything",
  "admin.ingest.sources":
    "Every configured place recordings come from: watched directories, recorders that push, and bulk imports",
  "admin.ingest.sources.add":
    "Add a watched directory, name a recorder's upload token, or track a bulk import",
  "admin.ingest.sources.edit":
    "Change this source. A watched directory is re-pointed within about thirty seconds, with no restart",
  "admin.ingest.sources.delete": "Remove this source. Nothing already ingested is affected",
  "admin.ingest.sources.enabled": "Whether the watcher acts on this source at all",
  "admin.ingest.sources.path":
    "The directory to watch. It is saved even if it is not there yet — a mount that comes up later starts working on its own",
  "admin.ingest.sources.label": "What you call this source",
  "admin.ingest.sources.kind":
    "A watched directory, a recorder pushing over an upload token, or a bulk import reporting its progress",
  "admin.ingest.sources.token": "The upload token this recorder presents in its `key` field",
  "admin.ingest.sources.recursive": "Descend into subdirectories — sdrtrunk writes a day/talkgroup tree",
  "admin.ingest.sources.prefer":
    "Which file to keep when trunk-recorder wrote a call as both a WAV and an M4A",
  "admin.ingest.sources.sidecar":
    "How long to wait for trunk-recorder's .json sidecar before ingesting from the filename alone",
  "admin.ingest.sources.notes": "Anything you want to remember about this source",
  "admin.ingest.sources.state":
    "Whether this process can actually read that directory, checked every time this page loads",
  "admin.backends.rtf": "How fast this backend transcribes, as seconds of audio per second of wall clock",
  "admin.backends.queue": "How many calls are waiting on this backend",
  "admin.backends.enabled": "Whether the scheduler is allowed to route work here",
  "admin.backends.toggle":
    "Turn this backend on or off without editing STT_BACKENDS or restarting — the worker picks the change up within a few seconds and finishes whatever it has in flight",
  "admin.backends.reset": "Drop the override and go back to what STT_BACKENDS says",
  "admin.backends.inflight": "Transcriptions running on this backend right now, out of its concurrency limit",
  "admin.backends.completed": "Transcriptions this backend has finished since the worker started",
  "admin.backends.failed": "Attempts on this backend that ended in an error since the worker started",
  "admin.backends.mean": "How long a transcription takes on this backend on average",
  "admin.backends.recent": "Finished and failed on this backend in the last minute",
  "admin.backends.lastsuccess": "When this backend last returned a transcript",
  "admin.backends.lasterror": "The last error this backend returned; hover for the whole message",
  "admin.backends.uptime": "How long the worker process reporting these counters has been running",
  "admin.backends.queue.live":
    "Live calls waiting to be transcribed — recent traffic, ahead of any backlog import",
  "admin.backends.queue.backlog": "Backlog calls waiting; these yield to live traffic",
  "admin.backends.queue.running": "Calls a backend is transcribing right now",
  "admin.backends.queue.completed": "Transcriptions finished in the last hour, counted from the queue table",
  "admin.backends.queue.failed": "Jobs that gave up in the last hour",
  "admin.backends.queue.mean": "Average wall time per transcription over the last hour",
  "admin.backends.queue.throughput": "Transcriptions finished per minute over the last ten minutes",
  "admin.backends.queue.eta": "How long the current queue takes to drain at that rate",
  "admin.retention.table": "Retention rules, most specific last — a channel rule beats the global one",
  "admin.retention.keepDays": "Delete the call record entirely after this many days; blank keeps it forever",
  "admin.retention.keepAudioDays":
    "Delete or archive just the audio after this many days, keeping the metadata",
  "admin.retention.keepTranscripts": "Keep the transcript even once the audio has gone",
  "admin.retention.archivePath": "Move the audio here instead of deleting it",
  "admin.retention.add": "Add a retention rule for a system or a single channel",
  "admin.retention.run": "Apply the retention rules now rather than waiting for tonight's run",
  "admin.settings.form":
    "Runtime settings; anything left at its default follows the environment. Groups with a page of their own — Audio, Export, Feed stats — are edited there instead, and linked at the foot of this form",
  "admin.settings.save": "Save these settings",
  "admin.settings.discard": "Throw away every unsaved edit on this tab and go back to what is stored",
  "admin.settings.reset": "Put this setting back to its default",
  "admin.settings.default": "This setting is still at its default value",
  "admin.settings.changed": "This setting has been changed from its default and saved",
  "admin.settings.unsaved": "You have edited this here; nothing is stored until you save",
  "admin.settings.filterClear": "Clear the filter and show every group again",
  "admin.tokens.table": "Tokens that let recorders and other systems reach the API",
  "admin.tokens.create": "Create a token — you will only see its value once",
  "admin.tokens.label": "What this token is for, so you know what breaks if you revoke it",
  "admin.tokens.scopes":
    "What this token may do: read the archive, change it, administer it, receive live updates, or push recordings",
  "admin.tokens.expires": "When the token stops working; leave it blank for a token that never expires",
  "admin.tokens.copy": "Copy the token — this is the only time it is shown",
  "admin.tokens.revoke": "Revoke this token immediately",
  "admin.tokens.delete": "Delete this revoked token's row for good — the audit trail goes with it",
  "admin.tokens.delete.cancel": "Leave the revoked row where it is",
  "admin.tokens.delete.confirm": "Delete the row permanently; this cannot be undone",
  "admin.tokens.lastUsed": "When this token last made a request",
  "admin.tokens.usedBy":
    "The recorders that first reported holding this token, so you can see what revoking it would take off the air",
  "admin.users.table": "Accounts that can sign in",
  "admin.users.create": "Add an account that can sign in and change things",
  "admin.users.admin": "Administrators can change settings, tokens and other accounts",
  "admin.users.active": "Turn an account off without deleting its history",
  "admin.users.save": "Save this account",
  "admin.users.password":
    "The password this account signs in with. A new account needs one; setting it again replaces the old one straight away",

  /* --------------------------------------------------------- recorders */
  "admin.tab.recorder":
    "The recorders keyup manages, each tuner's live status, and what you want it tuned to",
  "admin.recorder.edit":
    "Change what this tuner should be doing — its role, focus site, center frequency and streaming",
  "admin.recorder.role":
    "What this tuner is for: follow the control channel, carry voice grants, decode the data channel, or let the recorder decide",
  "admin.recorder.site": "Which site this tuner should focus on",
  "admin.recorder.hardware":
    "The antenna run this tuner is on, as the recorder's operator wrote it down. Keyup only displays it — change it in trunkd's own panel or in the tuner's [[device]] block",
  "admin.recorder.center":
    "The center frequency to tune to, in hertz; it must fall within the tuner's current span",
  "admin.recorder.enabled": "Whether the recorder should use this tuner at all",
  "admin.recorder.spectrum": "Whether the recorder should stream a live spectrum for this tuner",
  "admin.recorder.bands":
    "Which downlink ranges this tuner may record inside its capture span: the bands its antenna and filtering actually reach, or explicit MHz ranges. No bands means no restriction, and the recorder refuses a list that misses its span",
  "admin.recorder.save": "Send this desired state to the recorder; it applies it on its next poll",
  "admin.recorder.sampleRate":
    "How wide a slice of spectrum this tuner captures, in hertz. Divided by the 12.5 kHz channel step it has to come out an even whole number of at least four channels, because that is what the channelizer works in",
  "admin.recorder.bandwidth":
    "The analog filter in front of the converter, in hertz. Leave it empty to let the driver pick one to match the sample rate",
  "admin.recorder.gain":
    "The overall gain in dB, which a Soapy driver spreads across the stages below. Read it against the level meter on the tuner row: too little and the front end hears nothing, too much and the converter compresses",
  "admin.recorder.agc":
    "Let the hardware set its own gain. Convenient on a quiet band and a poor idea next to a strong pager transmitter, which is what it will turn the gain down for",
  "admin.recorder.lna":
    "The HackRF's front-end low-noise amplifier, in 8 dB steps to 40 dB. It sets how much signal reaches the mixer, and is the first thing to raise on a weak site",
  "admin.recorder.vga":
    "The HackRF's baseband variable-gain amplifier, in 2 dB steps to 62 dB. It sets the level into the converter, and is the one to lower when the level meter reads hot",
  "admin.recorder.amp":
    "The HackRF's extra 14 dB front-end amplifier. It raises the noise floor along with the signal, so it helps a genuinely weak site and hurts a merely distant one",
  "admin.recorder.rxPort":
    "Which antenna connector this device receives on, when it has more than one. Empty leaves the driver's own default",
  "admin.recorder.ppm":
    "Crystal correction in parts per million. A dongle that is a few ppm out tunes a few kilohertz off and drifts further as it warms up, until it loses the control channel",
  "admin.recorder.autoPpm":
    "Let the recorder measure its own error against the control channel and correct itself. What it has settled on is shown below, and is worth a look before trusting it",
  "admin.recorder.autoPpmLimit":
    "How far the automatic correction may wander from zero. A bound rather than a target: it stops a bad lock from walking the tuner off the system entirely",
  "admin.recorder.dataFollow":
    "Follow SNDCP data grants as well as voice ones. Needed for the unknown-data analysis, at the cost of a recorder slot per data call",
  "admin.recorder.controlCandidates":
    "The frequencies this tuner tries when hunting for a control channel, in order. They have to sit on the 12.5 kHz grid and inside the tuner's span",
  "admin.recorder.eventLogs":
    "Days of the recorder's raw control-channel log that have been archived here. The firehose is never stored as rows, so this file is the only copy of what was actually on the air that day",
  "admin.recorder.link":
    "The peers this recorder is linked to: whether each is alive, when it was last heard, and what it currently claims",
  "admin.recorder.remoteGrants":
    "Let this tuner act on grants a linked peer published, not only the ones it decoded itself. It needs the recorder's link to be up and accepting them; a recorder with no link refuses this",
  "admin.recorder.settings":
    "Everything keyup can tell this recorder as a whole rather than one of its tuners: its name, the upload hold, the event-log archive and the link to its peers",
  "admin.recorder.settings.instance":
    "The identity this recorder names itself by. Renaming it here carries everything already filed under the old name with it — but the recorder itself has to be reconfigured to match, or its next poll creates a second row under the old name",
  "admin.recorder.settings.station":
    "Where this recorder stands and who runs it, as its own operator wrote it down. Keyup only displays it — change it in trunkd's own panel or in its [station] block",
  "admin.recorder.settings.spoolOnly":
    "Hold this recorder's calls in its own spool instead of uploading them. Leave it to the recorder's own config to keep whatever its file or front panel says",
  "admin.recorder.settings.archive":
    "Have this recorder ship its daily raw control-channel log here. That file is the only copy of what was actually on the air, because the firehose is never stored as rows",
  "admin.recorder.settings.linkManaged":
    "Whether keyup sets this recorder's link at all. Off leaves the recorder on whatever its own configuration says, which is what every recorder does until you turn this on",
  "admin.recorder.settings.linkEnabled":
    "Whether the link runs. Off keeps the settings — bind address, peers, key — without bringing it up, which is how you stage one before the second recorder exists",
  "admin.recorder.settings.linkBind":
    "The address and port this recorder listens on for its peers, as host:port. 0.0.0.0 listens on every interface",
  "admin.recorder.settings.linkPeers":
    "The other recorders in the mesh, each as host:port — the address that recorder binds, reached from this one",
  "admin.recorder.settings.linkKey":
    "The shared secret every recorder in the mesh authenticates with. It has to be identical on all of them, so generate it once and paste the same value into each",
  "admin.recorder.settings.linkKeyReveal":
    "Show the stored key so you can copy it into another recorder. It is fetched only when you ask, and is not in the recorder list",
  "admin.recorder.settings.linkKeyGenerate":
    "Generate a fresh 32-byte key. Nothing is saved until you save this dialog, and every recorder in the mesh needs the same one",
  "admin.recorder.settings.linkHello":
    "How often this recorder announces itself to its peers, in seconds. Too long and a peer is called dead before it has spoken",
  "admin.recorder.settings.linkAccept":
    "Act on the grants peers publish, rather than only the ones this recorder decoded itself",
  "admin.recorder.settings.linkPublish": "Tell the peers about the grants this recorder decoded",
  "admin.recorder.settings.linkDefer":
    "Leave a call to the peer that claimed it first, instead of recording a second copy of it here",
  "admin.recorder.settings.fingerprint":
    "The first eight hex of the shared key's fingerprint. Two recorders whose fingerprints differ cannot link, and this is the one way to see that without showing the key",
  "admin.recorder.settings.save":
    "Send these settings to the recorder; it applies them on its next poll and echoes back what it took",
  "admin.recorder.occupancy":
    "Every channel each site is using, laid out by frequency and colored by how recently it was active",
  "admin.recorder.station.map": "Open this station's coordinates on OpenStreetMap in a new tab",
  "admin.recorder.lowDisk":
    "Less than 5 GiB free where this recorder spools: it is close to having nowhere to put a call it cannot upload",
  "admin.recorder.freqError":
    "How far the tuner is tracking off the control channel, in hertz; only a control-following tuner measures one",
  "admin.recorder.standby":
    "What this recorder is for: hot records whenever it is healthy, warm waits with its tuners off until keyup promotes it because the hot recorders went quiet, cold is never switched on automatically",
  "admin.recorder.priority": "Which standby to prefer when more than one could cover a site — lower wins",
  "admin.recorder.label":
    "A name for this recorder, for when its instance id is a hostname nobody recognizes; blank shows the id",
  "admin.recorder.forget":
    "Forget this recorder. Nothing it recorded is touched — what goes is the name and standby policy set here",
  "admin.recorder.forget.confirm":
    "Forget this recorder. A recorder that is still reporting refuses, because its next poll would create it again",
  "admin.recorder.forget.cancel": "Close without forgetting anything; the recorder stays as it is",
  "admin.recorder.connect":
    "Mint a key for a new recorder and get the configuration to paste into it, already carrying the key",
  "admin.recorder.connect.label": "What this key is for, so you know which recorder breaks if you revoke it",
  "admin.recorder.connect.mint":
    "Create a key carrying the upload scope and nothing more, and fill it into the configuration below",
  "admin.recorder.connect.kind": "Which recorder software this configuration is for",
  "admin.recorder.connect.copyToken": "Copy the key — this is the only time it is shown",
  "admin.recorder.connect.copySnippet": "Copy the configuration, ready to paste into the recorder",
  "admin.recorder.reported":
    "How long since this recorder last reported its status; it stops being trusted to cover a site once this goes past the staleness threshold",
  "admin.recorder.promoted":
    "Keyup switched this standby on because the hot recorders for its site stopped reporting; it stands down on its own once they are back",
  "admin.recorder.uplink":
    "What this recorder is doing with the calls it records: uploading them, or holding them in its own spool until the hold is released",
  "admin.recorder.uplink.hold":
    "Stop this recorder uploading. It keeps recording and keeps every call in its local spool, which it sends once you release the hold",
  "admin.recorder.uplink.release":
    "Let this recorder upload again; everything it has spooled while held goes up behind it",
  "admin.recorder.uplink.release.confirm":
    "Release the hold. The spooled calls upload from the recorder in the background",
  "admin.recorder.uplink.release.cancel": "Close without releasing; the recorder keeps holding",
  "admin.recorder.uplink.source":
    "Where the current setting came from: the recorder's own configuration, keyup, or the switch on the recorder's panel",
  "admin.recorder.uplink.pending":
    "The recorder has not yet polled for this change; it applies on its next poll",
  "call.source":
    "Which receiver's recording this is, and how well it heard the call — voice frames decoded, frames it could not, and the level it was hearing",

  /* --------------------------------------------------------------- help */
  "help.search": "Filter this page down to the controls whose description mentions a word",
  "help.area": "Jump to the controls on this page",
  "help.talkgroupFields":
    "Jump to what a talkgroup's agency, category and service tag mean, and what belongs in each",

  /* -------------------------------------------------------------- login */

  /* ------------------------------------------------------- live activity */
  "status.health":
    "A health condition the worker raised and has not seen recover for five minutes: a hot recorder silent, no new calls, live transcription stalled or the backup overdue. Rules of kind health deliver it. Thresholds: Admin → Settings → Health.",
  "status.diskLow":
    "Free space is under the disk warning threshold (or the critical one). The estimate comes from the last 24 h of the worker's five-minute samples. Thresholds: Admin → Settings → Retention.",
  "status.live.window":
    "How far back the counters reach. Every tile shows this window and compares it with the window of the same length before it",
  "status.live.lastCall":
    "How long ago the newest call reached the archive. Seconds on a busy system; minutes of silence on a system that was talking is worth a look",
  "status.live.ingested":
    "Calls stored in this window, from every recorder. The small number beside it is the same count over the previous window",
  "status.live.duplicates":
    "Copies of the same transmission that reconciliation folded together in this window. Two recorders hearing one call is normal; a sudden jump is not",
  "status.live.speech": "Calls the audio pass measured as somebody actually talking",
  "status.live.pages": "Calls whose first seconds carry a paging or alert tone",
  "status.live.noSpeech":
    "Calls with no speech in them at all — a mic key-up or a squelch tail. They are never uploaded",
  "status.live.static":
    "Calls that were noise rather than a voice. A rising static count is an RF or radio fault, not quiet traffic",
  "status.live.audioProcessed": "Audio jobs that finished in this window: trimmed, levelled and measured",
  "status.live.audioDiscarded":
    "Calls whose recording was deleted in this window because their channel has Record turned off",
  "status.live.transcribed": "Transcription jobs that finished in this window",
  "status.live.transcriptsRejected":
    "Transcripts the engine produced and Keyup threw away as hallucinations. A few is normal; a lot means the prompt or the model needs attention",
  "status.live.audioQueue":
    "Audio jobs waiting right now. It turns amber when more is queued than finished this window",
  "status.live.sttQueue":
    "Transcription jobs waiting right now. It turns amber when more is queued than finished this window",
  "status.live.exportsPending": "Calls waiting in the export ledger for their turn at an upload",
  "status.live.webhooks":
    "Webhook deliveries that succeeded in this window; the tile turns red when any failed",
  "status.live.heartbeat":
    "This recorder is green when a call landed in the last minute, amber under five, red beyond that, and grey when none has for a day",
  "status.live.listen":
    "Opens this recorder's own panel in a new tab, where its live monitor plays what the tuners are hearing. Keyup does not carry that audio. A loopback address only opens from a browser on the recorder itself",
  "status.live.exportUploaded": "Calls this service accepted in the current window",
  "status.live.exportFailed": "Uploads this service refused or errored on in the current window",
  "status.live.exportPending": "Calls queued for this service right now",
  "status.live.exportSkipped":
    "Why calls were not sent: a duplicate already uploaded, no speech, static, past the skew window, or claimed by the recorder itself",

  "login.username": "The account name you were given",
  "login.password": "The password for that account",
  "login.submit": "Sign in and return to what you were doing",
  "login.why": "Reading is open to everyone; signing in is only needed to change things",
} as const;

const HELP = HELP_TEXT;

export type HelpId = keyof typeof HELP_TEXT;

export interface HelpArea {
  /** The id prefix this area collects. */
  prefix: string;
  label: string;
  description: string;
}

/** How `/help` groups the registry; the order is the order the page renders. */
export const HELP_AREAS: HelpArea[] = [
  {
    prefix: "app",
    label: "Getting around",
    description:
      "The shell every page sits in: navigation, the global search box, the theme toggle and the status badges in the header.",
  },
  {
    prefix: "calls",
    label: "Calls",
    description:
      "The main view. A filter rail down the left, the matching calls in the middle, facet counts on the right, and the player pinned to the bottom.",
  },
  {
    prefix: "player",
    label: "Player",
    description:
      "The sticky player at the foot of the page. It follows whatever you select and can roll on through a list or follow new calls as they arrive.",
  },
  {
    prefix: "call",
    label: "Call detail",
    description:
      "Everything known about a single transmission: waveform, transcript synced to playback, the exchange it belongs to, your notes and the raw file.",
  },
  {
    prefix: "search",
    label: "Search",
    description:
      "Full-text search over transcripts, notes and aliases, with highlights, saved searches, and the RSS/Atom feeds a search can be published as.",
  },
  {
    prefix: "feeds",
    label: "Feeds",
    description:
      "The RSS and Atom feeds the archive publishes: a built-in one for every kind of channel, the feeds a search has been published as, and the rules both follow.",
  },
  {
    prefix: "live",
    label: "Live",
    description: "Calls as they arrive, who has been active recently, and optional automatic playback.",
  },
  {
    prefix: "talkgroups",
    label: "Talkgroups",
    description:
      "The channel directory: volume, category, agency and the per-channel transcription and retention policies.",
  },
  {
    prefix: "radios",
    label: "Radios",
    description: "Every radio heard on the system, what unit it is, and which channels it uses.",
  },
  {
    prefix: "roster",
    label: "Units and personnel",
    description:
      "Who was on the radio: the units a callsign names, the people who carry the radios, and the assignments tying the two together over time.",
  },
  {
    prefix: "systems",
    label: "Systems",
    description:
      "The radio systems everything else hangs off — renaming one, folding a mislabeled duplicate into the real one, and mapping it to RadioReference so the channel audit has something to compare against.",
  },
  {
    prefix: "sites",
    label: "Sites",
    description:
      "The tower sites of each system and their channel audit: what the site announces on the air, what RadioReference documents, what the recorder hunts, and every difference between the three.",
  },
  {
    prefix: "stats",
    label: "Statistics",
    description: "Volume, activity patterns, transcription coverage and throughput, and recorder uptime.",
  },
  {
    prefix: "alerts",
    label: "Alerts",
    description: "Rules that watch incoming calls, where they deliver, and the log of what they have sent.",
  },
  {
    prefix: "admin",
    label: "Admin",
    description:
      "Operating the system: ingest health, the transcription queue and backends, retention, settings, API tokens and accounts.",
  },
  {
    prefix: "status",
    label: "Live activity",
    description:
      "The panel at the top of Admin and Statistics: what the pipeline has ingested, processed, transcribed and uploaded in the last few minutes, one heartbeat per recorder, and a ticker of the operational log.",
  },
  {
    prefix: "login",
    label: "Signing in",
    description: "Reading is open to everyone. An account is only needed to change something.",
  },
  {
    prefix: "help",
    label: "This page",
    description: "The controls on the help page itself.",
  },
];

/** Every id in one area, in registry order. */
export function idsForArea(prefix: string): HelpId[] {
  return (Object.keys(HELP) as HelpId[]).filter((id) => id.split(".")[0] === prefix);
}

/** A readable name for a control from its id, for the help page's left column. */
export function labelForId(id: HelpId): string {
  const parts = id.split(".").slice(1);
  if (parts.length === 0) return id;
  const words = parts
    .join(" ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
