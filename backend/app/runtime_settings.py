"""The ``settings`` table layered over the environment (ARCHITECTURE §4.7).

:class:`app.config.Settings` is the deployment's own configuration, read once from the
environment and cached for the life of the process.  The Admin page, however, writes
*runtime* overrides into the ``settings`` table, and until something reads them back they
are decoration.  :func:`effective_settings` is that read: the environment defaults with
every override from the table applied on top, validated, and cached for
:data:`CACHE_TTL_S` seconds so the hot request path costs no query.

Only the keys in :data:`SETTINGS_SPEC` may be overridden, and the spec deliberately lists
*only* keys a consumer actually reads through this function:

``auth_require_read``
    :func:`app.auth.mutation_guard`, so closing the archive takes effect on the next
    request rather than the next deploy.
``auth_session_days`` / ``auth_idle_hours``
    :mod:`app.auth`'s two session deadlines — the absolute one, read when a session is
    created, and the sliding idle one, read on every request.  Runtime rather than
    environment because "how long does a sign-in last" is a policy an operator changes and
    watches, not a property of the deployment; the environment keeps its role as the seed.
``tls_*``
    :mod:`app.edge` — the HTTPS edge.  The odd ones out here: they are not read by a request
    path at all, they are *rendered into a Caddyfile* and pushed into the running proxy over
    its admin API when they change (:mod:`app.api.edge`).  So the switch does take effect
    without a deploy, which is the rule this list is under; it simply takes effect in
    another container.  ``KEYUP_TLS_DOMAINS`` and friends stay the seed, and
    ``deploy/edge/entrypoint.sh`` still reads them directly so the edge can start with no
    API to ask (docs/HTTPS.md).
``stt_*``, ``transcribe_*``
    the transcription worker's per-job options and the ingest enqueue policy.
``dedupe_*``, ``reconcile_*``, ``conversation_gap_seconds``, ``gap_minutes``
    ingest and the nightly gap detector.  The reconciliation knobs are read by the file
    watcher on every recording, so widening the clock-skew allowance takes effect on the
    next call rather than the next deploy (docs/RECONCILIATION.md).
``retention_*``
    :func:`app.retention.resolve_policies` — the floor under the per-scope policies.
``health_*``
    :mod:`app.health` — the worker's once-a-minute health checks re-read their thresholds
    on every pass.
``export_*``
    the streamed export cap and the bundle caps/vacuum.
``upload_*``
    :mod:`app.ingest.flow` — the push path's admission control, so an operator can pause a
    running migration or widen the caps without a restart.
``arbiter_*`` / ``best_source_enabled``
    :mod:`app.arbiter` — the standby promotion job, which re-reads them on every pass, so
    turning arbitration on or widening its windows takes effect on the next 30-second tick
    rather than on the next deploy — and :mod:`app.api.call_upload`'s best-source
    comparison, which reads the switch on the upload path.  The arbiter's switch defaults
    **off** (docs/RECORDERS.md).
``shared_agency_code``
    ``keyup mark-shared``'s default agency for the shared dispatch channels, read when the
    command runs, so the deployment's dispatch-center code lives in its settings rather
    than in code.

``export_broadcastify_*`` / ``export_openmhz_*`` / ``broadcastify_*`` / ``openmhz_*``
    :mod:`app.exporters` — the outbound exporters, all of it: the three enable switches
    (Broadcastify Calls, OpenMHz and the Broadcastify **Feed**, which is a live Icecast
    stream rather than an upload queue and carries the mount credentials of its own that
    ``broadcastify_feed_*`` names), the
    credentials and endpoints, the rate knobs, and the selection (which talkgroups, which
    systems, encrypted or not, a minimum duration).  These are switches on somebody else's
    service, so they are deliberately *runtime* rather than environment: flipping one takes
    effect on the exporter worker's next cycle, and flipping it back stops the uploads
    without a deploy.  Both enable switches default to **false** (docs/EXPORTERS.md).

    The environment keeps its role as the **seed**: an unset row falls through to the
    ``BROADCASTIFY_*`` / ``OPENMHZ_*`` environment values exactly as before, and a row that
    is set wins — the same precedence every other key here has, and the same one the STT
    backends use.  Clearing a field in the Admin page deletes the row rather than storing
    an empty string, so the environment value comes back.

``bcfy_*``
    :mod:`app.bcfy_stats` — the Broadcastify **feed owner** poller: the credentials, the feed
    id, the two switches and the two intervals.  Runtime rather than environment for the same
    reason the exporters are: they are switches on somebody else's service, and turning the
    poller off must not need a deploy.  Both switches default to **false**
    (docs/BCFY_STATS.md).

A key that would need a restart to take effect (``ingest_watch``, the database URL, the
secret key) is not settable here on purpose: a switch that does nothing is worse than no
switch.  ``tests/test_runtime_settings_spec.py`` holds the other half of that rule: every
key here must be a field of the object this module returns, or it is a switch the Admin
page renders and saves and nothing ever reads.

Where a key is *edited* is a UI question, not a backend one, but the split is worth knowing
when reading the groups below.  Admin → Settings is the generic form over this spec and
renders every group except three that have a home of their own: ``Export`` belongs to
Admin → **Exports**, ``Feed stats`` to the **Feed Stats** page, and ``Audio`` to Admin →
**Audio**, each beside the status and the actions that knob acts on
(``DEDICATED_GROUPS`` in ``frontend/src/pages/Admin.tsx``).

Two things that used to be on that list are no longer settings *at all*, because the right
answer turned out to be removing the restart rather than the switch.  ``AUDIO_ROOT`` and
``STT_BACKENDS`` now have tables of their own — ``ingest_sources`` and ``stt_backends`` —
with their own CRUD (:mod:`app.api.admin_config`), their own generation counters in this
same ``settings`` table (``ingest_config_version``, ``stt_config_version``) and their own
reload paths in the watcher and the transcription worker.  A key/value override could never
have expressed either of them: one is a *list* of directories and the other a list of
objects, and both need the process that acts on them to notice a change.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Setting

log = logging.getLogger("app.runtime_settings")

#: How long one merged :class:`Settings` is reused before the table is read again.
#: Short enough that an operator's change lands "immediately" and long enough that a burst
#: of requests costs one query.
CACHE_TTL_S = 5.0

#: ``(key, kind, group, label, description)`` — the whitelist the Admin UI may write and
#: the only keys :func:`effective_settings` will layer.  ``kind`` is how the value is
#: coerced from the string the UI sends.
SETTINGS_SPEC: list[tuple[str, str, str, str, str]] = [
    (
        "transcribe_default",
        "bool",
        "Transcription",
        "Transcribe new talkgroups",
        "When ingest first sees a talkgroup with no policy of its own, this decides whether its calls are queued for transcription. Turn off to leave new talkgroups silent until you opt them in; talkgroups that already have a policy keep it.",
    ),
    (
        "transcribe_min_seconds",
        "float",
        "Transcription",
        "Minimum duration",
        "Recordings shorter than this (seconds) are treated as squelch tails or dead keys and never queued, sparing the engine empty clips. Raise to drop more brief blips; lower to catch very short traffic.",
    ),
    (
        "transcribe_max_attempts",
        "int",
        "Transcription",
        "Max attempts",
        "How many times a transcription job is retried after a backend error or timeout before it is parked as 'failed' and left for a manual requeue.",
    ),
    (
        "stt_vad",
        "bool",
        "Transcription",
        "Voice activity detection",
        "Voice-activity detection: asks the engine to skip non-speech so silence and static aren't turned into hallucinated words (14% of calls are under a second). Effective on the local faster-whisper path; the OVMS NPU/GPU backends have no VAD and ignore it.",
    ),
    (
        "stt_language",
        "string",
        "Transcription",
        "Language",
        "Language hint (an ISO code such as `en`) sent to the engine so it doesn't waste time detecting language or drift into another. Blank lets the engine auto-detect.",
    ),
    (
        "stt_prompt_extra",
        "text",
        "Transcription",
        "Extra prompt",
        "Free text appended to the auto-built domain prompt (talkgroup aliases, unit numbers, city and street names) to bias decoding toward local vocabulary — agency names, callsigns, streets. Not used by the OVMS backends, which accept no prompt.",
    ),
    (
        "stt_places",
        "text",
        "Transcription",
        "Place names",
        "Comma-separated place names heard on this system — towns, campuses, landmarks — added to the domain prompt and the hotword list after the talkgroup and unit aliases, so the engine spells them the local way. Blank leaves places out. Not used by the OVMS backends, which accept no prompt.",
    ),
    (
        "stt_hallucination_patterns",
        "text",
        "Transcription",
        "Hallucination patterns",
        "Extra regular expressions, one per line, that mark a transcript as a hallucination: matching text is labeled `noise` and the call keeps no text. Merged with the built-in caption-credit patterns (`closed caption`, `captioning provided by`, `amara.org`, `www.`, `copyright`) — add a new one when whisper invents a credit line this deployment has not seen. Matching is case-insensitive and anywhere in the text, so keep them specific; an entry that is not a valid regex is logged and skipped.",
    ),
    (
        "stt_word_timestamps",
        "bool",
        "Transcription",
        "Word timestamps",
        "Ask the engine for per-word start/end times (for word-level transcript highlighting) where it supports them. Slightly more compute; the `ovms` backend must have word timestamps enabled server-side or it errors.",
    ),
    (
        "stt_normalize",
        "bool",
        "Transcription",
        "Normalize loudness",
        "Even out loudness before transcription so loud alert tones and soft talkers land at a usable level, improving accuracy on quiet speech. Applied only to the copy sent to the engine — the stored MP3 is never changed.",
    ),
    (
        "stt_normalize_always",
        "bool",
        "Transcription",
        "Normalize twice",
        "Apply the transcription loudness filter even when the call already has a processed copy (Audio tab). Off by default: the processed copy is already trimmed, gated and normalized, and running `speechnorm` over it again expands what is already even and lifts the noise floor the gate just pushed down. Turn it on only to compare the two.",
    ),
    (
        "stt_normalize_filter",
        "string",
        "Transcription",
        "Normalize filter",
        "The exact ffmpeg `-af` filter used for the loudness pass above. Blank uses the built-in default for the installed ffmpeg (`speechnorm`, else `dynaudnorm`, else `loudnorm`). Only set a custom chain if you know ffmpeg filter syntax.",
    ),
    (
        "dedupe_window_seconds",
        "float",
        "Ingest",
        "Dedupe window",
        "When two recordings look like the same transmission, their start times must fall within this many seconds to be matched as duplicates. Widen if recorder clocks drift apart; narrow to avoid collapsing distinct back-to-back calls.",
    ),
    (
        "dedupe_duration_tolerance",
        "float",
        "Ingest",
        "Dedupe duration tolerance",
        "How much two copies' durations may differ and still count as one call, as a fraction (0.10 = 10%). Works together with the dedupe window.",
    ),
    (
        "reconcile_enabled",
        "bool",
        "Ingest",
        "Reconcile recorders",
        "When two recorders capture the same transmission, store both copies and show the better one (longer, complete audio) as the call; the other is kept as a hidden duplicate. Turn off to go back to first copy wins: a second copy is rejected as a duplicate and its audio is not kept.",
    ),
    (
        "reconcile_clock_skew_seconds",
        "float",
        "Ingest",
        "Reconcile clock skew",
        "How far two recorders' clocks may disagree before their call intervals are lined up for comparison. Read on every recording, so a change takes effect on the next call, not the next deploy.",
    ),
    (
        "reconcile_min_overlap",
        "float",
        "Ingest",
        "Reconcile overlap",
        "The fraction of the shorter recording that must overlap the other for the two to be judged one call (0.5 = half). Higher is stricter about what counts as the same transmission.",
    ),
    (
        "conversation_gap_seconds",
        "int",
        "Ingest",
        "Conversation gap",
        "Calls on one talkgroup closer together than this many seconds are grouped into a single conversation thread. Larger values stitch more back-and-forth into one exchange.",
    ),
    (
        "gap_minutes",
        "int",
        "Ingest",
        "Recorder gap",
        "Silence on a monitored talkgroup longer than this many minutes is flagged by the nightly gap detector as a possible recorder outage rather than a quiet stretch.",
    ),
    (
        "retention_keep_days",
        "int",
        "Retention",
        "Keep days",
        "Delete calls (row and audio) older than this many days — the floor beneath any per-scope retention policy. 0 = keep forever.",
    ),
    (
        "retention_keep_audio_days",
        "int",
        "Retention",
        "Keep audio days",
        "Purge just the MP3 after this many days while keeping the call's metadata and transcript. 0 = never purge audio on age alone.",
    ),
    (
        "retention_keep_transcripts",
        "bool",
        "Retention",
        "Keep transcripts",
        "When audio is purged by age, keep the transcript and metadata — a searchable record with no playback — instead of deleting the whole call.",
    ),
    (
        "retention_archive_path",
        "string",
        "Retention",
        "Archive path",
        "If set, purged MP3s are moved to this filesystem path instead of being deleted, so they can be cold-stored. Blank deletes them outright.",
    ),
    (
        "radio_events_keep_days",
        "int",
        "Retention",
        "Keep radio events days",
        "How long to keep radio presence and affiliation events — data about identifiable people's movements — before purging. Kept short by default for privacy; 0 = keep forever.",
    ),
    (
        "activity_retention_days",
        "int",
        "Retention",
        "Keep activity log days",
        "How long to keep the operational activity log — the per-call ingest, transcription and export lines behind the Activity page. It is the archive's fastest-growing table by row count (3.3 M rows and 1.2 GB after two weeks) and nothing else reads it, so this is a diagnostics window, not a record. 0 = keep forever, which on this deployment means unbounded growth.",
    ),
    (
        "disk_warn_free_percent",
        "float",
        "Retention",
        "Disk warning below %",
        "Raise a warning (Activity, alert rules of kind disk_low, a banner on the status panel) when free space on the audio disk falls under this percentage — or under the GB figure beside it, whichever trips first.",
    ),
    (
        "disk_warn_free_gb",
        "float",
        "Retention",
        "Disk warning below GB",
        "The same warning as an absolute floor in gigabytes. Either threshold trips it; it fires once per crossing and re-arms when free space is back above both.",
    ),
    (
        "disk_critical_free_percent",
        "float",
        "Retention",
        "Disk critical below %",
        "A second, error-level event under this percentage. Uploads are refused outright (507) near 5 GB free, so this should trip well before that.",
    ),
    (
        "disk_critical_free_gb",
        "float",
        "Retention",
        "Disk critical below GB",
        "The critical threshold in gigabytes. Keep it above the 5 GB upload floor so the error arrives while there is still time to act.",
    ),
    (
        "health_recorder_silent_minutes",
        "int",
        "Health",
        "Recorder silent after (minutes)",
        "Raise a health alert when a hot recorder has sent no status report for this many minutes. trunkd reports every few seconds, so a few minutes rides out a restart. Warm and cold recorders (spares, test boxes) are never alerted on. Fires once per outage and clears when the recorder is back. 0 = off.",
    ),
    (
        "health_ingest_lag_minutes",
        "int",
        "Health",
        "No new calls after (minutes)",
        "Raise a health alert when no call at all has been recorded for this many minutes. The longest real quiet spell on this system has been about 14 minutes in the small hours, so 20 stays silent on a quiet night. 0 = off.",
    ),
    (
        "health_stt_backlog_minutes",
        "int",
        "Health",
        "Transcription stalled after (minutes)",
        "Raise a health alert when the oldest live transcription job has been waiting this long — the speech engine is down or cannot keep up. Backlog-priority work (migrations, repairs) is not counted. 0 = off.",
    ),
    (
        "health_backup_max_age_hours",
        "int",
        "Health",
        "Backup overdue after (hours)",
        "Raise a health alert when the newest database backup is older than this. Only checked when the backup directory is mounted into the worker (docs/OPERATIONS.md §7). 0 = off.",
    ),
    (
        "auth_require_read",
        "bool",
        "Access",
        "Require sign-in to read",
        "Require a sign-in even to read the archive, closing it to anonymous viewers. Takes effect on the next request. Uploads and admin actions always require auth regardless of this.",
    ),
    (
        "auth_session_days",
        "int",
        "Access",
        "Sign-in lasts",
        "The **absolute** life of a sign-in, in days: however active you are, a session is over this many days after it was created and the next request asks for the password again. 14 is the default. It is read when the session is created, so a change applies to the next sign-in rather than to sessions already open. The shorter of this and the idle window below is what actually signs somebody out.",
    ),
    (
        "auth_idle_hours",
        "int",
        "Access",
        "Sign out after idle",
        "The **sliding** half of the same deadline: a session with no request for this many hours is deleted on sight, whatever the absolute deadline says. Unlike the absolute one this is read on every request, so shortening it signs idle sessions out immediately. Set it as long as the absolute deadline to leave only the absolute one in play.",
    ),
    (
        "tls_enabled",
        "bool",
        "HTTPS",
        "Serve HTTPS",
        "The master switch for the archive's own certificates (docs/HTTPS.md). On, the edge asks Let's Encrypt for a certificate covering the domains below, by the challenge chosen under 'ACME challenge': DNS-01 through Cloudflare (no inbound port needed; the names may resolve to a LAN address) or HTTP-01 (the names resolve publicly to this box, with ports 80 and 443 open to the internet). Off, the edge is a plain HTTP pass-through, exactly as it was before this existed. Turning it on needs the domains and the contact address filled in, and for DNS-01 the token too; without them the edge stays on plain HTTP and says so.",
    ),
    (
        "tls_domains",
        "string",
        "HTTPS",
        "Domains",
        "The hostnames the certificate covers, space separated. For DNS-01 each needs an A record pointing at this box, with Cloudflare's orange-cloud proxy **off**, in a zone the token below can edit; DNS-01 does not care where the name points, so a private address is fine. For HTTP-01 each must resolve publicly to this box. Blank is the off switch however the master switch is set.",
    ),
    (
        "tls_acme_email",
        "string",
        "HTTPS",
        "ACME contact address",
        "The contact address on the Let's Encrypt account. Expiry and policy notices go here. Required with HTTPS on.",
    ),
    (
        "tls_cloudflare_api_token",
        "secret",
        "HTTPS",
        "Cloudflare API token",
        "The token DNS-01 writes its TXT record with; not needed for HTTP-01. Create it in the Cloudflare dashboard as a **custom token** with exactly `Zone -> DNS -> Edit` on the one zone holding these names — not the Global API Key. Write-only: stored, never shown again, never logged, and leaving the field blank on save keeps the one you already have. 'Test Cloudflare token' proves it before an issuance is attempted.",
    ),
    (
        "tls_acme_ca",
        "enum",
        "HTTPS",
        "Certificate authority",
        "`production` is the real Let's Encrypt. `staging` is their rehearsal directory: no rate limits, and an untrusted certificate, so browsers complain — which is the expected result and the reason to use it first. Production rate limits are per-week and unforgiving, so prove the token and the DNS on staging before switching here.",
    ),
    (
        "tls_redirect_http",
        "bool",
        "HTTPS",
        "Redirect plain HTTP to HTTPS",
        "With HTTPS on, what plain `:80` does. On (the default): browsers are sent to https with a 308. Off: `:80` serves the whole site in the clear **as well as** https, which is what you want while something on the LAN cannot follow a redirect. Either way `/health` and `/api/*` are answered over plain HTTP and never redirected — recorders push by IP address, and no certificate can cover an IP.",
    ),
    (
        "tls_challenge",
        "enum",
        "HTTPS",
        "ACME challenge",
        "How Let's Encrypt checks that this box owns the names. `dns`: the DNS-01 challenge, answered by writing a TXT record through Cloudflare with the token above — works for names that resolve only on the LAN, and needs no inbound port. `http`: Caddy's own HTTP-01 / TLS-ALPN-01 — no token, but every name must resolve publicly to this box and ports 80 and 443 must reach the edge from the internet (`WEB_PORT=80`, `WEB_TLS_PORT=443`). `auto` (the default): `dns` when a token is stored, `http` otherwise.",
    ),
    (
        "export_max_rows",
        "int",
        "API",
        "Streamed export row cap",
        "Cap on the number of rows a streamed CSV/JSON export may return, protecting the API from an unbounded query. 0 = uncapped.",
    ),
    (
        "export_max_calls",
        "int",
        "API",
        "Bundle call cap",
        "The most calls a single `POST /exports` audio bundle may contain, so one request can't assemble an enormous download.",
    ),
    (
        "export_max_bytes",
        "int",
        "API",
        "Bundle byte cap",
        "The most bytes of audio a single export bundle may carry, so one download can't exhaust disk.",
    ),
    (
        "export_keep_days",
        "int",
        "API",
        "Keep bundles for",
        "The nightly vacuum deletes finished export bundles older than this many days, reclaiming disk. 0 = keep them indefinitely.",
    ),
    (
        "upload_pause_backlog",
        "bool",
        "Upload",
        "Pause backlog ingestion",
        "Refuse `priority=backlog` and `bulk` uploads (the archive migration) while letting live pushes through. Flip on to instantly pause a running backfill without stopping live recording.",
    ),
    (
        "upload_max_queue_live",
        "int",
        "Upload",
        "Live queue cap",
        "If more than this many live-band transcription jobs are already waiting, even a live push is refused (429) so the recorder spools and retries instead of piling on.",
    ),
    (
        "upload_max_queue_backlog",
        "int",
        "Upload",
        "Backlog queue cap",
        "The same ceiling for backlog/bulk jobs: above it, backlog uploads are refused so live traffic keeps priority over the migration.",
    ),
    (
        "upload_backlog_max_eta_seconds",
        "float",
        "Upload",
        "Backlog ETA ceiling",
        "Refuse backlog uploads when the transcription queue would need longer than this many seconds to drain — keeps the backfill from outrunning the engines.",
    ),
    (
        "upload_backlog_max_concurrency",
        "int",
        "Upload",
        "In-flight backlog uploads",
        "How many backlog uploads one API process will accept in flight at once, capping the parallelism the migration tool can push.",
    ),
    (
        "export_broadcastify_enabled",
        "bool",
        "Export",
        "Upload calls to Broadcastify",
        "Re-publish this system's live calls to Broadcastify Calls. **Off by default, and it must stay off while another uploader (sdrtrunk, trunk-recorder) is feeding the same system** — two uploaders means every call is posted twice. Turning it on starts the clock: only calls that arrive *after* the switch is flipped are ever uploaded, and only live-band calls, so a running archive migration is never re-published. Calls picked up by the file watcher rather than pushed by a recorder carry no priority and are never exported either. Needs BROADCASTIFY_API_KEY and BROADCASTIFY_SYSTEM_ID in the environment; without them the switch does nothing.",
    ),
    (
        "export_openmhz_enabled",
        "bool",
        "Export",
        "Upload calls to OpenMHz",
        "Re-publish this system's live calls to OpenMHz. **Off by default**, and subject to the same rule as Broadcastify above: run exactly one uploader per system, or every call appears twice. Only calls recorded after the switch is turned on, and only live-band calls, are uploaded — the archive backfill is never sent, and neither is anything the file watcher picked up off disk rather than a recorder pushing it. Needs OPENMHZ_API_KEY and OPENMHZ_SHORT_NAME in the environment.",
    ),
    (
        "arbiter_enabled",
        "bool",
        "Recorders",
        "Promote a standby recorder automatically",
        "When every *hot* recorder covering a site stops reporting, let keyup enable a *warm* standby's tuners for that site — and disable them again once the hot side has been healthy for the recovery window below. **Off by default**: with a single recorder there is nothing to arbitrate, and a job that switches receivers on and off is not something to leave running unnoticed. Only the per-tuner `enabled` flag is ever touched, only on recorders you have marked `warm`, and never on a `cold` one; a warm recorder *you* enabled by hand is never switched off by this.",
    ),
    (
        "arbiter_stale_seconds",
        "int",
        "Recorders",
        "Recorder considered down after",
        "How many seconds without a status report before the arbiter treats a recorder as down. trunkd reports about every 15 seconds, so 60 tolerates three missed pushes — a reboot, a brief network drop — without handing the site to the spare. Lower it to fail over faster at the cost of flapping on a lossy link; raise it if your recorders report less often than every 20 seconds.",
    ),
    (
        "arbiter_recovery_seconds",
        "int",
        "Recorders",
        "Stand the spare down after",
        "How long *every* hot recorder for a site must report continuously before the promoted standby is switched off again. This is the hysteresis: a recorder that comes back and immediately drops out again would otherwise hand the site back and forth every half minute, and each handover loses the calls in flight. Five minutes is deliberately much longer than the down threshold. The clock is kept in the running job, so a keyup restart makes the spare wait the full window again rather than standing down early.",
    ),
    (
        "best_source_enabled",
        "bool",
        "Recorders",
        "Keep the better receiver's audio",
        "When two receivers push the same transmission and both say how well they heard it (`sourceQuality`: voice frames, bad frames, level), keep the better recording instead of always keeping whichever arrived first. The replacement has to win by a real margin — 5% better on voice frames less five times the bad ones — so two receivers that heard a call equally well do not swap the audio back and forth. A second push from the *same* recorder is a retransmit and never replaces anything, and a call whose recorders sent no quality figures behaves exactly as it always did: the duplicate is rejected.",
    ),
    (
        "shared_agency_code",
        "string",
        "Directory",
        "Shared channels' agency",
        "The agency code `keyup mark-shared` gives the shared dispatch and interop channels — the ones every department keys up on and none owns — when they have no agency yet. Usually the dispatch center's own code, which makes those channels answer `?agency=` for the dispatch center rather than for nobody. Blank flags them `shared` and leaves their agency alone. It must be a code in the agency list; an unknown one is reported and not applied.",
    ),
    (
        "webhook_url",
        "string",
        "Webhooks",
        "Webhook URL",
        "Where to POST an event when something happens that a consumer can act on — a CAD integration matching a new call the moment it lands, rather than polling. **Blank turns the whole feature off**, which is how it ships: nothing is queued and nothing is sent. Each delivery is a JSON POST with an `X-Keyup-Event` header; a 2xx is success, anything else is retried with a widening backoff and then parked. Deliveries never affect ingest, transcription or playback — the queue is written in the same transaction as the work and drained separately.",
    ),
    (
        "webhook_secret",
        "secret",
        "Webhooks",
        "Webhook signing secret",
        "Shared secret used to sign each delivery: `X-Keyup-Signature: sha256=<hex>` is HMAC-SHA256 of the exact request body, which lets the receiver prove the call came from this deployment and not from whoever found the URL. Write-only — it is stored but never shown again, and leaving the field blank on save keeps the secret you already have. Blank *sends* no signature at all, which is only sensible on a trusted LAN.",
    ),
    (
        "webhook_events",
        "string",
        "Webhooks",
        "Webhook events",
        "Which events to send, comma separated: `call.stored` (a recording is in the archive and playable — sent before transcription, so a consumer that wants the audio does not wait on an engine), `call.transcribed` (text now exists for it), `link.created` and `link.deleted` (somebody confirmed or removed a link between a call and an outside record). Empty sends nothing, even with a URL set.",
    ),
    (
        "webhook_call_scope",
        "enum",
        "Webhooks",
        "Send call events for",
        "Which calls are worth a delivery. `all` sends `call.stored`/`call.transcribed` for every call, which is what this shipped as — and on a deployment replaying an archive that is hundreds of thousands of events a day, nearly all of them about 2016. `live` sends only calls that arrived as live traffic (`ingest_priority` = `live`), so a backlog migration is silent. `linked` sends only calls that already carry an external link — the consumer has said this call matters. `live+linked` sends a call that is either. **Link events are never filtered by this**: `link.created` and `link.deleted` are somebody's deliberate act on one particular call and always go out.",
    ),
    (
        "webhook_max_attempts",
        "int",
        "Webhooks",
        "Max delivery attempts",
        "How many times one event is retried against the receiver — with a widening backoff between tries — before it is parked as 'failed' and left alone. A receiver that has refused five times is refusing for a reason a sixth try will not fix, and a queue that retries for ever is a queue that never drains. Parked deliveries stay in the admin list and can be retried by hand.",
    ),
    # ---- Audio (§5.5) ----------------------------------------------------------------
    # The whole processing chain is runtime-settable because the right value for every one
    # of these is found by listening to the archive, not by reasoning about it.
    (
        "audio_process_enabled",
        "bool",
        "Audio",
        "Process stored audio",
        "Master switch for the audio-processing stage. When on, every new call gets a processed copy stored beside the original — trimmed, denoised, loudness-normalized and peak-limited — and that copy is what the player, the exporters and the transcriber use. The original recording is never modified and is always still available (`?raw=1` on the audio URL). Turning this off stops new work; copies already made keep being served.",
    ),
    (
        "audio_trim_silence",
        "bool",
        "Audio",
        "Trim dead air",
        "Cut the silence off the front and back of each call. A third of live calls carry more than half a second of dead air before anybody speaks, which is half a second of nothing every time somebody clicks play. Interior pauses are never touched.",
    ),
    (
        "audio_trim_threshold_db",
        "float",
        "Audio",
        "Dead-air threshold (dBFS)",
        "Level below which audio counts as dead air for the trim. -40 dBFS is comfortably under the quietest real speech in this archive (the tenth percentile call averages -42 dBFS overall, but its speech peaks far above this). Raise it toward -30 to trim more aggressively; lower it toward -50 if the start of words is being clipped.",
    ),
    (
        "audio_trim_keep_ms",
        "int",
        "Audio",
        "Padding kept (ms)",
        "How much of the silence to leave on each end so a call does not begin on the first syllable. 150 ms is about one breath; below ~50 ms speech sounds truncated even when nothing was actually cut.",
    ),
    (
        "audio_target_lufs",
        "float",
        "Audio",
        "Loudness target (LUFS)",
        "Where every call is normalized to, so one is as loud as the next. -16 LUFS is the streaming-speech convention and leaves headroom for the limiter. Clips of at least three seconds get a proper two-pass `loudnorm`; anything shorter — the median call is 2.2 s — gets a measured gain instead, because EBU R128 cannot integrate a loudness from a second and a half of audio.",
    ),
    (
        "audio_true_peak_db",
        "float",
        "Audio",
        "Peak ceiling (dBFS)",
        "The limiter every processed file ends on. Nothing in the archive is allowed above this, whichever loudness branch ran, so a page tone cannot clip a listener's speakers. The number is a *sample* ceiling, but what a listener hears is a 16 kbps MP3 at 8 kHz — and a lossy decoder rebuilds a waveform that overshoots the one it encoded, by 1.5-2 dB on this material, plus whatever the in-app player's own make-up gain adds. -4 dBFS is that headroom: it is what keeps the decoded file under full scale rather than exactly on it. Raising it toward -1 makes the archive louder and brings the clipping back; lowering it only costs volume.",
    ),
    (
        "audio_max_gain_db",
        "float",
        "Audio",
        "Maximum gain (dB)",
        "The most the short-clip branch may lift (or cut) a call. The measured spread across live traffic is about 20 dB, so 30 dB covers everything real while stopping a clip that measured as near-silence from being amplified into a wall of hiss.",
    ),
    (
        "audio_gate_db",
        "float",
        "Audio",
        "Noise gate (dBFS)",
        "Audio under this is treated as silence, so a 20 dB lift on a quiet talker raises his words and not the hiss between them. Set it to -100 to disable the gate entirely; raise it toward -40 if background noise still comes up with the speech, and lower it if quiet tails of words are being chopped.",
    ),
    (
        "audio_silent_below_db",
        "float",
        "Audio",
        "Silent below (dBFS)",
        "A call whose level is still under this after trimming is marked silent — some 0.5 s 'calls' are genuinely empty (-80 dBFS). It is stored and processed either way; the list simply shows a muted icon so nobody wonders why clicking play does nothing.",
    ),
    (
        "audio_speech_floor_db",
        "float",
        "Audio",
        "Speech floor (dBFS)",
        "The level a stretch of the *processed* copy has to clear to count as somebody talking. Paired with **Minimum speech** below, this is what separates a call from a mic key-up: the silent floor asks whether the whole clip is quiet, and a kerchunk answers no — 0.35 s of nothing and one 30 ms click averages about -50 dBFS. -40 dBFS is the same floor the trim already uses to find the first word.",
    ),
    (
        "audio_min_speech_seconds",
        "float",
        "Audio",
        "Minimum speech (seconds)",
        "How much audio above the speech floor a call needs before it counts as speech. Under this it is marked `content = none`: not transcribed, not exported, and — with **Discard silent calls** on — its recording is dropped after the retention grace while the row, the talkgroup and the radio stay. 0.25 s is well under the shortest real word and well over the click a key-up makes.",
    ),
    (
        "audio_static_flatness",
        "float",
        "Audio",
        "Static flatness",
        "How flat a 32 ms frame's spectrum (300-3400 Hz) has to be before it counts as noise rather than a voice. Spectral flatness is 1.0 for white noise and near zero for a vowel, which puts its energy in formants and leaves deep nulls between them. 0.30 is the measured separation: the three known static calls sit at 0.37-0.48 median and known-good speech at 0.04-0.07. Raise it if real speech is being called static; lower it to catch more.",
    ),
    (
        "audio_static_frame_share",
        "float",
        "Audio",
        "Static frame share",
        "…and what fraction of a call's live frames have to be that flat before `content = static`. Paired with a level test — a clip louder than -35 dBFS is somebody talking whatever its spectrum looks like, and that half of the rule is the one carrying it. At 0.70 the review corpus flagged 3 of 3 static calls and 0 of 36 others, the closest miss being 0.66. A static call is handled exactly like a no-speech one: never transcribed, never exported, discarded with the rest.",
    ),
    (
        "audio_discard_no_speech",
        "bool",
        "Audio",
        "Discard no-speech calls",
        "Set `audio_policy = discard` on a call with no speech in it, so the ordinary discard pass removes the recording after its grace period. The call itself is never deleted — the archive still knows the radio keyed up on that talkgroup at that second. Turn it off to keep every byte.",
    ),
    (
        "audio_bandpass_low_hz",
        "float",
        "Audio",
        "Speech band low edge (Hz)",
        "The high-pass in front of the denoiser. A P25 vocoder reconstructs roughly 300-3800 Hz and nothing outside it, so the rumble under 300 Hz — handling thumps, engine noise, mains hum — is energy the codec never carried and cutting it costs no speech at all. It is also what stops the denoiser spending its budget on noise that a two-pole filter removes outright. 0 disables this edge. Measured on 20 production calls, the band-pass plus `afftdn nf=-35 nr=18` gains **+17.2 dB** of signal-to-noise against +0.3 dB for the band-pass with no denoiser behind it, and leaves the 2-8 Hz speech envelope untouched.",
    ),
    (
        "audio_bandpass_high_hz",
        "float",
        "Audio",
        "Speech band high edge (Hz)",
        "…and the low-pass above it. This is a guard rail, not a cut: `lowpass` is a two-pole filter, so its corner is where it is already 3 dB down and about 2 dB down 400 Hz below that, and a P25 vocoder carries consonant energy to roughly 3.7 kHz. At the 3400 Hz this shipped with, the edge took **3.35 dB** out of the 2.5-3.8 kHz band — the top of the voice, which is what an operator hears as muffled. 3800 is above everything an 8 kHz recording can hold (measured cost: 0.01 dB) and is still a real edge for a higher-rate source. 0 disables this edge. Both edges are skipped entirely on a talkgroup that has an audio profile — a profile replaces the whole noise section and declares its own band edges if it wants them.",
    ),
    (
        "audio_noise_reduction",
        "enum",
        "Audio",
        "Noise reduction",
        "Which denoiser runs before the gain: `afftdn` (spectral subtraction — the default, cheap and safe on vocoder audio, and with the band-pass above it the chain this system is measured on), `arnndn` (an RNNoise model, needs a model file, and none is installed here), `anlmdn` (non-local means) or `off`. **`anlmdn` is not recommended**: measured on this system's vocoder audio it *raised* the noise floor 8-9 dB (radio 5233201, Δsnr -6.05) and did nothing at all on the next radio tested (+0.35). It is kept selectable because it is cheap to try on a channel with a different problem, but for hiss the answer here is the band-pass plus `afftdn` at `nf=-35 nr=18`. `arnndn` falls back to `afftdn` with a logged warning when no model file is configured.",
    ),
    (
        "audio_noise_floor_db",
        "float",
        "Audio",
        "Noise floor (dB)",
        "Where `afftdn` assumes the noise **is**, in dB — not how hard it pushes, which is the `nr` below. Ignored by the other denoisers. Setting it too loud does not buy more noise reduction; it buys the same reduction applied to quiet speech. Measured on 20 production calls, every value from -30 to -45 removes the same ~18 dB of noise, while the 2.5-3.8 kHz speech energy lost runs -2.17 dB at -30, **-1.06 at -35**, -0.50 at -40 and -0.25 at -45. The floors actually measured on this system run -20 to -76 dBFS, median **-63**, so the -25 this shipped with and the -30 that replaced it both claimed far more noise than exists, and the difference came out of the fricatives — the 'filtering is cutting the upper range of voices' complaint. -35 is the value that keeps the whole noise reduction and holds the high band inside 1 dB.",
    ),
    (
        "audio_noise_reduction_nr",
        "float",
        "Audio",
        "Noise reduction (dB)",
        "`afftdn`'s `nr` — how many dB of the assumed floor it actually takes out. This is the aggression control; `nf` above only says where the floor is. ffmpeg's own default is 12, and 18 is what measured best here: with the band-pass ahead of it, `nf=-35 nr=18` gains +17.2 dB of signal-to-noise on production audio against +4.0 dB for the old `nf=-25` with no `nr`, and — once `nf` is honest — it costs only 1.06 dB of the 2.5-3.8 kHz speech band. Set it to 0 to leave `nr` off the command line entirely (ffmpeg then uses 12). Ignored by the other denoisers.",
    ),
    (
        "audio_rnnoise_model",
        "string",
        "Audio",
        "RNNoise model path",
        "Path inside the container to an RNNoise model file for `arnndn`. Blank means none is installed, and selecting `arnndn` will quietly use `afftdn` instead — this field is what makes that a configuration choice rather than a mystery.",
    ),
    (
        "audio_tone_detect",
        "bool",
        "Audio",
        "Detect tones",
        "Find sustained pure tones — two-tone pages, alert tones, warbles — and record them on the call. This is what sets the tone and page badges and the `tone_detected=` / `alert=` filters. It is a few FFTs over audio already decoded for the level measurement, so it costs almost nothing.",
    ),
    (
        "audio_tone_handling",
        "enum",
        "Audio",
        "What to do with tones",
        "`attenuate` turns detected tones down in the processed copy so a page does not blast a listener who has the volume up for the dispatcher who follows it (the recommended setting); `remove` cuts them out entirely, which shortens the processed file and is recorded in its metadata; `keep` leaves the audio alone and only records where the tones were. The original recording always keeps its tones either way.",
    ),
    (
        "audio_tone_attenuate_db",
        "float",
        "Audio",
        "Tone attenuation (dB)",
        "How far `attenuate` pulls a **single held tone** down — the console attention tone that leads a dispatch (call 2396065 is 1.95 s of 993 Hz at -10.5 dBFS in front of 17 s of speech). It carries no information and is simply loud, so it can go a long way down: 30 dB leaves a marker where it was.",
    ),
    (
        "audio_page_tone_attenuate_db",
        "float",
        "Audio",
        "Page tone attenuation (dB)",
        "How far `attenuate` pulls a **page** down — the two-tone, warble and alert patterns that tone a station out. Shallower than the setting above on purpose: a page is information a listener wants to hear under the dispatch that follows it, not something to be got rid of. 18 dB takes it off the top of the archive and leaves it plainly audible.",
    ),
    (
        "audio_process_max_attempts",
        "int",
        "Audio",
        "Max attempts",
        "How many times one call is retried after an ffmpeg error before the job is parked as failed. A parked call simply plays back raw, which is what happened before this stage existed — nothing is lost.",
    ),
    (
        "audio_reprocess_version",
        "int",
        "Audio",
        "Reprocess version",
        "Bump this to make every call reprocess lazily: any call whose stored metadata names an older version is treated as stale by the backfill. Use it after changing the settings above when you want the whole archive to match, rather than only the calls that arrive from now on.",
    ),
    (
        "broadcastify_environment",
        "enum",
        "Export",
        "Broadcastify environment",
        '**production** or **development**. Broadcastify issues the two sets of credentials separately and each endpoint refuses the other\'s: "Use the development endpoint with development credentials only. Production credentials are issued separately and must be used against the production endpoint." This one field picks the URL, the API key *and* the system id together, so the two can never be crossed. Production is the default and the one this deployment publishes with; development uploads to the calls-dev sandbox and shows on `broadcastify.com/calls-dev/status/` instead.',
    ),
    (
        "broadcastify_api_key",
        "secret",
        "Export",
        "Broadcastify API key",
        "The per-system API key from the Broadcastify **Calls** dashboard — not the live-feed mount password, which is a different product and is refused by `call-upload`. Write-only: it is stored but never shown again, and leaving the field blank on save keeps the key you already have. Blank keeps the exporter dormant however the switch is set. Falls back to `BROADCASTIFY_API_KEY` in the environment while no value is set here.",
    ),
    (
        "broadcastify_system_id",
        "string",
        "Export",
        "Broadcastify system id",
        "Broadcastify's own numeric system id for this system, from the same Calls dashboard. Not keyup's `systems.id`. Falls back to `BROADCASTIFY_SYSTEM_ID` while blank.",
    ),
    (
        "broadcastify_slot",
        "string",
        "Export",
        "Broadcastify conventional slot",
        "The predefined frequency-slot id a *conventional* Broadcastify Calls system uses in place of a talkgroup. Leave blank for a trunked system — which this deployment is — and the talkgroup is sent instead. Setting it makes **every** call report that slot.",
    ),
    (
        "broadcastify_url",
        "string",
        "Export",
        "Broadcastify endpoint (production)",
        "The **production** call-registration endpoint, `https://api.broadcastify.com/call-upload`. There is no reason to change it except to point a test at a local stub; the audio then goes to the single-use, time-limited URL that endpoint hands back. Used only while the environment is `production`.",
    ),
    (
        "broadcastify_dev_url",
        "string",
        "Export",
        "Broadcastify endpoint (development)",
        "The **development** call-registration endpoint, `https://api.broadcastify.com/call-upload-dev`, used only while the environment is `development`. Development credentials are issued separately from production ones and work only here; production credentials work only against the production endpoint.",
    ),
    (
        "broadcastify_dev_api_key",
        "secret",
        "Export",
        "Broadcastify development API key",
        "The API key issued for the **development** environment. Kept apart from the production key on purpose: each endpoint refuses the other's credentials, and there is deliberately no fall-through — selecting `development` with this blank leaves the exporter unconfigured rather than quietly sending the production key to a test host. Write-only: stored, never shown again, and a blank field on save keeps the key you already have. Falls back to `BROADCASTIFY_DEV_API_KEY` while unset.",
    ),
    (
        "broadcastify_dev_system_id",
        "string",
        "Export",
        "Broadcastify development system id",
        "The numeric system id that goes with the development API key. Not the production system id, and not keyup's `systems.id`. Falls back to `BROADCASTIFY_DEV_SYSTEM_ID` while blank.",
    ),
    (
        "broadcastify_encoding",
        "enum",
        "Export",
        "Broadcastify encoding",
        'The audio format each call is uploaded in. **m4a** is AAC and is what Broadcastify prefers — "approximately 32 kb/s with an 18 kHz cutoff using libfdk_aac or equivalent"; keyup uses libfdk_aac when the installed ffmpeg has it and falls back to the built-in `aac` encoder otherwise. **mp3** is the accepted alternative, CBR mono at 22050 Hz. The choice drives the encode, the `enc` form field and the upload\'s `Content-Type` (`audio/aac` or `audio/mpeg`) together, so the three cannot disagree.',
    ),
    (
        "broadcastify_mp3_bitrate_kbps",
        "enum",
        "Export",
        "Broadcastify MP3 bitrate",
        "Which of the two MP3 bitrates the spec lists — **32** or **16** kbit/s, both mono 22050 Hz CBR. Ignored entirely while the encoding is `m4a`.",
    ),
    (
        "broadcastify_skew_limit_s",
        "float",
        "Export",
        "Broadcastify skew window",
        "How many seconds after a transmission **ends** Broadcastify Calls still accepts it. Past their budget the answer is `1 REJECTED-CALL-SKEW-TOO-LONG-<secs>`, and what they measure is the moment the registration arrives minus (`ts` + `callDuration`) — the age of the call counted from its end, not from its start. 15 s matches what this deployment observes: every accepted upload landed inside about 14 s. A call already past this (less a 1 s margin for the round trip) is skipped here with the reason `past the Broadcastify skew window`, spending no request and no encode on a refusal that is certain. 0 turns the pre-check off and lets Broadcastify answer for itself. Broadcastify Calls only — neither the Feed nor OpenMHz has a skew rule.",
    ),
    (
        "broadcastify_upload_at",
        "enum",
        "Export",
        "Broadcastify Calls uploaded by",
        "**Who actually POSTs the call to Broadcastify.** `keyup` (the default) is the path this deployment has always used: the recorder pushes the call here, ingest stores it, and the exporter worker registers and uploads it about three seconds after the transmission ended. `recorder` moves the upload to the edge — each trunkd recorder registers the call and PUTs its own MP3 the moment it stops, typically inside a second, which is most of the Broadcastify skew window handed back. The credentials for it are delivered to the recorders over the desired-state poll and only while this is set to `recorder`, the Calls switch is on and the key and system id are set. Either way **keyup stays the system of record**: the recorder reports what Broadcastify said on the call push, keyup writes that as the ledger row, and keyup uploads the call itself only when no recorder answered for it. Two recorders hear every call, so both — and keyup — ask a claim registry first; the loser skips instead of sending a duplicate.",
    ),
    (
        "export_broadcastify_feed_enabled",
        "bool",
        "Export",
        "Stream live audio to the Broadcastify Feed",
        "Open the Broadcastify **Feed** mount and stream this system's live calls to it as one continuous audio feed — the live stream on the feed's page, *not* the per-call Calls archive. **Off by default, and it must stay off while another broadcasting client is feeding the same mount**: an Icecast mount takes one source at a time, so the second one is refused. Turning it on starts the clock the same way the Calls switch does — only calls that arrive after the flip are ever played, and only live-band calls. Needs the Server, Mount and Password from the feed's *Feed Technical Details (Advanced)* panel; without them the switch does nothing.",
    ),
    (
        "broadcastify_feed_server",
        "string",
        "Export",
        "Feed server",
        "The **Server** from the feed's *Feed Technical Details (Advanced)* panel, e.g. `audio9.broadcastify.com`. Host name only — no scheme, no port.",
    ),
    (
        "broadcastify_feed_port",
        "int",
        "Export",
        "Feed port",
        "The **Port** from the same panel. Broadcastify publishes 80.",
    ),
    (
        "broadcastify_feed_mount",
        "string",
        "Export",
        "Feed mount",
        "The **Mount** from the same panel, e.g. `/yourmount`. The leading slash is optional — it is added if you leave it out.",
    ),
    (
        "broadcastify_feed_password",
        "secret",
        "Export",
        "Feed password",
        "The **Password** from the same panel — the Icecast *source* password for this mount. It is not the Broadcastify Calls API key and the two are not interchangeable. Write-only: stored, never shown again, and a blank field on save keeps the password you already have.",
    ),
    (
        "broadcastify_feed_username",
        "string",
        "Export",
        "Feed username",
        "Broadcastify's own instruction: \"If you are required to provide a username for your broadcast client, use: source\". There is no reason to change it.",
    ),
    (
        "broadcastify_feed_bitrate_kbps",
        "int",
        "Export",
        "Feed bitrate (kbit/s)",
        "The MP3 bitrate the mount is configured for. Broadcastify asks for mono MP3 at 16–32 kbit/s on a voice feed; 16 is their recommendation for scanner audio and the default here. It must match what the feed was provisioned with — a mismatch is heard as a wrong-speed stream.",
    ),
    (
        "broadcastify_feed_sample_rate",
        "int",
        "Export",
        "Feed sample rate (Hz)",
        "The stream's sample rate, 22050 by default, mono. Like the bitrate it has to match the mount's provisioning.",
    ),
    (
        "broadcastify_feed_max_backlog_s",
        "float",
        "Export",
        "Feed maximum backlog",
        "How many seconds of un-played audio the feed may hold before it starts dropping the **oldest** queued calls. A live feed's whole value is being live: when traffic arrives faster than real time the newest audio goes out and the rest is counted as dropped rather than played minutes late.",
    ),
    (
        "broadcastify_feed_silence",
        "bool",
        "Export",
        "Fill gaps with silence",
        "Send encoded silence between calls. **On, and it should stay on**: an Icecast mount drops a source that stops writing, and on a quiet system the gaps are most of the day. Turn it off only when debugging against a server that tolerates a silent source.",
    ),
    (
        "openmhz_api_key",
        "secret",
        "Export",
        "OpenMHz API key",
        "The per-system API key from your OpenMHz system settings. Write-only — stored, never shown again, and a blank field on save keeps the existing key. Falls back to `OPENMHZ_API_KEY` while no value is set here.",
    ),
    (
        "openmhz_short_name",
        "string",
        "Export",
        "OpenMHz short name",
        "The system's `shortName` — the path segment in `openmhz.com/system/<shortName>`, and in the upload URL. Falls back to `OPENMHZ_SHORT_NAME` while blank.",
    ),
    (
        "openmhz_url",
        "string",
        "Export",
        "OpenMHz API root",
        "The API root; the upload path is `/{shortName}/upload` under it. Change it only to point a test at a stub.",
    ),
    (
        "export_broadcastify_talkgroup_mode",
        "enum",
        "Export",
        "Broadcastify talkgroup selection",
        "Which talkgroups Broadcastify may publish. **all** = everything, and the per-talkgroup checkbox is ignored. **allow** = only the talkgroups whose BCFY checkbox is ticked. **deny** = everything except the talkgroups whose BCFY checkbox is cleared. `allow` and `deny` read the same checkbox and differ only in what you intend an untouched row to mean, so with every box still at its default they behave alike. A conventional call — no talkgroup — is judged by its frequency's checkbox on the Sites page under the same rule.",
    ),
    (
        "export_openmhz_talkgroup_mode",
        "enum",
        "Export",
        "OpenMHz talkgroup selection",
        "Which talkgroups OpenMHz may publish. **all** = everything, ignoring the per-talkgroup checkbox; **allow** = only the ticked ones; **deny** = everything but the cleared ones. Same column, same conventional-by-frequency rule as the Broadcastify mode above.",
    ),
    (
        "export_broadcastify_systems",
        "string",
        "Export",
        "Broadcastify systems",
        "Comma-separated **keyup** system ids (`systems.id`) Broadcastify may publish; empty means every system. This is the local key, not Broadcastify's numeric system id. Useful where one keyup archives several systems and only one of them is registered upstream.",
    ),
    (
        "export_openmhz_systems",
        "string",
        "Export",
        "OpenMHz systems",
        "Comma-separated keyup system ids OpenMHz may publish; empty means every system. One OpenMHz `shortName` is one system on their side, so a keyup holding several should usually name the one it belongs to.",
    ),
    (
        "export_ntp_server",
        "string",
        "Export",
        "NTP server for the clock check",
        'The server the exporter measures its own clock against, by one SNTP datagram. Step 1 of the Broadcastify Calls upload workflow is "verify time synchronization on the host system" — `ts` is part of the tuple they deduplicate on, so a drifted clock files calls at the wrong moment and cannot be corrected afterwards. More than 2 s out shows as a warning; more than 5 s **pauses the Calls uploads** (not the feed, not OpenMHz) until it recovers. Blank turns the query off and leaves the coarser `Date`-header reading from the service\'s own responses as the only measurement; an offset that could not be measured never pauses anything.',
    ),
    (
        "broadcastify_feed_include_unit_calls",
        "bool",
        "Export",
        "Feed: include unit-to-unit calls",
        "On. A unit-to-unit (private) call is one radio talking to one radio, on no talkgroup at all. The **Feed** carries audio with nothing attached to it, so a private call is just another voice on the stream — and leaving them out would quietly drop traffic an operator relaying their own system can hear. Turn it off if private conversations should not go out over a public feed. **Broadcastify Calls and OpenMHz never publish one** whatever this says: both archives are keyed on a talkgroup a private call does not have, and there is no honest value to send in its place — those two skip it with the reason `unit_call`.",
    ),
    (
        "export_include_encrypted",
        "bool",
        "Export",
        "Publish encrypted calls",
        "Off, and it should stay off: an encrypted call is a minute of digital noise with no intelligible audio in it, and publishing it fills somebody else's archive with unplayable recordings. On only if a downstream consumer genuinely wants the metadata.",
    ),
    (
        "export_min_duration_s",
        "float",
        "Export",
        "Minimum duration",
        "Recordings shorter than this many seconds are squelch tails and dead keys and are never published. 0 disables the floor. Independent of the transcription minimum — this one is about what is worth putting on somebody else's archive.",
    ),
    (
        "export_poll_seconds",
        "float",
        "Export",
        "Poll interval",
        "How often the exporter worker looks for new work, in seconds. Also the longest a flipped switch takes to take effect.",
    ),
    (
        "export_min_interval_s",
        "float",
        "Export",
        "Minimum gap between uploads",
        "The gentle rate limit: at least this many seconds between two uploads to the *same* service, so a burst of traffic does not hammer somebody else's server. Applies per service, not across both.",
    ),
    (
        "export_timeout_s",
        "float",
        "Export",
        "Request timeout",
        "Per-request HTTP timeout for one upload, in seconds. The transcode is allowed four times this before it is abandoned.",
    ),
    (
        "export_normalize",
        "bool",
        "Export",
        "Normalize exports",
        "Apply the transcription normaliser (the Transcription group's 'Normalize loudness' filter) to an unprocessed original before it goes out to Broadcastify, OpenMHz or the feed. Off, the services get the recording as made; transcription keeps its own copy either way.",
    ),
    (
        "export_target_lufs",
        "float",
        "Export",
        "Export loudness",
        "The integrated loudness every outbound service gets, applied at export time on top of the stored file. The archive is normalized to the Audio group's loudness target (-16 LUFS), which is picked for this app's player and the playback compressor it runs on top; nothing downstream has that compressor, so at -16 the same call arrives quiet on OpenMHz and on a Broadcastify feed. -11 LUFS is what those services sound like. A limiter at -1 dBTP always follows the lift, so it cannot clip. Set it to 0 to switch the whole stage off and send the exports at the stored level; clearing the field resets it to -11 like any other setting.",
    ),
    (
        "export_batch_size",
        "int",
        "Export",
        "Uploads per cycle",
        "How many ledger rows one worker cycle uploads per service. With the minimum gap above, this and the poll interval together set the sustained rate.",
    ),
    (
        "export_max_age_minutes",
        "int",
        "Export",
        "Discovery age limit",
        "A call older than this many minutes the first time the worker sees it never gets a ledger row at all, so a worker that was down for an afternoon starts from the last hour rather than replaying the gap. It never touches rows that already exist. 0 disables the bound.",
    ),
    (
        "export_max_attempts",
        "int",
        "Export",
        "Max upload attempts",
        "How many times one call is retried against one export service — with a widening backoff between tries — before it is parked as 'failed' in the export ledger and left alone. Failures never affect ingest, transcription or playback; a parked call is simply not re-published.",
    ),
    (
        "bcfy_owner_username",
        "string",
        "Feed stats",
        "Broadcastify username",
        "The Broadcastify account that **owns** the feed. The feed-owner API authenticates with your site login rather than an API key, and it only ever answers for feeds that account owns. Falls back to `BCFY_OWNER_USERNAME` while blank.",
    ),
    (
        "bcfy_owner_password",
        "secret",
        "Feed stats",
        "Broadcastify password",
        "The password for that account. Write-only: stored, never shown again, never logged, and leaving the field blank on save keeps the one you already have. Clearing it stops the poller however the switch is set.",
    ),
    (
        "bcfy_feed_id",
        "string",
        "Feed stats",
        "Feed id",
        "The numeric feed id — the `feedId` parameter, which is also the number in the feed's broadcastify.com URL. One feed per deployment; blank keeps the poller dormant.",
    ),
    (
        "bcfy_stats_enabled",
        "bool",
        "Feed stats",
        "Poll feed statistics",
        "Off, and it ships off. On, the worker asks the feed-owner API how the feed is doing every poll interval and keeps one row per answer, which is what the listener history and the hot-hours heatmap are built from. Polling reads somebody else's API on a timer, so leave the interval gentle.",
    ),
    (
        "bcfy_stats_poll_seconds",
        "float",
        "Feed stats",
        "Poll interval",
        "Seconds between two polls. 60 is one request a minute, which is fine granularity for listener counts and a rate no one will object to. A failing feed backs off on its own and does not poll at this rate while it is down.",
    ),
    (
        "bcfy_public_page_poll_seconds",
        "float",
        "Feed stats",
        "Public page interval",
        "Seconds between two fetches of the feed's **public** listen page. That page needs no login and server-renders a 24-hour hourly listener sparkline, which is the only listener *history* Broadcastify publishes anywhere — the owner API answers with one instant's count and nothing else. Each fetch fills in hours this deployment was not running for; an hour it did poll is never overwritten. The sparkline is hourly, so an hour between fetches reads everything there is to read. 0 turns it off. Requires the feed id and the poller switch above.",
    ),
    (
        "bcfy_archives_sync_enabled",
        "bool",
        "Feed stats",
        "Sync archive listings",
        "Off by default. On, the worker periodically fetches the list of days the feed has archives for and, for the last week, each day's individual archive blocks and their download links, so the listings can be browsed here rather than on the site.",
    ),
    (
        "bcfy_archives_sync_hours",
        "float",
        "Feed stats",
        "Archive sync interval",
        "How many hours between two archive-listing syncs. Archives appear at most a few times an hour and are kept for weeks, so there is nothing to gain from asking more often than every few hours.",
    ),
    (
        "bcfy_archives_backfill_days_per_sync",
        "int",
        "Feed stats",
        "Archive backfill per sync",
        "How many *older* days each sync catches up on, beyond the last week it always refreshes. Broadcastify lists every day the feed has archives for, and the first syncs of a new deployment have stored almost none of them; this is the budget for closing that gap. One day is one request, so 30 walks a year of history in about a fortnight of six-hourly syncs without ever making a burst of requests. 0 turns the backfill off and keeps only the recent week.",
    ),
]

#: Keys :func:`effective_settings` will accept from the table.
SETTABLE_KEYS = frozenset(key for key, *_ in SETTINGS_SPEC)

#: Keys the application itself keeps in the ``settings`` table and layers exactly like an
#: override, but that no form may write: ``PUT /admin/settings`` accepts only the spec.
#: ``retention_archive_roots`` is the history of every archive path retention has been
#: given (:func:`app.retention.remember_archive_root`) — state, not a knob — and it has to
#: reach every process's :class:`Settings`, because :func:`app.audio.audio_roots` reads it
#: from there.
INTERNAL_KEYS = frozenset({"retention_archive_roots"})

#: The choices behind an ``enum`` kind.  A separate map rather than a sixth tuple element
#: because the spec above is read positionally in several places and widening the tuple
#: would touch every one of them for the sake of two rows.
SETTINGS_OPTIONS: dict[str, list[str]] = {
    "audio_noise_reduction": ["off", "afftdn", "arnndn", "anlmdn"],
    "audio_tone_handling": ["keep", "attenuate", "remove"],
    "broadcastify_environment": ["production", "development"],
    "broadcastify_encoding": ["m4a", "mp3"],
    "broadcastify_mp3_bitrate_kbps": ["32", "16"],
    "broadcastify_upload_at": ["keyup", "recorder"],
    "export_broadcastify_talkgroup_mode": ["all", "allow", "deny"],
    "export_openmhz_talkgroup_mode": ["all", "allow", "deny"],
    "tls_acme_ca": ["production", "staging"],
    "tls_challenge": ["auto", "dns", "http"],
    "webhook_call_scope": ["all", "live", "linked", "live+linked"],
}

#: ``key -> (minimum, maximum)``, inclusive, for numeric keys where an out-of-range value
#: would be actively harmful rather than merely odd.  Checked by
#: :func:`app.api.admin.put_admin_settings` **before** the row is written, and that is the
#: point: a value pydantic later rejects does not fail on its own, it makes :func:`_merge`
#: throw away *the whole override set*, so one typo would silently revert every other
#: runtime setting at once.  ``None`` on either side means unbounded.
SETTINGS_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    # A zero-day session signs everybody out on their next request; a decade-long one is a
    # stolen laptop's forever.  A year is the outer edge of defensible.
    "auth_session_days": (1, 365),
    "auth_idle_hours": (1, 24 * 365),
    # A percentage outside 0-100 is a warning that always or never fires.
    "disk_warn_free_percent": (0, 100),
    "disk_critical_free_percent": (0, 100),
    "disk_warn_free_gb": (0, None),
    "disk_critical_free_gb": (0, None),
    # A negative threshold is a check that is always bad; 0 is "off".
    "health_recorder_silent_minutes": (0, None),
    "health_ingest_lag_minutes": (0, None),
    "health_stt_backlog_minutes": (0, None),
    "health_backup_max_age_hours": (0, None),
}

#: ``kind``s whose value is stored but never read back out.  ``secret`` is one: the Admin
#: page has to be able to *set* a signing key without also being a place the key can be
#: copied out of, and "who can see the archive" is a wider set than "who may hold its
#: credentials".  :func:`app.api.admin._to_str` blanks these; the row still says whether
#: one is set, which is the only thing a reader needs to know.
WRITE_ONLY_KINDS = frozenset({"secret"})

#: The keys those kinds cover, for anything that needs the set rather than the test.
WRITE_ONLY_KEYS = frozenset(key for key, kind, *_ in SETTINGS_SPEC if kind in WRITE_ONLY_KINDS)

_lock = threading.Lock()
_overrides: dict[str, Any] | None = None
_overrides_at: float = 0.0
_merged: Settings | None = None
_merged_base: Settings | None = None
#: The override dict :data:`_merged` was built from — compared by identity, so a merge
#: computed from a snapshot that has since been replaced is never handed out (see
#: :func:`effective_settings`).
_merged_values: dict[str, Any] | None = None


def invalidate() -> None:
    """Drop the cache, so the very next read sees a just-written override."""
    global _overrides, _overrides_at, _merged, _merged_values
    with _lock:
        _overrides = None
        _overrides_at = 0.0
        _merged = None
        _merged_values = None


def read_overrides(session: Session) -> dict[str, Any]:
    """The whitelisted (and internal), non-null rows of the ``settings`` table — one query, no cache."""
    return {
        row.key: row.value
        for row in session.scalars(select(Setting))
        if (row.key in SETTABLE_KEYS or row.key in INTERNAL_KEYS) and row.value is not None
    }


def _cached_overrides(session: Session) -> dict[str, Any]:
    global _overrides, _overrides_at, _merged
    now = time.monotonic()
    with _lock:
        if _overrides is not None and now - _overrides_at < CACHE_TTL_S:
            return _overrides
    values = read_overrides(session)
    with _lock:
        if values != _overrides:
            _merged = None  # the memoized merge is stale
            _overrides = values
        else:
            # Unchanged: keep the snapshot object the memoized merge was built from, so the
            # identity check in effective_settings still holds after the 5 s re-read.
            values = _overrides
        _overrides_at = time.monotonic()
    return values


def _merge(base: Settings, values: dict[str, Any]) -> Settings:
    if not values:
        return base
    try:
        return Settings.model_validate({**base.model_dump(), **values})
    except Exception:  # noqa: BLE001 - a bad row must not take the process down
        log.exception("settings: ignoring an unusable override set %s", sorted(values))
        return base


def effective_settings(session: Session, *, base: Settings | None = None) -> Settings:
    """Environment settings with the ``settings`` table layered on top.

    The table is read at most once every :data:`CACHE_TTL_S` seconds, and the merged object
    for the process-wide defaults is memoized with it, so the request path costs nothing
    once warm.  ``base`` layers the same overrides over a different starting point — how
    the worker and the tests keep their own :class:`Settings` while still honouring the UI.
    """
    global _merged, _merged_base, _merged_values
    values = _cached_overrides(session)
    # ``base=get_settings()`` is the process settings under another name: it gets the
    # memoized merge too, rather than a full re-validation (~4 ms) on every call — which
    # matters on the audio route, a hot path that passes it so tests can patch the base.
    process = get_settings()
    if base is not None and base is not process:
        return _merge(base, values)
    with _lock:
        # Keyed on the base it was built from as well: ``get_settings.cache_clear()`` makes
        # a new process settings object, and a merge of the old one must not outlive it.
        # And on the override snapshot: a thread that read the overrides just before a
        # settings write, and stores its merge just after the invalidation, would otherwise
        # leave the old values memoized until the next change.
        if _merged is not None and _merged_base is process and _merged_values is values:
            return _merged
    merged = _merge(process, values)
    with _lock:
        _merged = merged
        _merged_base = process
        _merged_values = values
    return merged


def settings_for(session: Session | None, fallback: Settings | None = None) -> Settings:
    """:func:`effective_settings` where a session is available, the plain settings otherwise."""
    if session is None:
        return fallback or get_settings()
    return effective_settings(session, base=fallback)


__all__ = [
    "CACHE_TTL_S",
    "INTERNAL_KEYS",
    "SETTABLE_KEYS",
    "SETTINGS_BOUNDS",
    "SETTINGS_OPTIONS",
    "SETTINGS_SPEC",
    "WRITE_ONLY_KEYS",
    "WRITE_ONLY_KINDS",
    "effective_settings",
    "invalidate",
    "read_overrides",
    "settings_for",
]
