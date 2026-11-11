"""Runtime configuration.

Every value below is settable from the environment (upper-cased field name) or from a
``.env`` file next to the process' working directory.  ``.env.example`` at the repo root
mirrors this file — keep the two in sync.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SttBackend(BaseModel):
    """One speech-to-text backend the transcription worker may route a job to.

    ``kind`` selects the adapter in ``app/stt/``:

    * ``openai_compatible`` — any OpenAI Audio API server (Speaches on the GPU box today).
      Uses ``base_url``, ``api_key``, ``model``.
    * ``faster_whisper_local`` — CTranslate2 in-process on the CPU.  ``model`` is a model
      name or path; ``base_url``/``api_key`` are ignored.  ``device``/``compute_type``/
      ``cpu_threads`` apply.
    * ``ovms`` — Intel's OpenVINO Model Server (``openvino/model_server``) over its
      ``/v3/audio/transcriptions`` endpoint.  Uses ``base_url``, ``model`` (the server's
      ``--model_name``, e.g. ``whisper-medium-fp16-ov``).  It is the only path that runs
      whisper on this host's NPU as well as its iGPU, but it accepts no ``prompt``, no
      ``hotwords`` and no ``vad_filter``, and answers with text only — so the domain prompt
      is not in play and quality scoring falls back to the text-based heuristics
      (docs/STT.md).  In compose it runs in the ``stt-ovms`` service.
    * ``openvino_genai`` — OpenVINO GenAI whisper in-process on an Intel CPU, iGPU or NPU
      (``device`` = ``CPU``|``GPU``|``NPU``|``AUTO``).  ``model`` is an OpenVINO-format HF
      repo id (``OpenVINO/whisper-large-v3-turbo-int8-ov``) or a local exported directory;
      ``cache_dir`` holds the download and the compiled-blob cache.  Needs the Intel
      userspace drivers, which the slim API image does not have — in compose this backend
      runs in the ``stt-openvino`` service and the worker reaches it as an
      ``openai_compatible`` entry instead (docs/STT.md).

    ``concurrency`` is how many jobs may run against this backend at once; ``priority``
    orders backends (lower first) when several are healthy.
    """

    name: str
    kind: Literal["openai_compatible", "ovms", "faster_whisper_local", "openvino_genai"] = (
        "openai_compatible"
    )
    base_url: str = ""
    api_key: str = ""
    model: str = "Systran/faster-whisper-large-v3"
    #: faster_whisper_local: cpu|cuda.  openvino_genai: CPU|GPU|NPU|AUTO.
    device: str = ""
    #: faster_whisper_local only (int8, float16, …)
    compute_type: str = "int8"
    cpu_threads: int = 4
    #: openvino_genai only: model download + compiled-blob cache (blank = ~/.cache/keyup/openvino)
    cache_dir: str = ""
    #: openvino_genai only: forward the domain prompt / the hotword list.  Hotwords default to
    #: off because openvino-genai repeats them into every 30 s window, and on short clips the
    #: decoder continues the list instead of the audio (app/stt/openvino_local.py).
    use_prompt: bool = True
    use_hotwords: bool = False
    concurrency: int = 2
    priority: int = 100
    timeout: float = 300.0
    enabled: bool = True


DEFAULT_STT_BACKENDS = [
    SttBackend(
        name="whisper",
        kind="openai_compatible",
        base_url="http://stt-cpu:8000",
        model="Systran/faster-whisper-small",
        concurrency=2,
        priority=10,
    )
]


def _tls_enabled_from_environment() -> bool:
    """The default for ``tls_enabled`` when nothing has set it.

    Before the HTTPS group existed there was no switch: a non-empty ``KEYUP_TLS_DOMAINS``
    *was* the switch, and ``deploy/edge/entrypoint.sh`` still reads it that way.  So an
    existing deployment that has never opened the Admin page must come up with TLS on, or
    the API's start-up apply would push a plain configuration into a working https site and
    quietly take the certificate down.

    ``os.environ`` rather than the settings' own sources because this is a *default*: it
    only runs when neither ``KEYUP_TLS_ENABLED`` nor a settings-table row supplied a value.
    Under compose that is the same thing — ``env_file:`` entries become real environment
    variables in the container — and a ``.env`` read only by pydantic is a development
    checkout, where TLS is off anyway.
    """
    explicit = os.environ.get("KEYUP_TLS_ENABLED")
    if explicit is not None and explicit.strip():
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    return bool(os.environ.get("KEYUP_TLS_DOMAINS", "").strip())


class Settings(BaseSettings):
    # ``populate_by_name`` because the ``tls_*`` group carries a ``validation_alias``: those
    # fields answer to the environment names the edge container already uses
    # (``KEYUP_TLS_DOMAINS``, ``CLOUDFLARE_API_TOKEN``, …) rather than to their own
    # upper-cased field names.  Without it, ``Settings.model_validate`` over a
    # ``model_dump()`` — which is exactly how ``app.runtime_settings.effective_settings``
    # layers the settings table — would stop accepting those fields by name and one runtime
    # override would drop every other one with it.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # ---- core -------------------------------------------------------------------------
    database_url: str = "postgresql+psycopg://keyup:keyup@localhost:55440/keyup"
    #: root of the recording tree; every ``calls.file_path`` is relative to it
    audio_root: Path = Path("/srv/keyup/audio")
    #: Where archived recorder event logs land, one ``<instance>/<date>.jsonl.gz`` per day
    #: (``POST /api/recorder/event-log``).  A sibling of ``audio_root`` rather than a
    #: directory inside it, deliberately: the ingest walk scans the audio tree, retention
    #: purges from it, and neither has any business finding a compressed JSONL log there.
    event_log_root: Path = Path("/srv/keyup/eventlogs")
    #: The most one uploaded day of event log may weigh (100 MB).  A day of a busy site
    #: compresses to single-digit megabytes; anything past this is a recorder shipping
    #: something other than a day, and is refused with 413 rather than written to disk.
    event_log_max_bytes: int = 100 * 1024 * 1024
    #: the recorder's wall clock — filename stamps and the ID3 ``Date`` are naive local time
    tz: str = "UTC"
    #: file a pushed call under ``uploads/YYYY/MM/DD/<2 hex>/`` rather than
    #: ``uploads/YYYY/MM/DD/``.  On by default: 256 buckets keep a single day's directory to
    #: a few hundred entries even when a whole system is re-pushed into it, and the bucket is
    #: a pure function of the recording's sha256, so the upload path and ``keyup
    #: refile-audio`` compute the same target.  Turning it off changes only where *new*
    #: calls land — ``calls.file_path`` is the source of truth and nothing guesses a path.
    audio_bucket_uploads: bool = True
    log_level: str = "INFO"
    #: the address people reach this archive at (the edge's port by default, like
    #: ``public_base_url``); its scheme drives ``AUTH_COOKIE_SECURE=auto``.
    public_url: str = "http://localhost:8095"

    # ---- ingest -----------------------------------------------------------------------
    #: full rescan interval for the watcher process (belt and braces behind inotify)
    ingest_scan_minutes: int = 5
    #: a file is eligible once its size has been stable this long
    ingest_settle_seconds: float = 3.0
    #: run the watchdog observer at all (false = periodic scan only)
    ingest_watch: bool = True
    #: default worker threads for ``keyup ingest scan``
    ingest_workers: int = 4
    #: directories under the scan root the ingest walk never descends into, comma separated.
    #: ``uploads`` is here because the push API writes those files *and their rows* itself:
    #: the walk could only ever find them already settled in the ledger, at the price of
    #: enumerating millions of files every ``INGEST_SCAN_MINUTES`` (docs/ARCHITECTURE.md,
    #: "Don't enumerate the tree").
    ingest_scan_skip: str = "uploads"
    #: stop the ingest walk after this many candidate recordings, 0 for no limit.  A
    #: guard-rail, not a schedule: it is logged loudly when it fires, because a truncated
    #: scan is a scan that did not finish.
    ingest_scan_max_files: int = 0
    #: fuzzy dedupe: |Δ started_at| in seconds…
    dedupe_window_seconds: float = 1.0
    #: …and relative duration difference (0.10 = 10 %)
    dedupe_duration_tolerance: float = 0.10
    #: consecutive calls on one talkgroup closer than this are one conversation
    conversation_gap_seconds: int = 20

    # ---- directory (docs/TALKGROUPS.md) ------------------------------------------------
    #: the agency code ``keyup mark-shared`` maps the shared dispatch/interop channels to
    #: when they have no owner (usually the dispatch center's own code).  Blank: the
    #: channels are only flagged ``shared`` and their ``agency_code`` is left as it is.
    #: Also a runtime setting (Admin → Settings → Directory).
    shared_agency_code: str = ""
    #: the agency CSV ``keyup seed-agencies`` loads (code,name,short_name,kind).  Blank: no
    #: seed, and the compose ``migrate`` step's ``--if-configured`` run does nothing.  The
    #: format is ``examples/agencies.example.csv``.
    seed_agencies_csv: str = Field(
        default="", validation_alias=AliasChoices("seed_agencies_csv", "KEYUP_SEED_AGENCIES")
    )

    # ---- cross-recorder reconciliation (docs/RECONCILIATION.md) ------------------------
    #: match two recorders' copies of one transmission and keep the better one as the
    #: primary.  False falls back to the old dedupe: first copy wins, the second is
    #: rejected on the push path.
    reconcile_enabled: bool = True
    #: how far two recorders may disagree about when one transmission happened, before
    #: their intervals are compared.  Not a guess: an sdrtrunk file's ``started_at`` is
    #: derived from the stamp the recorder writes when it *closes* the file, which sits at
    #: ``call end + 1.7 s`` (sd 0.43 s) against trunkd's control-channel grant times, so
    #: the file row runs about two seconds late and 2.0 is what makes it still overlap the
    #: push of the same call (docs/RECONCILIATION.md §5).
    reconcile_clock_skew_seconds: float = 2.0
    #: the two intervals must overlap by at least this fraction of the shorter one
    reconcile_min_overlap: float = 0.5
    #: …unless the shorter one is no longer than this and lies inside the longer: a
    #: late-granted trunkd fragment of a call sdrtrunk recorded whole
    reconcile_fragment_seconds: float = 1.5
    #: a copy longer than this multiple of the other is merged calls, not a better
    #: recording, so its duration credit is capped there
    reconcile_merge_ratio: float = 1.5
    #: recorder preference, best first — the lowest-weight tie-break in the score
    reconcile_prefer_sources: list[str] = Field(
        default_factory=lambda: ["trunkd", "trunk_recorder", "upload", "file"]
    )
    #: how often the worker runs the batch pass over recently ingested calls
    reconcile_minutes: int = 30
    #: how many candidate rows the batch pass examines per committed transaction.  The pass
    #: used to walk its whole window — up to 100,000 calls — in one transaction, which held
    #: every row it had updated and a ``KEY SHARE`` lock on every radio, site and talkgroup
    #: they point at until it finished, and held the per-talkgroup conversation advisory
    #: lock the whole time with it; on 2026-09-03 an upload waited 122 s behind one.  The
    #: pass is idempotent by construction (only primaries are examined and only primaries
    #: are matched), so a crash between chunks is safe and the next pass finishes the work.
    reconcile_batch_commit_every: int = 200

    # ---- trunk-recorder (§1.7) ---------------------------------------------------------
    #: which file to ingest when trunk-recorder wrote both a WAV and an M4A of one call.
    #: They are the same audio, so exactly one becomes the call; the M4A is a tenth the size.
    tr_prefer_audio: Literal["m4a", "wav"] = "m4a"
    #: trunk-recorder closes the audio file and writes the ``.json`` beside it a moment
    #: later, so the watcher holds a settled recording this long waiting for the sidecar.
    #: When it never arrives the call is ingested from the filename and the audio header
    #: with ``time_source='file_write'`` rather than being dropped.
    tr_sidecar_wait_seconds: float = 30.0

    # ---- transcription policy ---------------------------------------------------------
    #: default ``talkgroups.transcribe`` for a talkgroup created by ingest
    transcribe_default: bool = True
    #: shorter calls are squelch tails; Whisper hallucinates on them
    transcribe_min_seconds: float = 0.75
    #: attempts before a transcription job is parked as failed
    transcribe_max_attempts: int = 3

    # ---- speech to text ---------------------------------------------------------------
    # STT_BACKENDS is a JSON list; each entry is
    #   {"name": str,
    #    "kind": "openai_compatible" | "ovms" | "faster_whisper_local" | "openvino_genai",
    #    "base_url": str, "api_key": str, "model": str,
    #    "device": str, "compute_type": str, "cpu_threads": int, "cache_dir": str,
    #    "concurrency": int, "priority": int, "timeout": float, "enabled": bool}
    # The default is a single Speaches server.  Add a second entry with
    # kind=faster_whisper_local to let the Keyup host transcribe on its own CPU when the GPU box
    # is unreachable; the scheduler orders by `priority` (lower first) and fails over.
    # On the Core Ultra host, kind=openvino_genai with device=NPU or GPU transcribes on the
    # accelerator instead — in compose that runs in the stt-openvino service and is reached
    # as an openai_compatible entry at http://stt-openvino:8000 (docs/STT.md).
    stt_backends: list[SttBackend] = Field(default_factory=lambda: list(DEFAULT_STT_BACKENDS))
    #: always ask the backend to run voice-activity detection (14 % of calls are < 1 s)
    stt_vad: bool = True
    stt_language: str = "en"
    #: appended to the auto-assembled domain prompt (talkgroup aliases, units, city names)
    stt_prompt_extra: str = ""
    #: Place names that occur on this system's traffic — towns, campuses, landmarks — as a
    #: comma-separated list.  They join the domain prompt and the hotword list after the
    #: talkgroup and unit aliases.  Blank means no place section at all.
    stt_places: str = ""
    #: ask for word-level timestamps where the backend supports it
    stt_word_timestamps: bool = False
    #: how many transcription jobs one worker process claims at a time
    stt_batch_size: int = 4
    # hallucination heuristics (§5.2)
    #: Extra hallucination patterns, one regex per line (or ``;``-separated), merged with
    #: the builtin credit family in ``app/stt/postprocess.py``.  A transcript matching one
    #: is labeled ``noise`` and the call keeps no text.  Editable at runtime, because the
    #: next caption-credit variant whisper invents should not need a deploy.
    stt_hallucination_patterns: str = ""
    #: Site-specific corruption repairs applied to every transcript before it is judged:
    #: ``pattern => replacement`` rules, one per line or ``;``-separated, case-insensitive —
    #: e.g. ``\bxcom(\.com)+\b => XCOM`` for a callsign whisper writes as a domain
    #: (``app/stt/postprocess.py``, ``HALLUCINATION_REWRITES``).  Read at start-up.
    stt_text_rewrites: str = ""
    stt_no_speech_threshold: float = 0.6
    stt_compression_ratio_threshold: float = 2.4
    #: transcription worker threads in the ``worker`` process
    stt_workers: int = 2
    #: a job still ``running`` after this long has lost its worker and is requeued
    stt_stale_minutes: int = 10
    #: At most this share of ``STT_WORKERS`` may hold a backlog-band job (priority >= 500)
    #: at once, so a burst of live calls always finds a free thread.  0.5 with two workers
    #: means one backlog slot and one thread that only ever takes live work (§5.2).
    stt_backlog_worker_share: float = 0.5
    #: loudness-normalize every clip before it reaches an engine (loud alert tones, soft
    #: talkers).  Applied to the transcription input only — an ffmpeg ``-af`` filter in the
    #: audio-prep pass that already transcodes to 16 kHz mono — never to the stored recording,
    #: its sha256 or the dedupe (app/stt/normalize.py, §5.2).
    stt_normalize: bool = True
    #: whether the exporters apply that same normalising filter to an *unprocessed* original
    #: on its way out; ``stt_normalize`` alone used to govern both, so the engine could not
    #: keep its evened-out copy while the services received the recording as made
    export_normalize: bool = True
    #: the ffmpeg ``-af`` filter string.  Blank uses the built-in default for the installed
    #: ffmpeg (``speechnorm`` where available, else ``dynaudnorm``, else ``loudnorm``).
    stt_normalize_filter: str = ""
    #: Normalize even when the job is running on an already-processed copy (§5.5).  Off:
    #: doing it twice expands audio that is already even and undoes the gate.
    stt_normalize_always: bool = False

    # ---- stored-audio processing (§5.5, docs/ARCHITECTURE.md "Audio processing") --------
    # These govern the *stored* processed copy (``<stem>.proc.mp3``), not the throwaway
    # decode the transcriber and the exporters make.  Measured on six hours of live traffic:
    # the mean level spans 20 dB across calls, a third carry more than half a second of dead
    # air at the head, and the two-tone pages that matter most to a listener are the loudest
    # thing in the archive.  Every one is runtime-settable (``SETTINGS_SPEC``) because the
    # right value is found by listening, not by deploying.
    #: master switch: with this off, ingest queues no ``audio_process`` job and every reader
    #: falls back to the original recording.
    audio_process_enabled: bool = True
    #: strip leading/trailing dead air
    audio_trim_silence: bool = True
    #: level below which a stretch counts as dead air, dBFS
    audio_trim_threshold_db: float = -40.0
    #: padding left in front of the first word / behind the last, milliseconds
    audio_trim_keep_ms: int = 150
    #: loudness target, LUFS
    audio_target_lufs: float = -16.0
    #: true-peak ceiling the chain always ends on, dBFS.  Well under 0 on purpose: the
    #: limiter caps a PCM peak, but the archive is 16 kbps MP3 at 8 kHz, and a lossy
    #: decoder reconstructs peaks 1.5-2 dB above the ones it was handed.
    audio_true_peak_db: float = -4.0
    #: the most gain a short clip's measured-gain branch may apply, dB
    audio_max_gain_db: float = 30.0
    #: audio under this is treated as silence, so the gain does not raise the hiss between
    #: words along with the words.  Blank the feature by setting it to -100.
    audio_gate_db: float = -50.0
    #: a clip whose mean is still under this after trimming is marked ``silent`` in
    #: ``audio_meta`` and shown with a muted icon.  It is stored either way.
    audio_silent_below_db: float = -60.0
    #: the level a stretch of the *processed* copy has to clear to count as speech, dBFS —
    #: the ``silent`` floor asks whether the whole clip is quiet, this asks how much of it
    #: is not.  A mic key-up with no words behind it averages about -50 dB and clears
    #: ``audio_silent_below_db`` without anybody having said anything.
    audio_speech_floor_db: float = -40.0
    #: …and how many seconds above that floor a call needs before ``audio_meta.content`` is
    #: ``speech`` rather than ``none``.
    audio_min_speech_seconds: float = 0.25
    #: how flat a 300-3400 Hz frame spectrum has to be to count as noise rather than a
    #: voice (geometric mean over arithmetic mean: 1.0 is white noise, ~0.05 is speech)…
    audio_static_flatness: float = 0.30
    #: …and the share of a clip's live frames that have to be, before ``audio_meta.content``
    #: is ``static``.  Paired with a level test at -35 dBFS, which is the half of the rule
    #: doing the work: a loud clip is somebody talking whatever its spectrum looks like.
    audio_static_frame_share: float = 0.70
    #: set ``calls.audio_policy = 'discard'`` on a ``content = none`` call, so the discard
    #: pass takes its recording after the grace and leaves the metadata behind
    audio_discard_no_speech: bool = True
    #: the speech band the global chain keeps, Hz.  A P25 vocoder carries nothing under
    #: 300 Hz, so what is down there is rumble the denoiser would otherwise have to fight.
    #: The top edge is a guard rail rather than a cut: ``lowpass`` is two-pole, and at 3400
    #: it was 3.35 dB into the 2.5-3.8 kHz band where the consonants are
    #: (the 2026-09-14 band-pass measurement).  ``0`` on either edge drops that half.
    audio_bandpass_low_hz: float = 300.0
    audio_bandpass_high_hz: float = 3800.0
    #: ``off`` | ``afftdn`` | ``arnndn`` | ``anlmdn``
    audio_noise_reduction: str = "afftdn"
    #: afftdn's noise floor, dB — where the filter *thinks* the noise is, not how hard it
    #: pushes (that is ``nr``).  Measured floors on this system run -20 to -76 dBFS with a
    #: median of -63, so the old -25 and then -30 both claimed far more noise than exists,
    #: and the subtraction came out of the fricatives: every ``nf`` from -30 to -45 removes
    #: the same 18 dB of noise, while -30 costs 2.17 dB of the 2.5-3.8 kHz speech band and
    #: -35 costs 1.06 (the 2026-09-14 band-pass measurement).
    audio_noise_floor_db: float = -35.0
    #: …and how many dB of that floor afftdn actually takes out (``nr=``).  0 leaves ``nr``
    #: off the command line, which is ffmpeg's own default of 12.
    audio_noise_reduction_nr: float = 18.0
    #: an RNNoise model file for ``arnndn``; blank means none is available and the setting
    #: falls back to ``afftdn`` with a logged warning
    audio_rnnoise_model: str = ""
    #: find sustained pure tones and record them
    audio_tone_detect: bool = True
    #: ``keep`` | ``attenuate`` | ``remove``
    audio_tone_handling: str = "attenuate"
    #: how far ``attenuate`` pulls a *single* held tone — a console attention tone — down, dB
    audio_tone_attenuate_db: float = 24.0
    #: …and how far it pulls a **page** (two-tone, warble, alert) down.  Shallower on purpose:
    #: a page is information a listener wants to hear, an attention tone is only loud.
    audio_page_tone_attenuate_db: float = 18.0
    #: bump to make every call reprocess lazily
    audio_reprocess_version: int = 1
    #: retries before an audio job is parked as failed
    audio_process_max_attempts: int = 3

    # ---- upload admission control (§4.1, docs/RECORDERS.md) ----------------------------
    # The push path shares one transcription queue between trunkd's live calls and a bulk
    # migration of the sdrtrunk archive.  These bound the queue and tell a client to slow
    # down instead of letting it build an unbounded backlog (app/ingest/flow.py).
    #: open (queued or running) live-band jobs above which even a live push is refused 429
    upload_max_queue_live: int = 5000
    #: open backlog-band jobs above which a backlog/bulk push is refused 429
    upload_max_queue_backlog: int = 300
    #: refuse backlog when the whole queue needs longer than this to drain
    upload_backlog_max_eta_seconds: float = 900.0
    #: simultaneous in-flight backlog uploads (a semaphore, per API process)
    upload_backlog_max_concurrency: int = 4
    #: free bytes under ``AUDIO_ROOT`` below which every class is refused 507 (5 GiB)
    upload_min_disk_free_bytes: int = 5 * 1024 * 1024 * 1024
    #: operator switch: stop accepting backlog/bulk entirely (also in the settings table)
    upload_pause_backlog: bool = False
    #: How long one upload's write transaction may wait for a *lock* before giving up
    #: (Postgres ``lock_timeout``, milliseconds; 0 disables the cap).  The upload path takes
    #: a channel advisory lock and then updates the talkgroup/site counter rows, and every
    #: one of those can be held by another process — the file watcher, ``keyup reconcile``'s
    #: batch pass, which walks up to 100k calls in a single transaction — for as long as
    #: that process cares to hold it.  Waiting is unbounded by default, and on 2026-09-03 one
    #: upload waited 122 s.  A recorder spools and retries, so failing that push is strictly
    #: better than holding a connection (and, before the threading fix, the whole event
    #: loop) for minutes; the wait shows up as the ``import failed:`` 500 the endpoint has
    #: always answered for a failed write.
    upload_lock_timeout_ms: int = 15_000

    # ---- retention --------------------------------------------------------------------
    #: global default; a retention_policies row for a system/talkgroup overrides it.
    #: 0 = keep forever.
    retention_keep_days: int = 0
    #: purge the MP3 after this many days while keeping the row and transcript (0 = never)
    retention_keep_audio_days: int = 0
    #: keep transcripts when the audio is purged
    retention_keep_transcripts: bool = True
    #: move purged audio here instead of deleting it (blank = delete)
    retention_archive_path: str = ""
    #: Every archive directory retention has ever been pointed at — the global path and each
    #: policy's, past values included.  Not an operator knob: :mod:`app.retention` appends to
    #: it (a ``settings`` row of the same name) whenever an archive path is saved or used, and
    #: :func:`app.audio.audio_roots` allows all of them, so a call archived under a path that
    #: has since changed still plays, and is still unlinked when its row expires.
    retention_archive_roots: list[str] = []
    #: Where an archive path may live, comma separated.  An archive directory becomes an
    #: allowed root for ``GET /calls/{id}/audio``, so saving one is checked against this
    #: list rather than trusted: environment only, because it is the boundary the runtime
    #: setting is checked against.  ``/srv/keyup`` is where compose mounts every data volume.
    retention_archive_allowed_roots: str = "/srv/keyup"
    #: how long ``radio_events`` rows are kept (0 = for ever).  Deliberately **not** zero
    #: by default and deliberately shorter than the audio: these rows are presence data
    #: about identifiable people — where a radio was, when it turned on, which talkgroup
    #: its user selected — and they exist whether or not that user ever transmitted
    #: (docs/RADIO_EVENTS.md, "retention and privacy").
    radio_events_keep_days: int = 90
    #: how long ``activity_events`` rows (the operational activity log) are kept.  The
    #: nightly retention pass prunes older rows so the table stays bounded (0 = for ever).
    activity_retention_days: int = 30
    # ---- disk space alert (app/disk_watch.py) ----------------------------------------
    #: The worker samples free space every five minutes on every filesystem it knows —
    #: ``AUDIO_ROOT``, the archive paths and ``DISK_WATCH_PATHS`` — and raises
    #: ``system.disk_low`` (warn) when free space drops under *either* warn threshold, and
    #: again (error) under either critical one.  One event per crossing; re-armed once the
    #: filesystem is back above.  Runtime-settable.
    disk_warn_free_percent: float = 15.0
    disk_warn_free_gb: float = 30.0
    disk_critical_free_percent: float = 5.0
    disk_critical_free_gb: float = 10.0
    #: Extra paths to watch, comma separated — e.g. the database volume mounted read-only
    #: into the worker, which is otherwise invisible to it.
    disk_watch_paths: str = ""
    # ---- health alerts (app/health.py) ------------------------------------------------
    #: The worker checks these every minute; each condition is raised once (activity row +
    #: every enabled ``health`` alert rule) when it goes bad and cleared once when it has
    #: been good for :data:`app.health.RECOVERY` — never repeated while it stays bad.
    #: ``0`` switches that check off.  All four are runtime-settable.
    #: A ``hot`` recorder (docs/RECORDERS.md) that has not reported for this many minutes.
    health_recorder_silent_minutes: int = 5
    #: No call recorded, anywhere, for this many minutes.  The longest real quiet in the
    #: two weeks before 2026-09-23 was 13.5 minutes (a weekday 04:00), so 20 does not
    #: fire on a quiet night.
    health_ingest_lag_minutes: int = 20
    #: The oldest *live-band* transcription job (priority under the backlog band) has been
    #: due for this many minutes: STT is down or cannot keep up.  Backlog work is ignored.
    health_stt_backlog_minutes: int = 30
    #: The newest database backup manifest in ``BACKUP_DIR`` is older than this many hours.
    health_backup_max_age_hours: int = 30
    #: Where ``deploy/backup/keyup-db-backup.sh`` writes, as the worker sees it (compose
    #: mounts the host's /srv/keyup/backups/db read-only).  Not a directory here → the
    #: backup check is skipped, which is every deployment that does not mount it.
    backup_dir: str = "/srv/keyup/backups/db"
    #: nightly maintenance (retention, gap detection, stats) — APScheduler cron
    cron_retention: str = "20 3 * * *"
    cron_gaps: str = "40 3 * * *"
    #: nightly sweep of finished export bundles older than ``EXPORT_KEEP_DAYS``
    cron_export_vacuum: str = "50 3 * * *"

    # ---- recorder gap detection -------------------------------------------------------
    #: a silence longer than this on a system that normally has traffic is a recorder gap
    gap_minutes: int = 30

    # ---- site channel audit / RadioReference (docs/CHANNEL_AUDIT.md) --------------------
    #: RadioReference SOAP credentials.  All three blank (the default) leaves the whole RR
    #: half of the audit dormant: the tables, the observations endpoint and the report all
    #: work, they simply have no ``radioreference`` rows to compare against.  A **Premium**
    #: subscription and an app key of the operator's own are required — they are personal
    #: credentials and are never shipped with the code.
    rr_app_key: str = ""
    rr_username: str = ""
    rr_password: str = ""
    #: minimum hours between two syncs of one system.  RR is somebody else's server and a
    #: sync costs it three calls; nothing here polls it faster than this.
    rr_sync_hours: int = 24
    #: Keyup system name → RadioReference system id, e.g. {"Example System": 1234}.  A
    #: ``systems.rr_system_id`` set through the API wins over this map.
    rr_systems: dict[str, int] = Field(default_factory=dict)
    #: an RR-documented voice channel not heard within this many days counts as "missing"
    channel_audit_stale_days: int = 14
    #: how often the worker recomputes the audit and logs a line per site
    channel_audit_minutes: int = 60

    # ---- multi-recorder arbitration and best source (docs/RECORDERS.md) ----------------
    # Several recorders now hear the same site.  Two questions follow: which of them is
    # supposed to be recording it (the arbiter), and whose copy of a transmission do we
    # keep (best source).  All four knobs are runtime settings as well
    # (:data:`app.runtime_settings.SETTINGS_SPEC`, group "Recorders"), so the values here
    # are only the defaults the settings table is layered over.

    #: **The arbiter switch, and it is off.**  On, a background job every 30 s promotes a
    #: warm standby recorder when every hot recorder covering a site has gone stale, and
    #: demotes it again once the hot side has been back for the recovery window.  Off, it
    #: does not so much as look at the recorders — which is the right default for a
    #: deployment with one recorder, where there is nothing to arbitrate.
    arbiter_enabled: bool = False
    #: how long since a recorder last *reported* before the arbiter calls it down
    arbiter_stale_seconds: int = 60
    #: how long every hot recorder for a site must be healthy before the spare stands down
    arbiter_recovery_seconds: int = 300
    #: compare two receivers' quality reports on a duplicate upload and keep the better
    #: audio.  Off falls back to "first copy wins", which is what the archive did before.
    best_source_enabled: bool = True

    # ---- outbound call exporters (docs/EXPORTERS.md) -----------------------------------
    # Broadcastify Calls and OpenMHz re-publish this system's traffic.  Both ship
    # **disabled** — the enable switches are runtime settings (``export_broadcastify_enabled``
    # / ``export_openmhz_enabled``, both default false), because this deployment shares a
    # feed with an existing uploader and two uploaders on one system duplicate every call.
    # Credentials live here, in the environment, exactly like the RR_* logins: they are
    # per-deployment secrets and are never committed with the code.

    #: Broadcastify **Calls** API key — the per-system key from the Broadcastify Calls
    #: dashboard, *not* the live-feed mount password.  Blank keeps the exporter dormant
    #: however the enable switch is set.
    broadcastify_api_key: str = ""
    #: Broadcastify Calls system id (their numeric system, not our ``systems.id``)
    broadcastify_system_id: str = ""
    #: Optional slot/RRSlot when the account is configured for one; blank sends none.
    broadcastify_slot: str = ""
    #: The **production** metadata endpoint, from Broadcastify's own documentation.
    broadcastify_url: str = "https://api.broadcastify.com/call-upload"

    #: **Which set of credentials and which endpoint.**  Broadcastify issues development
    #: and production credentials separately and each is refused by the other's endpoint —
    #: "Use the development endpoint with development credentials only.  Production
    #: credentials are issued separately and must be used against the production endpoint."
    #: This one field picks URL, key and system id together (``app.exporters.broadcastify``),
    #: so the two sets can never be crossed by editing one field and forgetting another.
    broadcastify_environment: str = "production"
    #: The development endpoint.  **This URL is a guess** — Broadcastify's documentation
    #: names the development *status* page (``broadcastify.com/calls-dev/status/``) and
    #: says a development endpoint exists, but does not print its address; this is the
    #: production path under the ``calls-dev`` prefix the status page uses.  It has to be
    #: confirmed against a real development account before the development environment is
    #: relied on, and that is why it is a setting rather than a constant.
    broadcastify_dev_url: str = "https://api.broadcastify.com/call-upload-dev"
    #: The development API key, kept apart from the production one on purpose.
    broadcastify_dev_api_key: str = ""
    #: The development system id, likewise.
    broadcastify_dev_system_id: str = ""

    #: ``m4a`` (AAC, the spec's default and preference) or ``mp3``.  Drives the encode, the
    #: ``enc`` form field and the stage-2 ``Content-Type`` together.
    broadcastify_encoding: str = "m4a"
    #: When ``broadcastify_encoding`` is ``mp3``: 32 or 16 kbit/s, the only two the spec
    #: lists.  Both are mono 22050 Hz CBR.
    broadcastify_mp3_bitrate_kbps: int = 32
    #: How long after a transmission *ends* Broadcastify Calls still accepts it, in
    #: seconds.  Their answer past it is ``1 REJECTED-CALL-SKEW-TOO-LONG-<secs>``, and the
    #: measurement is receipt − (``ts`` + ``callDuration``) — the age of the call at the
    #: moment stage 1 lands, counted from its end.  15 s is what production observation
    #: shows: every accepted upload was inside ~14 s.  The worker keeps a 1 s margin under
    #: this for the round trip and skips anything already past it, without an HTTP call.
    #: 0 disables the pre-check entirely and lets Broadcastify answer for itself.
    broadcastify_skew_limit_s: float = 15.0

    #: **Who POSTs the call to Broadcastify Calls**: ``keyup`` (this host, after ingest)
    #: or ``recorder`` (each trunkd recorder, the moment the call ends).  A runtime
    #: setting rather than a deployment constant because it is the kind of choice an
    #: operator changes while watching the numbers on the Exports page, and because the
    #: recorders learn it from the desired-state poll within one poll of the change.
    #: Whatever it says, keyup stays the system of record: the ledger row is written here
    #: either way, and a claim registry (``bcfy_claims``) keeps two recorders — or a
    #: recorder and keyup — from both registering the same call.
    broadcastify_upload_at: str = "keyup"

    # ---- Broadcastify **Feed** (the live Icecast stream, a different product) ---------
    # Broadcastify's feed management page publishes these five under "Feed Technical
    # Details (Advanced)", and they are named here exactly as it names them so an operator
    # can copy them across without translating anything.  They are the *mount* credentials
    # and are refused by the Calls API, just as ``broadcastify_api_key`` is refused by the
    # mount (docs/EXPORTERS.md).

    #: e.g. ``audio9.broadcastify.com`` — the feed page's **Server**.
    broadcastify_feed_server: str = ""
    #: the feed page's **Port**; Broadcastify publishes 80.
    broadcastify_feed_port: int = 80
    #: the feed page's **Mount**, e.g. ``/yourmount``.  A leading slash is optional.
    broadcastify_feed_mount: str = ""
    #: the feed page's **Password** — the Icecast source password, not the Calls API key.
    broadcastify_feed_password: str = ""
    #: the feed page's **Username**: "If you are required to provide a username for your
    #: broadcast client, use: source".
    broadcastify_feed_username: str = "source"
    #: The stream's format.  Broadcastify asks for mono MP3 at 16–32 kbit/s on a voice
    #: feed; 16 kbit/s at 22.05 kHz is their own recommendation for scanner audio and is
    #: what the encoder produces unless this is changed.  Both must match the mount.
    broadcastify_feed_bitrate_kbps: int = 16
    broadcastify_feed_sample_rate: int = 22_050
    #: How many seconds of un-played audio the feed may hold before it starts dropping the
    #: **oldest** queued calls.  A live feed that plays a backlog is not live.
    broadcastify_feed_max_backlog_s: float = 120.0
    #: Fill the gaps between calls with encoded silence.  On, and it should stay on: an
    #: Icecast mount drops a source that stops writing.
    broadcastify_feed_silence: bool = True
    #: Whether unit-to-unit (private) calls go out on the feed.  **On.**  The feed carries
    #: audio with no identity attached — no talkgroup, no radio id, nothing a listener could
    #: file — so a private call on it is a voice among the others, and leaving it out would
    #: silently hide traffic from an operator who is relaying their own system's air.
    #: Broadcastify Calls and OpenMHz are not offered the choice: their archives are keyed
    #: on a talkgroup a private call does not have (docs/EXPORTERS.md).
    broadcastify_feed_include_unit_calls: bool = True
    #: **The feed's switch, and it is off.**  The operator already streams this feed from
    #: another broadcast client; two sources cannot hold one mount, and turning this on
    #: before that one is stopped simply fails.
    export_broadcastify_feed_enabled: bool = False

    #: OpenMHz per-system API key and the system's ``shortName`` (its path segment).
    openmhz_api_key: str = ""
    openmhz_short_name: str = ""
    #: The API root; the upload path is ``/{shortName}/upload``.
    openmhz_url: str = "https://api.openmhz.com"

    #: **The switches, and they are off.**  These two are also runtime settings
    #: (:data:`app.runtime_settings.SETTINGS_SPEC`, group "Export"), which is how an
    #: operator turns a service on without a deploy; the values here are only the defaults
    #: the table is layered over.  They default ``False`` because this deployment's system
    #: is already being uploaded by other software, and two uploaders publish every call
    #: twice.  Turning one on also stamps an enable watermark: only calls that arrive after
    #: that instant are ever published (app/exporters/gate.py).
    export_broadcastify_enabled: bool = False
    export_openmhz_enabled: bool = False

    #: **Which talkgroups a service may publish.**  ``all`` publishes every talkgroup that
    #: passes the other guards and ignores the per-talkgroup checkboxes entirely; ``allow``
    #: publishes *only* the talkgroups whose ``export_<service>`` checkbox is ticked;
    #: ``deny`` publishes everything *except* the talkgroups whose checkbox is cleared.
    #: The checkbox is the same column in both directions (``talkgroups.export_broadcastify``
    #: / ``talkgroups.export_openmhz``, default true); the mode only decides whether it is
    #: consulted, and which way round.  Conventional calls — no talkgroup, keyed by
    #: frequency — are judged by ``site_frequencies.export_<service>`` under the same rule.
    export_broadcastify_talkgroup_mode: str = "all"
    export_openmhz_talkgroup_mode: str = "all"
    #: Encrypted calls carry nothing intelligible; publishing a minute of digital noise
    #: helps nobody, so they are dropped before the ledger.  Set true only if a downstream
    #: consumer genuinely wants them.
    export_include_encrypted: bool = False
    #: Recordings shorter than this many seconds are squelch tails and dead keys.
    export_min_duration_s: float = 0.5
    #: Comma-separated **keyup** ``systems.id`` values a service may publish.  Empty (the
    #: default) means every system.  This is keyup's own primary key, not the remote
    #: service's system id — that is ``broadcastify_system_id`` / ``openmhz_short_name``.
    export_broadcastify_systems: str = ""
    export_openmhz_systems: str = ""

    #: How many times one call is retried against one service before it is parked as
    #: ``failed`` in ``call_exports`` and never retried automatically again.
    export_max_attempts: int = 5
    #: How often the exporter worker looks for new work, seconds.
    export_poll_seconds: float = 15.0
    #: Minimum gap between two uploads to the *same* service — the gentle rate limit that
    #: keeps a burst of traffic from hammering somebody else's server.
    export_min_interval_s: float = 1.0
    #: Per-request HTTP timeout for an upload, seconds.
    export_timeout_s: float = 30.0
    #: **The integrated loudness every outbound service is sent at**, LUFS, applied at
    #: export time on top of the stored file.  The archive is normalized to
    #: ``audio_target_lufs`` (-16), which is chosen for the in-app player and its playback
    #: compressor; nothing downstream has that compressor, so at -16 the same call is
    #: audibly quiet on OpenMHz and on a Broadcastify feed.  Raising the stored target
    #: instead made the player far too loud, hence a second number
    #: (:mod:`app.exporters.loudness`).  0 disables the stage entirely.
    export_target_lufs: float = -11.0
    #: How many ledger rows one worker cycle will upload per service.
    export_batch_size: int = 20
    #: **The NTP server the exporter checks its own clock against.**  Step 1 of the
    #: Broadcastify Calls workflow is "verify time synchronization on the host system", and
    #: a call registered with a drifted ``ts`` is deduplicated against the wrong tuple and
    #: cannot be corrected afterwards.  One SNTP datagram, no dependency
    #: (:mod:`app.exporters.clock`).  Blank disables the query and leaves the coarser
    #: ``Date``-header fallback as the only measurement.
    export_ntp_server: str = "pool.ntp.org"
    #: Calls older than this many minutes are never picked up, even inside the live band:
    #: a service that was down for an hour should not be handed an hour of stale traffic
    #: the moment it comes back.  0 disables the age bound.
    export_max_age_minutes: int = 60

    # ---- alerts / notifications --------------------------------------------------------
    #: base URL used in alert links (call page and audio); set it to the address people
    #: reach this archive at.  The default is the edge's own port on this machine.
    public_base_url: str = "http://localhost:8095"
    #: fallback Discord webhook when an alert rule does not carry its own
    discord_webhook_url: str = ""
    #: ntfy server, e.g. https://ntfy.sh — the topic comes from the rule's channel_config
    ntfy_url: str = ""
    #: SMTP relay for the ``email`` channel; a blank host disables the channel
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "keyup@localhost"
    #: per-delivery HTTP timeout for webhook/discord/ntfy (seconds)
    alert_timeout_s: float = 5.0

    # ---- outbound webhooks (docs/WEBHOOKS.md) -------------------------------------------
    # One HTTP endpoint told about the events a consumer can act on, so a CAD integration can
    # match a new call the moment it lands instead of polling /calls every minute.
    # All three are *runtime* settings as well (:data:`app.runtime_settings.SETTINGS_SPEC`,
    # group "Webhooks") because turning a consumer's receiver on and off is an operational
    # act, not a deploy; the values here are only the defaults the table is layered over.

    #: **The switch, and it is empty.**  A blank URL is what keeps the whole feature dark:
    #: nothing is enqueued, the table stays empty, and the worker's tick returns at once.
    webhook_url: str = ""
    #: Shared secret for the ``X-Keyup-Signature`` HMAC-SHA256 over the exact request body.
    #: Blank sends no signature, which is only sensible on a trusted LAN — and the receiver
    #: has no way to tell a real call from anybody who found the URL.
    webhook_secret: str = ""
    #: Comma-separated subset of :data:`app.models.WEBHOOK_EVENTS`.  Empty means *none*:
    #: a URL with no events subscribed is a URL that is never called, which is the right
    #: reading of "somebody filled in half the form".
    webhook_events: str = ""
    #: Which *calls* ``call.stored`` / ``call.transcribed`` are sent for — ``all`` (every
    #: call, the shipped behaviour), ``live`` (only ``ingest_priority == 'live'``, so an
    #: archive migration replaying 2016 is not news a consumer has to filter out),
    #: ``linked`` (only calls that already carry an external link) or ``live+linked``.
    #: Link events are never scoped by it: a link is a human act on one particular call.
    webhook_call_scope: str = "all"
    #: Attempts against one delivery before it is parked as ``failed`` and left alone.
    webhook_max_attempts: int = 5
    #: How often the worker looks for due deliveries, seconds.
    webhook_poll_seconds: float = 5.0
    #: How many deliveries one tick sends.  They go **one at a time** (docs): a consumer's
    #: receiver is somebody else's server and this is not the place to open ten sockets.
    webhook_batch_size: int = 20
    #: Per-request HTTP timeout, seconds.
    webhook_timeout_s: float = 10.0

    # ---- Broadcastify feed-owner statistics (docs/BCFY_STATS.md) -----------------------
    # The *feed* API, which is a different product from Broadcastify Calls above: it reports
    # on a live audio feed the operator owns (listeners, status, archive availability) and
    # authenticates with the owner's own Broadcastify **login**, not an API key.  Seeded
    # here and overridable at runtime (group "Feed stats"), and every switch ships off.

    #: Broadcastify account username that owns the feed.
    bcfy_owner_username: str = ""
    #: …and its password.  A ``secret`` runtime setting: stored, never read back, never logged.
    bcfy_owner_password: str = ""
    #: The numeric feed id (the ``feedId`` parameter, and the number in the feed's URL).
    bcfy_feed_id: str = ""
    #: The poller's switch.  Off, so a deployment with no feed does nothing.
    bcfy_stats_enabled: bool = False
    #: Seconds between two ``a=feed`` polls.
    bcfy_stats_poll_seconds: float = 60.0
    #: Seconds between two fetches of the **public** feed page, whose server-rendered
    #: sparkline is the only 24-hour listener history Broadcastify hands out (and the only
    #: backfill this feature has).  An hour, because the sparkline is hourly; ``0`` is off.
    bcfy_public_page_poll_seconds: float = 3600.0
    #: The archive-listing sync's own switch, and how often it runs, in hours.
    bcfy_archives_sync_enabled: bool = False
    bcfy_archives_sync_hours: float = 6.0
    #: How many *backlog* days one archive sync may fetch listings for, on top of the last
    #: week it always refreshes.  This is the one-time catch-up over the feed's whole
    #: archive history, spread over successive syncs rather than made in one burst.
    bcfy_archives_backfill_days_per_sync: int = 30
    #: The owner API root.  Overridable only so a test can point at a local stub.
    bcfy_owner_url: str = "https://api.broadcastify.com/owner/"

    # ---- the HTTPS edge (docs/HTTPS.md) -----------------------------------------------
    # These six are the **seed** for the runtime group "HTTPS": a row in the settings table
    # wins, and an unset row falls through to the environment — the precedence every other
    # runtime key here has.  The environment names are the ones the ``edge`` container
    # already reads, deliberately: one variable configures both the container's own start-up
    # choice and the API's, so a redeploy and an Apply cannot disagree.
    #
    # ``tls_enabled`` had no environment name before, because a non-empty
    # ``KEYUP_TLS_DOMAINS`` *was* the switch; ``_tls_enabled_from_environment`` keeps that
    # true for a deployment that has never opened the Admin page.

    #: The master switch.  Off ships off; the edge is then a plain HTTP pass-through.
    tls_enabled: bool = Field(
        default_factory=_tls_enabled_from_environment,
        validation_alias=AliasChoices("tls_enabled", "KEYUP_TLS_ENABLED"),
    )
    #: Space-separated hostnames the certificate covers.  Blank is off however the switch is
    #: set — a certificate needs a name.
    tls_domains: str = Field(
        default="", validation_alias=AliasChoices("tls_domains", "KEYUP_TLS_DOMAINS")
    )
    #: The Cloudflare API token DNS-01 writes its TXT record with: Zone -> DNS -> Edit on the
    #: one zone, nothing wider.  A ``secret`` runtime setting — stored, never read back.
    tls_cloudflare_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("tls_cloudflare_api_token", "CLOUDFLARE_API_TOKEN"),
    )
    #: Contact address on the ACME account; expiry warnings go here.
    tls_acme_email: str = Field(
        default="", validation_alias=AliasChoices("tls_acme_email", "ACME_EMAIL")
    )
    #: ``production`` | ``staging``.  The environment's ``ACME_CA`` is a directory *URL*, so
    #: the validator below maps one onto the other; a URL is still honoured verbatim.
    tls_acme_ca: str = Field(
        default="production",
        validation_alias=AliasChoices("tls_acme_ca", "KEYUP_TLS_ACME_CA", "ACME_CA"),
    )
    #: With TLS on: whether plain ``:80`` redirects browsers to https (the default, and what
    #: this deployment has always done) or serves the whole site in the clear beside it.
    #: ``/health`` and ``/api/*`` are never redirected either way — recorders push by IP.
    #: Addresses allowed to tell the edge who the visitor is: the Cloudflare Tunnel connector.
    #: Space-separated IPs or CIDRs; blank trusts nobody but loopback (docs/REVERSE-PROXY.md).
    edge_trusted_proxies: str = ""
    tls_redirect_http: bool = Field(
        default=True,
        validation_alias=AliasChoices("tls_redirect_http", "KEYUP_TLS_REDIRECT_HTTP"),
    )
    #: ``auto`` | ``dns`` | ``http``: how the certificate is obtained.  ``dns`` is Cloudflare
    #: DNS-01 (needs the token); ``http`` is Caddy's default HTTP-01/TLS-ALPN-01 (needs the
    #: names to resolve publicly to this box with inbound 80/443).  ``auto`` is DNS-01 when a
    #: token is set, HTTP-01 otherwise (``app.edge.render.tls_challenge``).
    tls_challenge: str = Field(
        default="auto",
        validation_alias=AliasChoices("tls_challenge", "KEYUP_TLS_CHALLENGE"),
    )
    #: Where the edge's Caddy admin API answers.  A deployment value rather than a runtime
    #: setting: it is a compose-network address, and a knob that could point the API at
    #: somebody else's proxy would be a hole rather than a feature.
    edge_admin_url: str = "http://edge:2019"

    @field_validator("tls_acme_ca", mode="before")
    @classmethod
    def _acme_ca_enum(cls, value: object) -> str:
        """``ACME_CA`` is a URL in the environment and an enum in the settings table.

        Blank means production, which is what ``entrypoint.sh`` already defaults it to; a
        staging directory URL becomes ``staging``; any other URL passes through, so a
        deployment pinned to some other ACME server keeps working.
        """
        text = str(value or "").strip()
        if not text:
            return "production"
        if text.startswith(("http://", "https://")):
            return "staging" if "staging" in text else text
        return text.lower()

    # ---- auth -------------------------------------------------------------------------
    #: reads are open by default; mutations always need a credential
    auth_require_read: bool = False
    #: reserved for future signed URLs (audio and export links that carry their own proof).
    #: Nothing signs with it today: sessions and API tokens are database rows and only their
    #: sha256 is stored, so rotating this key logs nobody out and invalidates nothing.
    secret_key: str = "change-me-in-production"
    #: The two session deadlines.  Both are also runtime settings (group "Access"), read
    #: through ``app.runtime_settings.effective_settings``; these are their defaults.
    #: ``auth_session_days`` is absolute and stamped on the session row at sign-in;
    #: ``auth_idle_hours`` slides and is re-read on every request.
    auth_session_days: int = 14
    auth_idle_hours: int = 12
    auth_cookie_secure: str = "auto"
    #: how many reverse proxies sit in front of this process.  0 (the default) ignores
    #: ``X-Forwarded-For``/``X-Real-IP`` entirely and treats the peer as the client; 1 trusts
    #: the last hop, 2 the last two.  Anything else lets a caller forge its own address in
    #: the audit columns and in the login rate limiter.
    trusted_proxy_hops: int = 0

    # ---- api --------------------------------------------------------------------------
    #: cap on a single streamed export; 0 = uncapped
    export_max_rows: int = 0
    #: where ``POST /exports`` writes its zip bundles (audio + calls.csv + transcripts.json)
    export_dir: Path = Path("/srv/keyup/exports")
    #: hard cap on how many calls one ``POST /exports`` bundle may contain
    export_max_calls: int = 20_000
    #: hard cap on the bytes of audio one bundle may carry (2 GiB)
    export_max_bytes: int = 2 * 1024 * 1024 * 1024
    #: the nightly vacuum deletes finished bundles older than this many days (0 = keep)
    export_keep_days: int = 7
    #: cached waveform peak files; unset = ``<AUDIO_ROOT>/../cache/waveform``
    waveform_cache_dir: Path | None = None
    #: how many peak buckets ``GET /calls/{id}/waveform`` returns
    waveform_buckets: int = 200
    #: ``ingest.status`` heartbeat interval on the live feed, seconds
    live_status_seconds: int = 15
    #: How long a syndication feed (RSS/Atom) waits for a call's transcript and audio jobs
    #: before publishing it anyway, seconds after ingest.  The readiness gate holds an item
    #: back until both queues let go of it; without a cap a stuck speech or audio worker
    #: froze every feed.  0 = no cap (wait for ever).
    feed_ready_max_wait_s: float = 900.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
