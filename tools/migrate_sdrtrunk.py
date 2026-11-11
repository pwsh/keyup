#!/usr/bin/env python3
"""Push an sdrtrunk archive into Keyup through ``POST /api/call-upload``.

The archive is years of recordings that predate the recorder ever having been pointed at
an API — about 20 GB of it, on an SMB share, as **one ZIP per channel per day**:

    //nas/scanner/48 FIRE OPS/20260829.zip
                             /20260830.zip
    //nas/scanner/52 EVENT 6/20260829.zip

This walks that layout in place — the zips are read through their central directories and
one member at a time, never extracted — and uploads every call as *backlog*, which is the
whole point: Keyup's admission control (``backend/app/ingest/flow.py``) queues their
transcription behind live traffic, refuses them with ``429`` when the queue is deep, and
this client obeys the ``Retry-After`` it is given instead of hammering.  The calls
themselves are stored — and searchable — the moment they are accepted; only the
transcription waits.

Standard library only, on purpose: it runs on the recorder host, an old NAS, a rescue
shell — anywhere with a python3.11+ and no way to install anything.

    tools/migrate_sdrtrunk.py --url http://keyup.example.org --key kup_… \\
        --ledger ~/keyup-migration.jsonl /mnt/sdrtrunk-archive

Sources
-------
Any number of them, and each may be a **directory** (recursed: ``.zip`` archives are opened
and loose ``.mp3`` files are taken as they are), a single **``.zip``**, or a **glob**
(``'/mnt/archive/48 FIRE OPS/2026*.zip'`` — quote it so the shell leaves it alone).  A
corrupt archive, or a member that will not decompress, is logged and counted as failed;
it never ends the run.

What it sends per call
----------------------
``key``, ``priority`` (``backlog`` unless ``--priority`` says otherwise), the audio part,
``audioName`` (the **original member name**, which is what lets Keyup's sdrtrunk parser read
the system, site, channel, talkgroup and radio out of it), ``audioType=audio/mpeg``, and —
parsed from the name — ``talkgroup`` and ``source``.  ``talkgroup`` is not optional: the
endpoint answers ``incomplete call data: no talkgroup`` without one.

The channel folder (``48 FIRE OPS``) is informational — the talkgroup comes from the name
and the ID3 frames — but it is recorded in the ledger, and sent as ``talkgroupLabel`` when
*neither* the filename nor the recording's own ``TIT2`` carries an alias.  It is a last
resort on purpose: ``talkgroupLabel`` outranks the ID3 alias on the push path, so sending
the folder blindly would overwrite the better name with the worse one.

It does **not** send ``dateTime`` unless asked (``--send-datetime``), which makes the
server's reading of ``audioName`` the *only* source of the call's time — so a name Keyup's
parser cannot read has no time at all.  The endpoint refuses such a push at ``backlog``
priority (400, ``{"error": "no_call_time"}``) rather than filing the call under the moment
it arrived; that is deliberate, and it is the guard on the failure this run already had
once, when a parser that rejected every pre-2025 filename put 1,041,258 calls on their
upload date (docs/MIGRATION.md, "Timestamps").  A refusal is counted as failed, stays out
of the ledger's done set, and is re-sent by the next run — so the fix is to deploy the
parser (or pass ``--send-datetime``) and start the tool again.  Whether an sdrtrunk
filename stamp is the call's *start* or the instant the finished file was *closed* (its end)
is being measured against the production recorder; until that lands, the server's own
reading of the ID3 ``Date`` frame plus the duration it measures places the call correctly
and this client stays out of it.  When it lands, it is one flag either way:
``--send-datetime`` alone if the stamp is the start, ``--datetime-is-end`` as well if it is
the end (the duration parsed out of the MP3's own frame headers is subtracted).

Not uploading what Keyup already has
------------------------------------
``POST /api/call-upload`` can only answer ``duplicate call rejected`` once it has read the
entire multipart body, so re-running this over an archive Keyup already holds costs the
whole transfer to be told nothing was needed — days of it, for a few million calls, on a
path that is rate-limited whenever live traffic is busy.  So before it uploads, each worker
takes a batch of up to 200 pending recordings, hashes their bytes (the same sha256 the
ledger records — nothing is hashed twice), and asks ``POST /api/upload/exists``
(``{"sha256": [...]}`` → ``{"present": [...]}``, one indexed lookup, not an upload as far as
flow control is concerned).  Every hash that comes back is written to the ledger as
``duplicate`` with ``precheck: already stored`` — the same done-status a server-side
duplicate gets, so a resumed run skips it — and no bytes are sent for it.  The progress
line shows the subset as ``dup=N (pre M)``.  A server too old to have the endpoint answers
404 and the precheck turns itself off for the rest of the run; any other trouble falls back
to the upload path for that batch.  ``--no-precheck`` disables it outright.

Flow control
------------
Before each batch it polls ``GET /api/upload/flow`` and waits while ``accepting.backlog`` is
false.  Every response carries ``X-Keyup-Queue-*`` headers, so the common case costs no poll
at all.  ``429``/``507`` are obeyed to the second via ``Retry-After``; ``5xx`` and timeouts
retry with exponential backoff; ``duplicate call rejected`` counts as done.

The ledger
----------
``--ledger path.jsonl`` appends one JSON object per finished call — its key
(``archive.zip!member.mp3``, or the plain path for a loose file), the channel folder, the
member's CRC and size, the sha256 of the bytes, the status, the server's answer — and is
read at startup so a re-run skips what is already in.  Kill it and start it again; it picks
up where it stopped.
"""

from __future__ import annotations

import argparse
import glob as globlib
import hashlib
import http.client
import json
import mimetypes
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Ledger",
    "Recording",
    "Uploader",
    "id3_alias",
    "iter_recordings",
    "main",
    "mp3_duration",
    "parse_name",
]

#: sdrtrunk writes ``.raw.wav`` alongside the mp3 for some configurations; it is the
#: undecoded capture, not a call, and Keyup's own watcher ignores it too.
SKIP_SUFFIXES = (".raw.wav",)
DEFAULT_SUFFIXES = (".mp3",)
ARCHIVE_SUFFIX = ".zip"

#: ``20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3``
#: The underscore after the time is optional: older sdrtrunk builds butt the system
#: straight against it (``20240120_012207Example_System_…__TO_1481_FROM_9802004.mp3``).
NAME_RE = re.compile(r"^(?P<date>\d{8})_(?P<time>\d{6})_?(?P<sysite>.+?)__TO_(?P<rest>.+)$")
#: ``P1157-[1812--1561--1811]`` — a patch: the supergroup is the talkgroup to send
PATCH_RE = re.compile(r"^P(?P<patch>\d+)-\[(?P<members>[\d\-]*)\]$")
#: a fully-qualified radio, ``101010.57.9831120`` rendered with underscores in the name
RADIO_RE = re.compile(r"^(?:\d+_){0,2}(?P<radio>\d+)$")
#: ``1701-Fire-Dispatch`` — some sdrtrunk configurations print the alias after the id
ALIASED_TO_RE = re.compile(r"^(?P<tgid>\d+)[-_ ](?P<alias>.+)$")
#: a glob is only expanded when it looks like one
MAGIC_RE = re.compile(r"[*?\[]")
#: ``20260829``/``2026-08-29``: a date folder is a filing convention, never a channel alias
DATE_FOLDER_RE = re.compile(r"^\d+$|^\d{4}[-_]\d{2}[-_]\d{2}$")

#: the literal bodies the endpoint answers with (backend/app/api/call_upload.py)
OK_BODY = "Call imported successfully."
DUPLICATE_BODY = "duplicate call rejected"

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 507})
MAX_SLEEP = 600.0
DONE_STATUSES = ("uploaded", "duplicate", "skipped")

#: ``POST /api/upload/exists`` takes up to 500 digests; 200 keeps a worker's held bytes
#: bounded (it hashes a batch before it asks) while still costing one request per 200 calls.
PRECHECK_BATCH = 200
#: what the ledger records for a call the server said it already had, before any upload
PRECHECK_DETAIL = "precheck: already stored"


# --------------------------------------------------------------------------------------
# filenames
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NameParts:
    """What the recording's own name says.  Every field may be ``None``."""

    stamp: str | None = None  # 'YYYYMMDD_HHMMSS', local time
    talkgroup: int | None = None
    source: int | None = None
    patch: int | None = None
    #: a talkgroup alias printed in the name itself, when there is one
    alias: str | None = None


def parse_name(name: str) -> NameParts:
    """Read ``…__TO_<tg>[_FROM_<radio>][_V2].mp3``.

    Deliberately forgiving: an unreadable name is not a reason to skip a recording, it is a
    reason to send fewer form fields and let the server's own parser (which also reads the
    ID3 frames) do the work.
    """
    stem = Path(name).stem
    match = NAME_RE.match(stem)
    if match is None:
        return NameParts()
    rest = match["rest"]
    if (version := re.match(r"^(?P<stem>.*)_V\d+$", rest)) is not None:
        rest = version["stem"]
    to_raw, _, from_raw = rest.partition("_FROM_")

    talkgroup: int | None = None
    patch: int | None = None
    alias: str | None = None
    if (patched := PATCH_RE.match(to_raw)) is not None:
        # The supergroup is the talkgroup the call was carried on; Keyup reconstructs the
        # members from the ID3 frames, which are richer than the name.
        patch = int(patched["patch"])
        talkgroup = patch
    elif to_raw.isdigit():
        talkgroup = int(to_raw)
    elif (aliased := ALIASED_TO_RE.match(to_raw)) is not None:
        talkgroup = int(aliased["tgid"])
        alias = aliased["alias"].replace("-", " ").strip() or None

    source: int | None = None
    if from_raw and (radio := RADIO_RE.match(from_raw)) is not None:
        source = int(radio["radio"])

    return NameParts(
        stamp=f"{match['date']}_{match['time']}",
        talkgroup=talkgroup,
        source=source,
        patch=patch,
        alias=alias,
    )


# --------------------------------------------------------------------------------------
# reading the archive
# --------------------------------------------------------------------------------------

#: one open ``ZipFile`` per worker thread.  Members are uploaded in timestamp order, so a
#: worker usually stays inside one day's archive for a while; reopening costs a central
#: directory read, keeping it open costs one file descriptor per thread.
_archives = threading.local()


def _archive(path: Path) -> zipfile.ZipFile:
    entry = getattr(_archives, "entry", None)
    if entry is not None:
        if entry[0] == path:
            return entry[1]
        entry[1].close()
        _archives.entry = None
    handle = zipfile.ZipFile(path)
    _archives.entry = (path, handle)
    return handle


def close_archives() -> None:
    """Drop this thread's open archive, if it has one."""
    entry = getattr(_archives, "entry", None)
    if entry is not None:
        entry[1].close()
        _archives.entry = None


@dataclass(frozen=True, slots=True)
class Recording:
    """One call to upload: a loose file, or a member of a per-day ZIP.

    ``key`` is what the ledger is keyed by, so a re-run recognizes exactly this member of
    exactly this archive.  For a loose file it is the path, which is what the ledger held
    before archives were a thing — an old ledger still resumes.
    """

    path: Path
    #: the member name inside ``path``; None for a loose file
    member: str | None = None
    #: the channel folder this came out of, informational (``48 FIRE OPS``)
    folder: str | None = None
    size: int = 0
    #: the member's CRC-32 as the archive recorded it; None for a loose file
    crc32: int | None = None

    @property
    def name(self) -> str:
        # rpartition, not Path(): this runs once per member of a million-member archive.
        return self.member.rpartition("/")[2] if self.member else self.path.name

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix

    @property
    def key(self) -> str:
        return f"{self.path}!{self.member}" if self.member else str(self.path)

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """Chronological: an sdrtrunk name starts with its own timestamp, which orders
        members across archives without opening or stat-ing anything."""
        return (parse_name(self.name).stamp or "", self.name, self.key)

    def read_bytes(self) -> bytes:
        """The member's bytes, decompressed on their own.

        ``ZipFile.open`` streams one member out of the archive and checks its CRC as it
        goes, so a bit-rotted member raises rather than uploading silently wrong audio.
        """
        if self.member is None:
            return self.path.read_bytes()
        with _archive(self.path).open(self.member) as handle:
            return handle.read()

    def ledger_fields(self) -> dict[str, object]:
        return {
            "key": self.key,
            "path": str(self.path),
            "member": self.member,
            "name": self.name,
            "folder": self.folder,
            "size": self.size,
            "crc32": None if self.crc32 is None else f"{self.crc32:08x}",
        }


def _channel_folder(*candidates: str | None) -> str | None:
    """The first candidate that could be a channel alias.

    The archive is a folder per channel — ``48 FIRE OPS`` — but a tree filed by date has
    ``20260829`` in the same position, and that is a date, not the name of anything.
    """
    for candidate in candidates:
        name = (candidate or "").strip()
        if name and not DATE_FOLDER_RE.match(name):
            return name
    return None


def _is_recording(name: str, suffixes: tuple[str, ...]) -> bool:
    lower = name.rpartition("/")[2].lower()
    if not lower or lower.startswith("."):
        return False
    if any(lower.endswith(skip) for skip in SKIP_SUFFIXES):
        return False
    return any(lower.endswith(suffix) for suffix in suffixes)


def _zip_members(path: Path, suffixes: tuple[str, ...], on_error) -> list[Recording]:
    """Every recording in one archive, from its central directory alone.

    Nothing is decompressed here: this is what makes counting 20 GB of archives up front
    cheap enough to do before the first upload.
    """
    found: list[Recording] = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        if on_error is not None:
            on_error(path, f"{type(exc).__name__}: {exc}")
        return []
    # One folder string for the whole archive: at a million members the difference between
    # sharing it and allocating it per member is tens of megabytes.
    channel = _channel_folder(path.parent.name)
    for info in infos:
        if info.is_dir() or not _is_recording(info.filename, suffixes):
            continue
        inner = info.filename.rpartition("/")[0].rpartition("/")[2]
        found.append(
            Recording(
                path=path,
                member=info.filename,
                folder=_channel_folder(inner, channel),
                size=info.file_size,
                crc32=info.CRC,
            )
        )
    return found


def _loose(path: Path) -> Recording:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return Recording(path=path, folder=_channel_folder(path.parent.name), size=size)


def _expand(sources, on_error) -> list[Path]:
    """Resolve the command line: paths as given, globs expanded here.

    The globs are expanded in-process because the archive lives on a mount whose paths have
    spaces in them and the shell is not always the one quoting them.
    """
    paths: list[Path] = []
    for source in sources:
        text = str(source)
        if MAGIC_RE.search(text) and not Path(text).exists():
            matches = sorted(globlib.glob(text, recursive=True))
            if not matches and on_error is not None:
                on_error(Path(text), "no such file, and the glob matched nothing")
            paths.extend(Path(match) for match in matches)
        else:
            paths.append(Path(text))
    return paths


def iter_recordings(
    *sources,
    suffixes: tuple[str, ...] = DEFAULT_SUFFIXES,
    newest_first: bool = False,
    on_error=None,
) -> list[Recording]:
    """Every recording under every source, oldest first unless told otherwise.

    Sorted by the timestamp in the member's own name, not by mtime and not per archive: the
    order is the order the calls happened in, however the files were zipped or copied
    around.  ``on_error(path, message)`` is called for a source that does not exist and for
    an archive that will not open; neither is fatal.
    """
    found: list[Recording] = []
    for path in _expand(sources, on_error):
        if path.is_dir():
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for filename in sorted(filenames):
                    child = Path(dirpath) / filename
                    if filename.lower().endswith(ARCHIVE_SUFFIX):
                        found.extend(_zip_members(child, suffixes, on_error))
                    elif _is_recording(filename, suffixes):
                        found.append(_loose(child))
        elif path.is_file() and path.name.lower().endswith(ARCHIVE_SUFFIX):
            found.extend(_zip_members(path, suffixes, on_error))
        elif path.is_file():
            if _is_recording(path.name, suffixes):
                found.append(_loose(path))
        elif on_error is not None:
            on_error(path, "no such file or directory")
    found.sort(key=lambda rec: rec.sort_key, reverse=newest_first)
    return found


# --------------------------------------------------------------------------------------
# what the bytes themselves say
# --------------------------------------------------------------------------------------

#: layer III bitrate tables, kb/s, indexed by the header's 4-bit field
_BITRATES_MPEG1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BITRATES_MPEG2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
#: keyed by the header's version field: 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _syncsafe(raw: bytes) -> int:
    value = 0
    for byte in raw:
        value = (value << 7) | (byte & 0x7F)
    return value


def _id3_end(blob: bytes) -> int:
    """Where the ID3v2 tag stops and the audio starts (0 when there is no tag)."""
    if len(blob) < 10 or blob[:3] != b"ID3":
        return 0
    end = 10 + _syncsafe(blob[6:10])
    if blob[5] & 0x10:  # a footer, ID3v2.4 only
        end += 10
    return min(end, len(blob))


def _id3_frame(blob: bytes, frame_id: bytes) -> str | None:
    """One text frame, decoded.  Enough ID3 to read a title; mutagen is not available here."""
    if len(blob) < 10 or blob[:3] != b"ID3":
        return None
    major = blob[3]
    if major not in (3, 4):
        return None
    end = _id3_end(blob)
    offset = 10
    if blob[5] & 0x40:  # an extended header, whose own size is the first field
        extended = _syncsafe(blob[offset : offset + 4]) if major == 4 else (
            4 + int.from_bytes(blob[offset : offset + 4], "big")
        )
        offset += max(0, extended)
    while offset + 10 <= end:
        name = blob[offset : offset + 4]
        if not name.strip(b"\x00"):  # the padding that follows the last frame
            return None
        raw = blob[offset + 4 : offset + 8]
        size = _syncsafe(raw) if major == 4 else int.from_bytes(raw, "big")
        offset += 10
        if size <= 0 or offset + size > end:
            return None
        if name == frame_id:
            return _id3_text(blob[offset : offset + size])
        offset += size
    return None


def _id3_text(payload: bytes) -> str | None:
    encodings = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}
    encoding = encodings.get(payload[0] if payload else 0, "latin-1")
    try:
        text = payload[1:].decode(encoding, "replace")
    except (LookupError, UnicodeDecodeError):  # pragma: no cover - 'replace' handles it
        return None
    return text.split("\x00")[0].strip() or None


def id3_alias(blob: bytes) -> str | None:
    """The talkgroup alias sdrtrunk quotes in ``TIT2`` (``24801"48 FIRE OPS"``)."""
    title = _id3_frame(blob, b"TIT2")
    if not title:
        return None
    match = re.search(r'"([^"]*)"', title)
    if match is None:
        return None
    return match.group(1).strip() or None


def mp3_duration(blob: bytes) -> float | None:
    """Seconds of audio, by walking the MPEG frame headers.

    The same count mutagen arrives at, without mutagen: every layer III frame carries a
    fixed number of samples, so summing them is exact for CBR and VBR alike.  ``None`` when
    there is no frame to read — a truncated member, or something that is not an MP3.
    """
    offset = _id3_end(blob)
    limit = len(blob)
    frames = 0
    seconds = 0.0
    while offset + 4 <= limit:
        if blob[offset] != 0xFF or (blob[offset + 1] & 0xE0) != 0xE0:
            if frames:
                break  # a trailing ID3v1/APE tag, not a hole in the stream
            offset += 1
            continue
        header = blob[offset : offset + 4]
        version = (header[1] >> 3) & 0b11
        layer = (header[1] >> 1) & 0b11
        bitrate_index = (header[2] >> 4) & 0b1111
        rate_index = (header[2] >> 2) & 0b11
        padding = (header[2] >> 1) & 0b1
        if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
            offset += 1
            continue
        table = _BITRATES_MPEG1 if version == 3 else _BITRATES_MPEG2
        bitrate = table[bitrate_index] * 1000
        sample_rate = _SAMPLE_RATES[version][rate_index]
        samples = 1152 if version == 3 else 576
        length = (samples // 8) * bitrate // sample_rate + padding
        if length <= 4:
            break
        frames += 1
        seconds += samples / sample_rate
        offset += length
    return seconds if frames else None


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------


class Ledger:
    """An append-only JSONL record of what is finished, so a re-run is cheap.

    Keyed by ``archive!member`` (or the plain path of a loose file) *and* by sha256: a file
    that moved — or was zipped up since the last run — is still recognized, and a file that
    changed under the same name is not.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.done_keys: set[str] = set()
        self.done_hashes: set[str] = set()
        if path is not None and path.exists():
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn last line from a kill -9; the file is re-tried
                    if entry.get("status") in DONE_STATUSES:
                        # 'path' alone is what ledgers written before archives carried.
                        if entry.get("key") or entry.get("path"):
                            self.done_keys.add(str(entry.get("key") or entry["path"]))
                        if entry.get("sha256"):
                            self.done_hashes.add(entry["sha256"])

    def already_done(self, key: str | Path, digest: str | None = None) -> bool:
        if str(key) in self.done_keys:
            return True
        return bool(digest) and digest in self.done_hashes

    def record(self, **entry: object) -> None:
        with self._lock:
            if entry.get("status") in DONE_STATUSES:
                if entry.get("key") or entry.get("path"):
                    self.done_keys.add(str(entry.get("key") or entry["path"]))
                if entry.get("sha256"):
                    self.done_hashes.add(str(entry["sha256"]))
            if self.path is None:
                return
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
                handle.flush()


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def retry_after(self) -> float | None:
        raw = self.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    def json(self) -> dict[str, object] | None:
        try:
            value = json.loads(self.body)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None


def _multipart(fields: dict[str, str], file_field: str, filename: str, blob: bytes,
               content_type: str) -> tuple[bytes, str]:
    """Build one ``multipart/form-data`` body.  No dependency does this for us here."""
    boundary = f"----keyup{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    # The server keeps this name (safe_filename) and parses it, so it must survive verbatim.
    escaped = filename.replace('"', "")
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{escaped}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    out += blob
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _request(url: str, *, data: bytes | None = None, content_type: str | None = None,
             timeout: float = 120.0) -> Response:
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - our own URL
            body = answer.read().decode("utf-8", "replace")
            return Response(answer.status, body, {k.lower(): v for k, v in answer.headers.items()})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return Response(exc.code, body, {k.lower(): v for k, v in (exc.headers or {}).items()})


# --------------------------------------------------------------------------------------
# the uploader
# --------------------------------------------------------------------------------------


@dataclass
class Stats:
    uploaded: int = 0
    duplicate: int = 0
    failed: int = 0
    skipped: int = 0
    waited_s: float = 0.0
    #: bytes of audio the server accepted (uploads and duplicates alike)
    sent_bytes: int = 0
    #: the subset of ``duplicate`` the precheck caught, i.e. uploads never spent
    precheck_duplicate: int = 0

    @property
    def done(self) -> int:
        return self.uploaded + self.duplicate + self.skipped


class Uploader:
    """Pushes recordings at Keyup, obeying its flow control.

    One instance is shared by every worker thread: the rate limiter, the pause gate and the
    counters have to be process-wide or ``--rate-per-min`` and ``--workers`` fight.
    """

    def __init__(
        self,
        *,
        base_url: str,
        key: str,
        priority: str = "backlog",
        system: str | None = None,
        rate_per_min: float = 0.0,
        max_attempts: int = 6,
        timeout: float = 120.0,
        send_datetime: bool = False,
        datetime_is_end: bool = False,
        dry_run: bool = False,
        ledger: Ledger | None = None,
        note: str | None = None,
        precheck: bool = True,
        sleep=time.sleep,  # noqa: ANN001 - injected in the tests
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.priority = priority
        self.system = system
        self.rate_per_min = rate_per_min
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.send_datetime = send_datetime
        self.datetime_is_end = datetime_is_end
        self.dry_run = dry_run
        self.ledger = ledger or Ledger(None)
        self.note = note
        #: cleared for the rest of the run the first time the server shows it has no
        #: /upload/exists (404) or will not answer it — the upload path is the fallback
        #: and it is correct on its own, so this is never worth failing a run over.
        self.use_precheck = precheck
        self.sleep = sleep
        self.stats = Stats()
        self._lock = threading.Lock()
        self._next_slot = 0.0
        #: set while the server says it is not accepting backlog; one thread polls, the
        #: rest wait on it rather than all hammering /upload/flow at once.
        self._gate = threading.Event()
        self._gate.set()
        self._gate_lock = threading.Lock()

    # ------------------------------------------------------------------ pacing

    def _throttle(self) -> None:
        if self.rate_per_min <= 0:
            return
        interval = 60.0 / self.rate_per_min
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_slot)
            self._next_slot = start + interval
        if start > now:
            self.sleep(start - now)

    def flow(self) -> dict[str, object] | None:
        """``GET /api/upload/flow`` — the gauge, or ``None`` when it cannot be read."""
        url = f"{self.base_url}/api/upload/flow?key={urllib.parse.quote(self.key)}"
        try:
            answer = _request(url, timeout=min(30.0, self.timeout))
        except OSError:
            return None
        if answer.status != 200:
            return None
        return answer.json()

    def wait_until_accepting(self, *, poll_s: float = 15.0, max_wait_s: float = 3600.0) -> None:
        """Block while the server is refusing backlog.

        Only the first thread to notice actually polls; the others wait on the gate, so a
        paused migration is one request every ``poll_s`` no matter how many workers there
        are.
        """
        if not self._gate.is_set():
            self._gate.wait(max_wait_s)
            return
        gauge = self.flow()
        if gauge is None:
            return  # no gauge: fall back to obeying 429s, which is the real contract anyway
        accepting = gauge.get("accepting") or {}
        if isinstance(accepting, dict) and accepting.get("backlog", True):
            return
        if not self._gate_lock.acquire(blocking=False):
            self._gate.wait(max_wait_s)
            return
        try:
            self._gate.clear()
            waited = 0.0
            while waited < max_wait_s:
                self.sleep(poll_s)
                waited += poll_s
                with self._lock:
                    self.stats.waited_s += poll_s
                gauge = self.flow()
                if gauge is None:
                    break
                accepting = gauge.get("accepting") or {}
                if not isinstance(accepting, dict) or accepting.get("backlog", True):
                    break
        finally:
            self._gate.set()
            self._gate_lock.release()

    # ------------------------------------------------------------------ precheck

    def precheck(self, digests: list[str]) -> set[str]:
        """``POST /api/upload/exists`` — which of these the archive already holds.

        The endpoint answers from an index on ``calls.sha256``, so asking is a few
        milliseconds and being told "yes" saves a whole multipart upload of bytes the
        server would read in full only to answer ``duplicate call rejected``.  Anything
        unexpected — an older server with no such endpoint, a hiccup, a body that will not
        parse — returns what is known so far and lets the upload path do its job: the
        precheck is an optimization, never the thing correctness rests on.
        """
        present: set[str] = set()
        if not self.use_precheck or self.dry_run:
            return present
        url = f"{self.base_url}/api/upload/exists?key={urllib.parse.quote(self.key)}"
        wanted = [d for d in dict.fromkeys(digests) if d]
        for start in range(0, len(wanted), PRECHECK_BATCH):
            chunk = wanted[start : start + PRECHECK_BATCH]
            payload = json.dumps({"sha256": chunk}).encode()
            try:
                answer = _request(url, data=payload, content_type="application/json",
                                  timeout=min(60.0, self.timeout))
            except (OSError, urllib.error.URLError, http.client.HTTPException):
                return present
            if answer.status in (401, 403, 404, 405, 422):
                # No endpoint (older server), a guard that will not let a bare token past
                # (403), or one that will not take our question:
                # stop asking for the rest of the run rather than paying for the answer.
                self.use_precheck = False
                return present
            if answer.status != 200:
                return present
            body = answer.json() or {}
            found = body.get("present")
            if not isinstance(found, list):
                return present
            present.update(str(item) for item in found)
        return present

    # ------------------------------------------------------------------ one recording

    def fields_for(self, rec: Recording, parts: NameParts, blob: bytes | None) -> dict[str, str]:
        fields = {
            "key": self.key,
            "priority": self.priority,
            "audioName": rec.name,
            "audioType": mimetypes.guess_type(rec.name)[0] or "audio/mpeg",
        }
        if self.system:
            fields["systemLabel"] = self.system
        if parts.talkgroup is not None:
            fields["talkgroup"] = str(parts.talkgroup)
        if parts.source is not None:
            fields["source"] = str(parts.source)
        # The channel folder is the *last* alias resort: talkgroupLabel outranks the ID3
        # alias on the push path, so it is only sent when nothing better exists.
        if rec.folder and not parts.alias and not (blob and id3_alias(blob)):
            fields["talkgroupLabel"] = rec.folder
        if self.send_datetime and parts.stamp:
            # Opt-in: see the module docstring.  --datetime-is-end turns the stamp from the
            # instant the file was closed into the instant the call started.
            epoch = time.mktime(time.strptime(parts.stamp, "%Y%m%d_%H%M%S"))
            if self.datetime_is_end and blob is not None:
                epoch -= mp3_duration(blob) or 0.0
            fields["dateTime"] = str(round(epoch))
        return fields

    def upload(self, rec: Recording, blob: bytes | None = None) -> tuple[str, str]:
        """Push one recording.  Returns ``(status, detail)`` with status in
        ``uploaded|duplicate|skipped|failed``."""
        parts = parse_name(rec.name)
        if parts.talkgroup is None:
            # Without a talkgroup the endpoint answers "incomplete call data: no talkgroup"
            # and stores nothing, so there is no point spending the upload.
            return "skipped", "no talkgroup in the filename"
        fields = self.fields_for(rec, parts, blob)
        if self.dry_run:
            return "skipped", f"dry-run: {fields.get('talkgroup')} {rec.name}"

        if blob is None:
            blob = rec.read_bytes()
        body, content_type = _multipart(fields, "audio", rec.name, blob, fields["audioType"])
        url = f"{self.base_url}/api/call-upload"

        for attempt in range(1, self.max_attempts + 1):
            self.wait_until_accepting()
            self._throttle()
            try:
                answer = _request(url, data=body, content_type=content_type, timeout=self.timeout)
            except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
                # HTTPException covers a reply the client could not even parse — seen as
                # "got more than 100 headers" while the edge proxy restarts mid-deploy.
                # It is a transient like a refused connection, not a verdict on the file.
                if attempt == self.max_attempts:
                    return "failed", f"{type(exc).__name__}: {exc}"
                self._backoff(attempt)
                continue

            text = answer.body.strip()
            if answer.status == 200 and DUPLICATE_BODY in text:
                return "duplicate", text
            if answer.status == 200 and OK_BODY in text:
                return "uploaded", text
            if answer.status in RETRY_STATUSES:
                if attempt == self.max_attempts:
                    return "failed", f"{answer.status}: {text[:200]}"
                delay = answer.retry_after
                with self._lock:
                    self.stats.waited_s += delay or 0.0
                if delay:
                    self.sleep(min(MAX_SLEEP, delay))
                else:
                    self._backoff(attempt)
                continue
            # 200 with an unexpected body (an sdrtrunk-dialect complaint), 400, 401, 413:
            # retrying will produce exactly the same answer.
            return "failed", f"{answer.status}: {text[:200]}"
        return "failed", "out of attempts"  # pragma: no cover - the loop returns first

    def _backoff(self, attempt: int) -> None:
        delay = min(MAX_SLEEP, 2.0**attempt) * (0.5 + random.random() / 2)  # noqa: S311
        with self._lock:
            self.stats.waited_s += delay
        self.sleep(delay)


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------


def run(uploader: Uploader, files: list[Recording], *, workers: int = 2, progress=None) -> Stats:  # noqa: ANN001
    """Upload every recording with ``workers`` threads, writing the ledger as it goes.

    Each worker takes a **batch** (``PRECHECK_BATCH``), reads and hashes its members one at
    a time, asks ``POST /api/upload/exists`` which of those hashes the archive already
    holds, and only uploads the rest — a call is tens of kilobytes and an archive is a
    gigabyte, so a batch of held bytes is a few megabytes and nothing here scales with the
    size of the archive.  With the precheck off (``--no-precheck``, ``--dry-run``, or a
    server that has no such endpoint) the batch is one recording and this is the loop it
    always was.  The blob is hashed exactly once either way: the digest the precheck asks
    about is the digest the ledger records.
    """
    work: queue.Queue[Recording] = queue.Queue()
    for rec in files:
        work.put(rec)
    started = time.monotonic()
    total = len(files)
    counter_lock = threading.Lock()

    def finish(rec: Recording, status: str, detail: str, *, digest: str | None = None,
               size: int | None = None) -> None:
        with counter_lock:
            setattr(uploader.stats, status, getattr(uploader.stats, status) + 1)
            if size and status in ("uploaded", "duplicate"):
                uploader.stats.sent_bytes += size
            if detail == PRECHECK_DETAIL:
                uploader.stats.precheck_duplicate += 1
        uploader.ledger.record(
            **rec.ledger_fields(), sha256=digest, status=status, detail=detail,
            note=uploader.note, at=time.time(),
        )

    def failed(rec: Recording, exc: Exception) -> None:
        # A corrupt member (bad CRC, truncated archive) lands here, is counted and is
        # written to the ledger as failed, so the next run tries it again.
        with counter_lock:
            uploader.stats.failed += 1
        uploader.ledger.record(
            **rec.ledger_fields(), status="failed",
            detail=f"{type(exc).__name__}: {exc}", note=uploader.note, at=time.time(),
        )

    def done(rec: Recording) -> None:  # noqa: ARG001 - symmetry with the calls above
        work.task_done()
        if progress is not None:
            progress(uploader.stats, total, time.monotonic() - started)

    def worker() -> None:
        try:
            while True:
                batch_size = PRECHECK_BATCH if uploader.use_precheck and not uploader.dry_run else 1
                batch: list[Recording] = []
                while len(batch) < batch_size:
                    try:
                        batch.append(work.get_nowait())
                    except queue.Empty:
                        break
                if not batch:
                    return
                # Read and hash first, so one question covers the whole batch.  A member
                # the ledger already knows never costs a read at all.
                pending: list[tuple[Recording, bytes | None, str | None]] = []
                for rec in batch:
                    try:
                        # The cheap check first: a key already in the ledger costs no read.
                        if uploader.ledger.already_done(rec.key):
                            with counter_lock:
                                uploader.stats.skipped += 1
                            done(rec)
                            continue
                        blob = None if uploader.dry_run else rec.read_bytes()
                        digest = None if blob is None else hashlib.sha256(blob).hexdigest()
                        if digest is not None and uploader.ledger.already_done(rec.key, digest):
                            with counter_lock:
                                uploader.stats.skipped += 1
                            done(rec)
                            continue
                        pending.append((rec, blob, digest))
                    except Exception as exc:  # noqa: BLE001 - one bad member ends nothing
                        failed(rec, exc)
                        done(rec)
                stored = uploader.precheck([d for _, _, d in pending if d])
                for rec, blob, digest in pending:
                    try:
                        if digest is not None and digest in stored:
                            # The server already holds these exact bytes; the upload would
                            # be read in full only to answer "duplicate call rejected".
                            finish(rec, "duplicate", PRECHECK_DETAIL, digest=digest)
                            continue
                        status, detail = uploader.upload(rec, blob)
                        finish(rec, status, detail, digest=digest,
                               size=None if blob is None else len(blob))
                    except Exception as exc:  # noqa: BLE001 - one bad member ends nothing
                        failed(rec, exc)
                    finally:
                        done(rec)
        finally:
            close_archives()

    threads = [threading.Thread(target=worker, name=f"upload-{i}") for i in range(max(1, workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return uploader.stats


def _progress_line(stats: Stats, total: int, elapsed: float) -> None:
    done = stats.done + stats.failed
    per_min = done / elapsed * 60 if elapsed > 0 else 0.0
    megabytes = stats.sent_bytes / 1_000_000
    mb_per_min = megabytes / elapsed * 60 if elapsed > 0 else 0.0
    eta = (total - done) / (done / elapsed) if done and elapsed > 0 else 0.0
    # "(pre M)" is the part of dup= that never cost an upload, which is the whole point of
    # the precheck: on a re-run over an archive Keyup already holds it is most of the line.
    pre = f" (pre {stats.precheck_duplicate})" if stats.precheck_duplicate else ""
    sys.stderr.write(
        f"\r{done}/{total}  ok={stats.uploaded} dup={stats.duplicate}{pre} skip={stats.skipped} "
        f"fail={stats.failed}  {megabytes:.0f} MB  {per_min:.0f}/min {mb_per_min:.1f} MB/min  "
        f"eta {eta / 60:.0f}m  waited {stats.waited_s:.0f}s "
    )
    sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_sdrtrunk.py",
        description="Upload an sdrtrunk archive (folders, per-day ZIPs, loose MP3s) to Keyup "
                    "as backlog.",
        epilog="Docs: docs/MIGRATION.md",
    )
    parser.add_argument("sources", type=str, nargs="+",
                        help="directories, .zip archives or globs to walk")
    parser.add_argument("--url", default="http://localhost:8096", help="Keyup base URL")
    parser.add_argument("--key", default=os.environ.get("KEYUP_KEY", ""),
                        help="an upload-scope token (or $KEYUP_KEY)")
    parser.add_argument("--priority", default="backlog",
                        help="urgent|live|backlog|bulk or 1-999 (default: backlog)")
    parser.add_argument("--system", default=None,
                        help="systemLabel to send; omit to let Keyup read it from the ID3/filename")
    parser.add_argument("--workers", type=int, default=2, help="upload threads (default: 2)")
    parser.add_argument("--newest-first", action="store_true",
                        help="upload the most recent recordings first (default: oldest first)")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many recordings")
    parser.add_argument("--rate-per-min", type=float, default=0.0,
                        help="cap uploads per minute across all workers (0 = uncapped)")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="resumable JSONL ledger; finished recordings are skipped on a re-run")
    parser.add_argument("--source-note", default=None,
                        help="free text recorded on every ledger entry (which share, which run)")
    parser.add_argument("--timeout", type=float, default=120.0, help="per-request timeout")
    parser.add_argument("--max-attempts", type=int, default=6, help="tries per recording")
    parser.add_argument("--send-datetime", action="store_true",
                        help="send dateTime from the filename stamp (opt-in; see --help)")
    parser.add_argument("--datetime-is-end", action="store_true",
                        help="the stamp is the call's END: subtract the audio's own duration "
                             "(implies --send-datetime)")
    parser.add_argument("--no-precheck", dest="precheck", action="store_false",
                        help="do not ask POST /api/upload/exists which hashes are already "
                             "stored; upload everything and let the server dedupe")
    parser.add_argument("--dry-run", action="store_true", help="walk and report, upload nothing")
    parser.add_argument("--quiet", action="store_true", help="no progress line")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.key and not args.dry_run:
        print("a --key (or $KEYUP_KEY) is required", file=sys.stderr)
        return 2

    bad: list[tuple[Path, str]] = []
    files = iter_recordings(
        *args.sources, newest_first=args.newest_first, on_error=lambda p, m: bad.append((p, m))
    )
    for path, message in bad:
        print(f"skipping {path}: {message}", file=sys.stderr)
    if args.limit:
        files = files[: args.limit]
    if not files and not bad:
        print(f"no recordings under {', '.join(args.sources)}", file=sys.stderr)
        return 0

    ledger = Ledger(args.ledger)
    uploader = Uploader(
        base_url=args.url,
        key=args.key,
        priority=args.priority,
        system=args.system,
        rate_per_min=args.rate_per_min,
        max_attempts=args.max_attempts,
        timeout=args.timeout,
        send_datetime=args.send_datetime or args.datetime_is_end,
        datetime_is_end=args.datetime_is_end,
        dry_run=args.dry_run,
        ledger=ledger,
        note=args.source_note,
        precheck=args.precheck,
    )
    # An archive that would not open is a failure of the run, not a reason to stop it: it is
    # counted, written to the ledger and reported in the exit code like any other.
    for path, message in bad:
        uploader.stats.failed += 1
        ledger.record(key=str(path), path=str(path), status="failed", detail=message,
                      note=args.source_note, at=time.time())

    archives = len({rec.path for rec in files if rec.member})
    gigabytes = sum(rec.size for rec in files) / 1_000_000_000
    print(f"{len(files)} recordings ({gigabytes:.1f} GB) in {archives} archives; "
          f"priority={args.priority} workers={args.workers}", file=sys.stderr)
    stats = run(uploader, files, workers=args.workers,
                progress=None if args.quiet else _progress_line)
    if not args.quiet:
        sys.stderr.write("\n")
    print(
        f"uploaded={stats.uploaded} duplicate={stats.duplicate} "
        f"(precheck {stats.precheck_duplicate}) skipped={stats.skipped} "
        f"failed={stats.failed} sent={stats.sent_bytes / 1_000_000:.0f}MB",
        file=sys.stderr,
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
