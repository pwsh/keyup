"""Outbound call exporters: Broadcastify Calls and OpenMHz (docs/EXPORTERS.md).

Keyup's own archive is the point of the system; these two services are a *republishing*
side-channel, and the module is built around three properties that follow from that.

**Both services ship disabled.**  ``export_broadcastify_enabled`` and
``export_openmhz_enabled`` are runtime settings defaulting to ``false``, and the
credentials are environment variables that default to empty.  Nothing here uploads
anything until an operator turns a switch on *and* the environment carries a key.  This is
not a cautious default; it is a correctness requirement — this system already has an
uploader feeding it from other software, and two uploaders publish every call twice.

**Only live traffic is ever published.**  A 400 000-call archive migration is streaming
into the same ``calls`` table as the live recorder.  :mod:`app.exporters.gate` refuses
anything that is not in the live admission band *and* newer than the moment the service was
enabled, and both conditions are checked twice — once in SQL when the ledger row is
created, once in Python immediately before the request goes out.

**Nothing here can break the archive.**  Uploading happens in its own worker thread over
its own ledger table (``call_exports``); every failure is caught, counted and parked.
Ingest, transcription, retention and playback have no dependency on any of it.

The modules:

``gate``
    the two safety guards and the enable watermark.
``ledger``
    ``call_exports`` as a queue — discovery, ``FOR UPDATE SKIP LOCKED`` claiming, backoff,
    and the ``UNIQUE (call_id, service)`` that makes a double upload structurally
    impossible.
``transcode``
    stored MP3 → m4a/AAC, through the same loudness filter the transcription path uses.
``broadcastify`` / ``openmhz``
    the two request shapes, each documenting where every field came from.
``broadcastify_feed`` / ``mp3``
    the *other* Broadcastify product: a live Icecast source that plays the same selected
    calls as one continuous MP3 stream, paced at real time and silence-filled between
    calls, plus the frame reader that pacing needs.  Same two safety guards, same operator
    selection, its own switch — and that switch ships off too.
``worker``
    the cycle that drives all of the above.
"""

from __future__ import annotations

from .base import CallFacts, ExportError, PermanentExportError, SkipCall
from .broadcastify_feed import BroadcastifyFeed, FeedConfig, FeedSupervisor, feed_config
from .gate import exportable, is_live_band, sync_watermark, watermark
from .ledger import summary
from .worker import ExportWorker, run_export_worker

__all__ = [
    "BroadcastifyFeed",
    "CallFacts",
    "ExportError",
    "ExportWorker",
    "FeedConfig",
    "FeedSupervisor",
    "PermanentExportError",
    "SkipCall",
    "exportable",
    "feed_config",
    "is_live_band",
    "run_export_worker",
    "summary",
    "sync_watermark",
    "watermark",
]
