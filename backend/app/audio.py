"""Where a call's recording actually is — the one place that answers it.

``calls.file_path`` is normally relative to ``AUDIO_ROOT``, but retention rewrites it to an
*absolute* path when it archives the file, so "join the root and go" is not enough and
"trust whatever the column says" is far too much: the column is written by ingest, by the
upload endpoint and by retention, and a path that escaped the tree would let any of them
turn ``GET /calls/{id}/audio`` into an arbitrary file read, or the nightly retention pass
into an arbitrary file *delete*.

:func:`resolve_audio` is therefore the only way to get from a :class:`~app.models.Call` to
a path — and :func:`resolve_playback`, which wraps it, the only way to get to the file a
*listener* should hear: the §5.5 processed copy when the call has one, the original when it
does not or when the caller asked for it.  ``processed_path`` is contained by exactly the
same rule as ``file_path``, because it is written by the same set of paths.

Resolution goes through symlinks and ``..`` and then insists the result lives under the audio
root or under a retention archive directory (:func:`archive_roots`: the current one and
every one retention has used) — and it distinguishes the
three ways a recording can be unavailable, because they mean different things to a caller:

``410 Gone``
    the row is right and the bytes are not there: purged by retention, or moved by hand.
``404 Not Found``
    the row names a path outside the tree.  Nothing legitimate produces this, so it is
    logged loudly and reported as if the audio simply were not there.

The other half of the module is where a recording *goes*: :func:`upload_partition` is the
one definition of the pushed-call layout — ``uploads/YYYY/MM/DD[/ab]``, dated by the call's
own ``started_at`` — shared by the upload endpoint, the metadata-only path and
``keyup refile-audio`` so that all three compute the same answer; :func:`prune_empty_dirs`
is what every deleting path calls afterwards so an expired day does not leave its
directories behind.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Settings

log = logging.getLogger("app.audio")

#: the subtree every pushed recording is filed under, relative to ``AUDIO_ROOT``
UPLOADS_DIR = "uploads"
#: where a push is written *while it is being received* — before anything has parsed it and
#: therefore before its partition is known.  Under the root, so it is the same filesystem
#: and ``os.rename`` into the partition is atomic and free; dot-prefixed, so the ingest
#: walk (which skips hidden directories) never mistakes a half-written body for a call.
INCOMING_DIR = f"{UPLOADS_DIR}/.incoming"

#: How old an entry in :data:`INCOMING_DIR` must be before :func:`sweep_incoming` removes it.
#: A receive takes seconds; an entry hours old is one a request lost track of (a crash, a
#: killed worker) and nothing will ever rename it into a partition.
INCOMING_MAX_AGE_HOURS = 6.0

#: ``calls.audio_state`` values that mean the bytes are meant to be on disk somewhere.
PRESENT_STATES = frozenset({"present", "archived"})

#: ``audio_meta.content`` values that mean there is nothing in the recording worth
#: transcribing, exporting or keeping: a mic key-up with no words behind it, and a channel
#: of broadband static.  The names themselves and the measurements behind them belong to
#: :mod:`app.audioproc.process`, which re-exports this; it is defined *here* so the
#: exporters and the ledger can read it without importing numpy to do so.
NO_CONTENT = ("none", "static")


class AudioUnavailable(Exception):
    """The recording cannot be served.  ``status`` is what an HTTP caller should answer."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def archive_roots(settings: Settings) -> list[Path]:
    """Every retention archive directory a recording may have been moved into.

    The current global path, plus the history :mod:`app.retention` keeps of every archive
    path it has been given — each policy's own and every past value — because a file
    archived under a path that has since changed is still a recording the archive owns: it
    must stay playable, and stay deletable when its row expires.
    """
    raw = [settings.retention_archive_path or "", *(getattr(settings, "retention_archive_roots", None) or [])]
    roots: list[Path] = []
    for value in raw:
        text = str(value or "").strip()
        if not text or not Path(text).is_absolute():
            continue
        resolved = Path(text).resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def audio_roots(settings: Settings) -> list[Path]:
    """Every directory a ``calls.file_path`` is allowed to resolve into."""
    roots = [Path(settings.audio_root).resolve()]
    for root in archive_roots(settings):
        if root not in roots:
            roots.append(root)
    return roots


def audio_path(call: object, settings: Settings) -> Path | None:
    """The resolved path for ``call``, or ``None`` when it is not inside an allowed root.

    Does not touch the filesystem beyond resolving the name, so callers that have their
    own opinion about a missing file — retention counts them — can use it too.
    """
    raw = getattr(call, "file_path", None)
    if not raw:
        return None
    # ``Path("/srv/audio") / "/archive/x.mp3"`` is ``/archive/x.mp3``: an archived row's
    # absolute path wins over the root, which is exactly what retention intended.
    candidate = (Path(settings.audio_root) / str(raw)).resolve()
    roots = audio_roots(settings)
    if not any(candidate.is_relative_to(root) for root in roots):
        log.warning(
            "audio: call %s names %r, which is outside %s",
            getattr(call, "id", "?"),
            str(raw),
            " and ".join(str(r) for r in roots),
        )
        return None
    return candidate


# --------------------------------------------------------------------------------------
# where a *new* recording goes
# --------------------------------------------------------------------------------------


def upload_bucket(*, sha256: str | None = None, name: str = "") -> str:
    """The two-hex-character sub-bucket a recording is filed in within its day.

    The content hash when the file has one, so the bucket is a property of the bytes and
    the upload path and :mod:`app.refile` compute the same answer from a row alone; a hash
    of the file name otherwise, which is the metadata-only (encrypted) call that has no
    bytes to hash but still needs a deterministic home.
    """
    if sha256:
        head = sha256.strip().lower()[:2]
        if len(head) == 2 and all(c in "0123456789abcdef" for c in head):
            return head
    return hashlib.sha256(name.encode()).hexdigest()[:2]


def upload_partition(
    started_at: datetime,
    settings: Settings,
    *,
    sha256: str | None = None,
    name: str = "",
) -> str:
    """``uploads/YYYY/MM/DD`` — or ``uploads/YYYY/MM/DD/ab`` — for a call, as a POSIX path.

    **The date is the call's own**, taken from ``started_at`` in ``TZ`` (the same zone the
    parser uses for ``started_local``), never the day the push arrived.  A backlog migration
    is a stream of 2022-2024 recordings arriving on one afternoon; filing them by arrival
    put nearly a million files in a single directory, which ext4's ``dir_index`` survives but
    ``rsync``, ``find``, a backup and every directory walk do not (docs/ARCHITECTURE.md,
    "Storage layout").  By call date the same archive spreads over ~1,100 real days.

    **Bucketing is one boolean**, ``AUDIO_BUCKET_UPLOADS``, on by default, and it applies to
    every pushed call alike.  The alternative considered — a ``bucket when the day will
    exceed N files`` threshold — cannot be answered at write time without either counting
    the directory (the very enumeration this layout exists to avoid) or a ``COUNT(*)`` per
    upload, and a threshold evaluated per-push would split one day between two layouts for
    no gain, because ``calls.file_path`` is the source of truth and nothing ever guesses a
    path.  256 buckets keeps a directory to a few hundred entries even on a day when a
    whole system is re-pushed, and costs 256 inodes for a day that only has ten calls,
    which is the cheaper mistake.
    """
    local = started_at.astimezone(ZoneInfo(settings.tz))
    parts = [UPLOADS_DIR, f"{local:%Y}", f"{local:%m}", f"{local:%d}"]
    if settings.audio_bucket_uploads:
        parts.append(upload_bucket(sha256=sha256, name=name))
    return "/".join(parts)


def prune_empty_dirs(path: Path, settings: Settings) -> int:
    """Remove ``path`` and its now-empty parents, stopping at (and never at) a root.

    Called after a recording is unlinked or moved away.  ``rmdir`` fails with ``ENOTEMPTY``
    the moment a directory still holds something, which is both the stop condition and the
    only locking this needs: a concurrent upload that has just created its target file
    keeps its own directory, and a directory removed a millisecond before a push needs it is
    remade by ``mkdir(parents=True, exist_ok=True)``.

    Returns the number of directories removed.  Never touches an audio root itself, and
    never anything outside one.
    """
    roots = {root for root in audio_roots(settings)}
    removed = 0
    current = path
    while True:
        try:
            resolved = current.resolve()
        except OSError:  # pragma: no cover - a parent vanished under us
            return removed
        if resolved in roots or not any(resolved.is_relative_to(root) for root in roots):
            return removed
        try:
            resolved.rmdir()
        except OSError:
            return removed
        removed += 1
        current = resolved.parent


def _newest_mtime(entry: Path) -> float:
    """The newest mtime of ``entry`` and, for a directory, of what is directly in it."""
    newest = entry.lstat().st_mtime
    if entry.is_dir() and not entry.is_symlink():
        with os.scandir(entry) as children:
            for child in children:
                try:
                    newest = max(newest, child.stat(follow_symlinks=False).st_mtime)
                except OSError:  # pragma: no cover - it went away mid-sweep
                    continue
    return newest


def sweep_incoming(
    settings: Settings,
    *,
    older_than_hours: float = INCOMING_MAX_AGE_HOURS,
    now: float | None = None,
) -> int:
    """Remove ``uploads/.incoming`` entries nothing has touched for ``older_than_hours``.

    Every request removes its own temp body on the way out, but a process that dies
    mid-request cannot, and nothing else ever looks in here.  Age is the newest mtime of the
    entry and its contents, so a body still being streamed (its file's mtime moves with
    every write) is never taken.  Returns the number of entries removed; never raises.
    """
    staging = Path(settings.audio_root) / INCOMING_DIR
    cutoff = (time.time() if now is None else now) - older_than_hours * 3600.0
    removed = 0
    try:
        entries = list(staging.iterdir())
    except FileNotFoundError:
        return 0
    except OSError:  # pragma: no cover - an unreadable staging directory
        log.warning("incoming sweep: cannot read %s", staging, exc_info=True)
        return 0
    for entry in entries:
        try:
            if _newest_mtime(entry) >= cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except FileNotFoundError:  # its request finished and cleaned up meanwhile
            continue
        except OSError:
            log.warning("incoming sweep: could not remove %s", entry, exc_info=True)
            continue
        removed += 1
    if removed:
        log.info("incoming sweep: removed %d abandoned upload(s) from %s", removed, staging)
    return removed


#: What the audio-processing stage (§5.5) names its output.  Defined here rather than in
#: :mod:`app.audioproc` so retention, the audio route and the waveform can find a processed
#: copy without importing numpy.
PROCESSED_SUFFIX = ".proc.mp3"


def processed_path_for(original: Path | str) -> Path:
    """``<stem>.proc.mp3`` beside ``original`` — the one definition of the name."""
    path = Path(original)
    return path.with_name(path.stem + PROCESSED_SUFFIX)


def processed_path(call: object, settings: Settings) -> Path | None:
    """The processed copy for ``call``, or ``None`` when there is not a usable one.

    Contained exactly as :func:`audio_path` contains ``file_path``, and for the same
    reason: ``processed_path`` is written by the processing worker and by the backfill, and
    a value that escaped the tree would turn ``GET /calls/{id}/audio`` into an arbitrary
    file read.  Also returns ``None`` when the row names a copy that is not on disk — a
    caller that gets ``None`` falls back to the original, which is always right.
    """
    raw = getattr(call, "processed_path", None)
    if not raw:
        return None
    candidate = (Path(settings.audio_root) / str(raw)).resolve()
    if not any(candidate.is_relative_to(root) for root in audio_roots(settings)):
        log.warning(
            "audio: call %s names processed copy %r, outside the audio roots",
            getattr(call, "id", "?"), str(raw),
        )  # fmt: skip
        return None
    return candidate if candidate.is_file() else None


def resolve_audio(call: object, settings: Settings) -> Path:
    """The file behind ``call``.  Raises :class:`AudioUnavailable` when there is not one."""
    state = getattr(call, "audio_state", "present") or "present"
    if state not in PRESENT_STATES:
        raise AudioUnavailable(410, "the recording has been purged")
    path = audio_path(call, settings)
    if path is None:
        raise AudioUnavailable(404, "no recording for this call")
    if not path.is_file():
        raise AudioUnavailable(410, "the recording is no longer on disk")
    return path


def resolve_playback(call: object, settings: Settings, *, raw: bool = False) -> tuple[Path, bool]:
    """``(path, processed)`` — what a listener should actually hear.

    The processed copy when there is one and the caller did not ask for the original; the
    original otherwise.  The original still has to resolve first even when a processed copy
    exists, because ``audio_state`` is the row's answer to "is there a recording at all" and
    a purged call must 410 whether or not a stale ``.proc.mp3`` survived the purge.
    """
    original = resolve_audio(call, settings)
    if raw:
        return original, False
    processed = processed_path(call, settings)
    return (processed, True) if processed is not None else (original, False)


__all__ = [
    "INCOMING_DIR",
    "INCOMING_MAX_AGE_HOURS",
    "PRESENT_STATES",
    "PROCESSED_SUFFIX",
    "UPLOADS_DIR",
    "AudioUnavailable",
    "archive_roots",
    "audio_path",
    "audio_roots",
    "processed_path",
    "processed_path_for",
    "prune_empty_dirs",
    "resolve_audio",
    "resolve_playback",
    "sweep_incoming",
    "upload_bucket",
    "upload_partition",
]
