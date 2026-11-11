"""Pure parsing of one recording — filename, ID3 frames or JSON sidecar, audio header.

Two recorders write into this archive.  **sdrtrunk** (docs/ARCHITECTURE.md §1.1–1.5) is
what everything below the merge section reads; **trunk-recorder** (§1.7) writes a JSON
sidecar next to every call and is parsed in :mod:`app.ingest.tr_parser`.
:func:`build_call_record` is the dialect switch: it recognizes a trunk-recorder call by
its ``-call_<n>`` suffix or an adjacent sidecar and hands it over, so every caller —
the watcher, the scan, the upload endpoint, the ledger retry — stays dialect-blind.

Nothing here touches the database or the network, so every rule can be tested against the
real corpus (``tests/test_parser.py``, ``tests/test_corpus.py``).  The three readers are
independent and ``build_call_record`` merges them with **ID3 authoritative and the
filename as fallback**, because the filename is a lossy rendering of the same facts:
spaces become ``-``, the fully-qualified radio's dots become ``_``, and a patch is
sometimes written as its bare patch id.

Time (docs/ARCHITECTURE.md 1.2) is the subtle part.  The ``COMM`` ``Date`` and the
filename stamp are the same clock read: the instant sdrtrunk *wrote* the finished file,
which is the call's end within a second.  So ``recorded_at`` is that instant,
``ended_at`` equals it, and ``started_at`` is it minus the measured duration.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mutagen import MutagenError
from mutagen.id3 import ID3NoHeaderError
from mutagen.mp3 import MP3, HeaderNotFoundError
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

__all__ = [
    "AUDIO_SUFFIXES",
    "AudioProbe",
    "CallRecord",
    "CallSourceRef",
    "FilenameParts",
    "Id3Meta",
    "ParseError",
    "RadioRef",
    "build_call_record",
    "id3_frame_map",
    "is_keyup_artifact",
    "is_recording_name",
    "parse_comm",
    "parse_filename",
    "parse_id3",
    "parse_tit2",
    "parse_tpe1",
    "probe_audio",
]

#: Every audio extension either recorder writes.  sdrtrunk writes MP3; trunk-recorder
#: writes WAV and/or M4A per its ``audioArchive``/``compressWav`` settings (§1.7).
AUDIO_SUFFIXES = (".mp3", ".m4a", ".wav")


#: Keyup's own processed copy (``<stem>.proc.mp3``, :data:`app.audio.PROCESSED_SUFFIX`)
#: and the hidden temp file it is written through (``.proc-XXXX.mp3``) — both sit beside
#: the original, so any watched tree the audio stage has touched contains them.
_KEYUP_WRITTEN_SUFFIX = ".proc.mp3"
_KEYUP_TEMP_PREFIX = ".proc-"


def is_keyup_artifact(name: str | Path) -> bool:
    """True for an audio file Keyup itself wrote next to a recording — never a call.

    Ingesting one would make a second call out of every processed recording, and the
    bytes differ from the original, so only dedupe would stand in the way.
    """
    text = Path(name).name.lower()
    return text.endswith(_KEYUP_WRITTEN_SUFFIX) or text.startswith(_KEYUP_TEMP_PREFIX)


def is_recording_name(name: str | Path) -> bool:
    """True for a file this ingest would try to make a call out of.

    ``.raw.wav`` is trunk-recorder's undecoded baseband capture — a debugging artifact,
    not a call, and orders of magnitude larger than one.  Keyup's own processed copies
    (:func:`is_keyup_artifact`) are not calls either.
    """
    text = Path(name).name.lower()
    if text.endswith(".raw.wav") or is_keyup_artifact(text):
        return False
    return text.endswith(AUDIO_SUFFIXES)


class ParseError(ValueError):
    """A file this ingest cannot make a call out of."""


# ``YYYYMMDD_HHMMSS_<System>_<Site>_<Name>__TO_<rest>``.  ``<rest>`` is everything after
# the double underscore: the TO value, then optionally ``_FROM_<radio>``, then optionally
# sdrtrunk's ``_V<n>`` collision suffix.
#
# **The underscore after the time is optional.**  sdrtrunk builds before 2025 butt the
# system straight against the stamp — ``20240223_210414Example_System_Central_T-Control__TO_…``
# — and half a decade of the archive is written that way.  Refusing those names is what
# put a million backlog uploads on their upload date (docs/MIGRATION.md, "Timestamps"):
# the push path tries this parser, and when it raises there is no stamp left to place the
# call by.  ``sep`` is kept because it is also the dialect flag — see
# :func:`_split_sysite`.
_NAME_RE = re.compile(
    r"^(?P<date>\d{8})_(?P<time>\d{6})(?P<sep>_?)(?P<sysite>.+?)__TO_(?P<rest>.+)$"
)
#: ``P14257-[57012--50061--57011]`` — patch id and members, members joined by ``--``
_PATCH_TO_RE = re.compile(r"^P(?P<patch>\d+)-\[(?P<members>[\d\-]*)\]$")
#: sdrtrunk's second-file-in-the-same-second suffix
_VERSION_RE = re.compile(r"^(?P<stem>.*)_V(?P<version>\d+)$")
#: ``P:9230 [19282, 50051, 50061, 29201]`` at the head of a patch TIT2
_TIT2_PATCH_RE = re.compile(r"^P:(?P<patch>\d+)\s*\[(?P<members>[^\]]*)\]")
#: ``24821`` at the head of a plain TIT2
_TIT2_TG_RE = re.compile(r"^(?P<tgid>\d+)")
#: every ``"…"`` alias in a TIT2, in order
_TIT2_ALIAS_RE = re.compile(r'"([^"]*)"')


@dataclass(frozen=True)
class RadioRef:
    """A radio as the recorder addressed it.

    A unit on its home network is printed fully qualified as ``WACN.SYS.ID``
    (``101010.57.9831120`` in ID3, ``101010_57_9831120`` in the filename); a unit on the
    system we are monitoring is a bare id.  ``wacn`` and ``home_system`` are 0 for the
    bare form — they are part of the ``radios`` unique key, so a sentinel keeps the key
    usable.
    """

    raw: str
    radio_id: int | None
    wacn: int = 0
    home_system: int = 0

    @property
    def is_fully_qualified(self) -> bool:
        return self.wacn != 0 or self.home_system != 0


@dataclass(frozen=True)
class FilenameParts:
    """What the file *name* says.  Every field is a fallback for the ID3 frame."""

    name: str
    #: naive local (``TZ``) file-write stamp, second resolution
    stamp: datetime | None
    #: the ``System_Site_Name`` block, spaces still rendered as ``-``
    system: str | None
    site: str | None
    channel_name: str | None
    to_raw: str
    from_raw: str | None
    tgid: int | None = None
    patch_id: int | None = None
    member_tgids: tuple[int, ...] = ()
    radio: RadioRef | None = None
    #: sdrtrunk's ``_V2`` collision suffix, as an int (None when absent)
    version: int | None = None
    #: the ``System_Site_Name`` block exactly as the name spells it, before the split
    #: (:func:`_split_sysite`) guessed where the system ends.  The ingest layer needs the
    #: unsplit form to re-split a legacy name against the systems that actually exist.
    sysite: str | None = None
    #: True when the name is the pre-2025 dialect: no underscore after the time, and the
    #: system's spaces rendered as underscores.  Those are the only names whose split is
    #: a guess (:func:`app.ingest.service.resolve_legacy_sysite`).
    legacy: bool = False

    @property
    def is_patch(self) -> bool:
        return self.patch_id is not None


@dataclass(frozen=True)
class Id3Meta:
    """What the ID3 frames say — authoritative for everything they carry."""

    #: ``COMM`` ``Date`` with milliseconds, naive local
    date: datetime | None = None
    system: str | None = None
    site: str | None = None
    channel_name: str | None = None
    decoder: str | None = None
    channel: str | None = None
    frequency_hz: int | None = None
    tgid: int | None = None
    patch_id: int | None = None
    member_tgids: tuple[int, ...] = ()
    #: every alias in TIT2, in order (patch alias first for a patch)
    aliases: tuple[str, ...] = ()
    talkgroup_alias: str | None = None
    #: the patch's own name ("PATCH 9230"); None when TIT2 does not print one
    patch_alias: str | None = None
    #: member tgid -> its alias, for a patch
    member_aliases: dict[int, str] = field(default_factory=dict)
    radio: RadioRef | None = None
    recorder_version: str | None = None
    comm: dict[str, str] = field(default_factory=dict)
    #: every frame as read, keyed by its ID3 key ('TIT2', 'COMM::eng') — kept verbatim on
    #: the call as ``calls.raw_meta`` so the detail page can show what was actually there
    frames: dict[str, str] = field(default_factory=dict)

    @property
    def is_patch(self) -> bool:
        return self.patch_id is not None


@dataclass(frozen=True)
class AudioProbe:
    """What the MP3 itself says, plus the bytes' identity."""

    duration_s: float
    bitrate: int | None
    sample_rate: int | None
    channels: int | None
    size: int
    sha256: str
    mtime: datetime


@dataclass(frozen=True)
class CallSourceRef:
    """One radio keying up inside one call — a ``call_sources`` row before it has an id.

    ``pos_s`` is seconds into the recording, so the UI can seek to it; ``len_s`` is how
    long that radio held the channel, which the sidecar does not state and the parser
    derives from the next source's position (or the end of the call, for the last one).
    """

    src: int | None
    raw: str
    pos_s: float = 0.0
    len_s: float | None = None
    emergency: bool = False
    tag: str | None = None
    seq: int = 0


@dataclass(frozen=True)
class CallRecord:
    """Everything ``ingest_file`` needs, already merged and in the right time zone."""

    file_path: str  # relative to AUDIO_ROOT
    file_name: str
    folder_alias: str | None

    system: str
    site: str | None
    channel_name: str | None
    decoder: str | None
    channel: str | None
    frequency_hz: int | None

    tgid: int | None
    talkgroup_ids: tuple[int, ...]
    patch_id: int | None
    member_tgids: tuple[int, ...]
    member_aliases: dict[int, str]
    talkgroup_alias: str | None
    call_type: str  # 'group' | 'patch' | 'individual'

    radio: RadioRef | None
    radio_resolved: bool

    recorded_at: datetime  # tz-aware UTC — the file-write instant
    started_at: datetime  # recorded_at - duration
    ended_at: datetime  # == recorded_at
    started_local: datetime  # naive local time (TZ)
    time_source: str

    duration_s: float
    bitrate: int | None
    sample_rate: int | None
    channels: int | None
    file_size: int
    sha256: str
    file_mtime: datetime
    recorder_version: str | None

    filename_parts: FilenameParts
    id3: Id3Meta

    # ---- fields only the trunk-recorder dialect (§1.7) and the push path fill in -------
    #: ``calls.ingest_source``: 'file' for an sdrtrunk file, 'trunk_recorder' for a sidecar
    ingest_source: str = "file"
    #: 'present' for a normal call; 'purged' for an encrypted metadata-only sidecar, which
    #: is kept because it still proves an agency was on the air (§1.7)
    audio_state: str = "present"
    emergency: bool | None = None
    encrypted: bool | None = None
    #: 'analog' | 'digital' | 'digital tdma'
    audio_type: str | None = None
    signal_db: int | None = None
    noise_db: int | None = None
    freq_error: int | None = None
    recorder_call_num: int | None = None
    #: ``talkgroups.group_name`` — trunk-recorder's ``talkgroup_group`` (the category)
    talkgroup_group: str | None = None
    #: ``talkgroups.description`` — trunk-recorder's ``talkgroup_description``
    talkgroup_description: str | None = None
    #: alias hint for the primary radio, from the first ``srcList`` entry's ``tag``
    radio_alias: str | None = None
    #: every keying radio, in order; sdrtrunk yields at most one, at position 0
    sources: tuple[CallSourceRef, ...] = ()
    #: the recorder's own metadata verbatim — the whole sidecar, or the ID3 frame map
    raw_meta: dict[str, Any] = field(default_factory=dict)
    #: **push path only** — where ``started_at`` actually came from, which ``time_source``
    #: cannot say: the endpoint stamps every push ``upload`` whether the recorder sent a
    #: ``dateTime`` or the server had to invent one.  ``app.api.call_upload`` sets it to
    #: one of its ``TIME_BASIS_*`` values and refuses a backlog push whose time is the
    #: last resort, which is the guard the million-row backlog mis-stamp went in through.
    time_basis: str | None = None
    #: ``calls.kind`` — ``'group'`` (a talkgroup call, patched or not) or ``'unit'`` (a P25
    #: private call, one radio to one radio, on no talkgroup at all).  Only the push path
    #: ever sets it: no filename and no sidecar carries the fact.
    kind: str = "group"
    #: **unit calls only** — the radio id the private call was addressed *to*, as the air
    #: carried it.  ``store_upload`` resolves it to a ``radios`` row the same way it
    #: resolves the source.
    target_radio_id: int | None = None


# --------------------------------------------------------------------------------------
# filename
# --------------------------------------------------------------------------------------


def _int_or_none(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _parse_radio_token(raw: str, sep: str) -> RadioRef | None:
    """``101010_57_9831120`` / ``101010.57.9831120`` / ``9250057`` -> RadioRef."""
    raw = raw.strip()
    if not raw:
        return None
    parts = raw.split(sep)
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return RadioRef(raw=raw, radio_id=int(parts[2]), wacn=int(parts[0]), home_system=int(parts[1]))
    if len(parts) == 1 and parts[0].isdigit():
        return RadioRef(raw=raw, radio_id=int(parts[0]))
    # An unexpected shape is recorded verbatim rather than dropped or guessed at.
    return RadioRef(raw=raw, radio_id=None)


def _split_sysite(sysite: str, *, legacy: bool) -> tuple[str | None, str | None, str | None]:
    """``System_Site_Name`` -> the three of them, in whichever dialect wrote it.

    The modern build renders a space as ``-`` (``Example-System_Central_T-Control``), so the
    block is exactly three underscore-separated tokens and anything longer belongs to the
    channel name, which is where a stray underscore would sit.

    The pre-2025 build — the one that also omits the underscore after the stamp, which is
    how it is recognized — renders a space as ``_`` instead, so the *system* is what
    grew extra tokens: ``Example_System_Central_T-Control`` is "Example System" on the
    "Central" site's "T-Control" channel, not a system called "Example".  Site and channel
    are one token each in *most* files of that era, so the split counts from the right and
    the leading tokens are the system, re-joined with ``-`` — the rendering the rest of
    this module already reads as a space (:func:`build_call_record`).  Getting this wrong
    is not cosmetic: ``upsert_system`` would mint a *second* system named "Example" beside
    the real one, which is exactly the shape of damage the phantom ``upload`` system did.

    **Counting from the right is a guess, and one of the four legacy shapes in the
    production archive breaks it.**  ``EXAMPLE_SCHOOL_DISTRICT_Eastside_District_Schools``
    is the *EXAMPLE SCHOOL DISTRICT* system, Eastside site, "District Schools"
    channel — a two-token channel — and from the right alone this reads it as a system
    called "… DISTRICT Eastside" on a site called "District": exactly the phantom-system damage
    again.  Only the database knows which prefix is a real system name, so the ingest layer
    re-splits a legacy block against the systems and sites that exist
    (:func:`app.ingest.service.resolve_legacy_sysite`) and keeps this as the fallback for
    a name it does not recognize.
    """
    tokens = sysite.split("_")
    if legacy and len(tokens) > 3:
        return "-".join(tokens[:-2]), tokens[-2], tokens[-1]
    system = tokens[0] if tokens else None
    site = tokens[1] if len(tokens) > 1 else None
    channel_name = "_".join(tokens[2:]) if len(tokens) > 2 else None
    return system, site, channel_name


def parse_filename(name: str) -> FilenameParts:
    """Parse ``YYYYMMDD_HHMMSS_<System>_<Site>_<Name>__TO_<TO>[_FROM_<FROM>][_V2].mp3``.

    The underscore after the stamp is optional: pre-2025 sdrtrunk writes
    ``20240223_210414Example_System_Central_T-Control__TO_1102_FROM_9226020.mp3``, and its
    absence is also what says the system's spaces are underscores rather than dashes
    (:func:`_split_sysite`).

    Raises :class:`ParseError` when the name does not have the date-time prefix and the
    ``__TO_`` marker; everything past that point degrades to ``None`` rather than failing,
    because an unrecognized TO or FROM is still a call worth keeping.
    """
    name = Path(name).name
    stem = name[:-4] if name.lower().endswith(".mp3") else name

    match = _NAME_RE.match(stem)
    if not match:
        raise ParseError(f"not an sdrtrunk recording name: {name!r}")

    rest = match["rest"]
    version: int | None = None
    if (vm := _VERSION_RE.match(rest)) is not None:
        version = int(vm["version"])
        rest = vm["stem"]

    if "_FROM_" in rest:
        to_raw, from_raw = rest.split("_FROM_", 1)
    else:
        to_raw, from_raw = rest, None

    try:
        stamp: datetime | None = datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S")
    except ValueError:
        stamp = None

    legacy = not match["sep"]
    system, site, channel_name = _split_sysite(match["sysite"], legacy=legacy)

    tgid = patch_id = None
    members: tuple[int, ...] = ()
    if (pm := _PATCH_TO_RE.match(to_raw)) is not None:
        patch_id = int(pm["patch"])
        members = tuple(int(m) for m in pm["members"].split("--") if m.isdigit())
    else:
        tgid = _int_or_none(to_raw)

    return FilenameParts(
        name=name,
        stamp=stamp,
        system=system,
        site=site,
        channel_name=channel_name,
        to_raw=to_raw,
        from_raw=from_raw,
        tgid=tgid,
        patch_id=patch_id,
        member_tgids=members,
        radio=_parse_radio_token(from_raw, "_") if from_raw else None,
        version=version,
        sysite=match["sysite"],
        legacy=legacy,
    )


# --------------------------------------------------------------------------------------
# ID3
# --------------------------------------------------------------------------------------


def parse_comm(value: str) -> dict[str, str]:
    """``Date:…;System:…;Site:…;`` -> dict.  Values may contain ``:`` (the timestamp does)."""
    out: dict[str, str] = {}
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        key, _, val = chunk.partition(":")
        out[key.strip()] = val.strip()
    return out


def parse_tit2(value: str) -> tuple[int | None, int | None, tuple[int, ...], tuple[str, ...]]:
    """Split a ``TIT2`` into ``(tgid, patch_id, member_tgids, aliases)``.

    Two shapes exist::

        24821"EC EMS OPS"
        P:9230 [19282, 50051, 50061, 29201]"PATCH 9230","STATE VLAW31 PATCH",…

    For a patch the first alias is the patch's own and the rest line up with the members.
    """
    aliases = tuple(_TIT2_ALIAS_RE.findall(value))
    if (pm := _TIT2_PATCH_RE.match(value)) is not None:
        members = tuple(int(m) for m in re.findall(r"\d+", pm["members"]))
        return None, int(pm["patch"]), members, aliases
    if (tm := _TIT2_TG_RE.match(value)) is not None:
        return int(tm["tgid"]), None, (), aliases
    return None, None, (), aliases


def parse_tpe1(value: str) -> RadioRef | None:
    """``101010.57.9831120`` or ``9250057`` -> RadioRef."""
    return _parse_radio_token(value, ".")


def _first_text(tags, key: str) -> str | None:  # noqa: ANN001
    frame = tags.get(key) if tags is not None else None
    if frame is None:
        return None
    texts = getattr(frame, "text", None)
    if not texts:
        return None
    return str(texts[0])


def id3_frame_map(tags) -> dict[str, str]:  # noqa: ANN001
    """Every ID3 frame flattened to ``key -> text``, in file order.

    The values are joined with ``; `` on the rare multi-text frame; nothing here is parsed
    back, so a faithful rendering is the whole job.
    """
    if tags is None:
        return {}
    out: dict[str, str] = {}
    for key in list(tags):
        frame = tags[key]
        texts = getattr(frame, "text", None)
        out[str(key)] = "; ".join(str(t) for t in texts) if texts else str(frame)
    return out


def _comm_text(tags) -> str | None:  # noqa: ANN001
    """``COMM`` is keyed ``COMM::eng`` (description + language), so scan for the prefix."""
    if tags is None:
        return None
    for key in list(tags):
        if key == "COMM" or key.startswith("COMM:"):
            texts = getattr(tags[key], "text", None)
            if texts:
                return str(texts[0])
    return None


def parse_id3(path: str | Path) -> Id3Meta:
    """Read the ID3 frames sdrtrunk writes.  A file without tags yields an empty Id3Meta."""
    try:
        audio = MP3(str(path))
    except (HeaderNotFoundError, ID3NoHeaderError):
        return Id3Meta()
    tags = audio.tags
    if tags is None:
        return Id3Meta()

    comm = parse_comm(_comm_text(tags) or "")

    date: datetime | None = None
    if raw_date := comm.get("Date"):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                date = datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                continue

    tgid = patch_id = None
    members: tuple[int, ...] = ()
    aliases: tuple[str, ...] = ()
    if title := _first_text(tags, "TIT2"):
        tgid, patch_id, members, aliases = parse_tit2(title)

    # A patch TIT2 lists the members' aliases, usually behind the patch's own name
    # ("PATCH 9230") but not always: the P1409x family in the corpus prints exactly one
    # alias per member and no patch name.  The count is what tells the two apart.
    member_aliases: dict[int, str] = {}
    patch_alias: str | None = None
    if patch_id is not None and members:
        if len(aliases) == len(members) + 1:
            patch_alias = aliases[0]
            member_aliases = dict(zip(members, aliases[1:], strict=True))
        elif len(aliases) == len(members):
            member_aliases = dict(zip(members, aliases, strict=True))

    #: what to record on the call: the patch's own name when it has one, else whatever
    #: alias came first (which is the primary member's).
    talkgroup_alias = patch_alias or (aliases[0] if aliases else None)

    radio = None
    if performer := _first_text(tags, "TPE1"):
        radio = parse_tpe1(performer)

    frequency = comm.get("Frequency", "")

    return Id3Meta(
        date=date,
        system=comm.get("System") or _first_text(tags, "TIT1"),
        site=comm.get("Site"),
        channel_name=comm.get("Name") or _first_text(tags, "TALB"),
        decoder=comm.get("Decoder"),
        channel=comm.get("Channel"),
        frequency_hz=int(frequency) if frequency.isdigit() else None,
        tgid=tgid,
        patch_id=patch_id,
        member_tgids=members,
        aliases=aliases,
        talkgroup_alias=talkgroup_alias,
        patch_alias=patch_alias,
        member_aliases=member_aliases,
        radio=radio,
        recorder_version=_first_text(tags, "TCOM"),
        comm=comm,
        frames=id3_frame_map(tags),
    )


# --------------------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------------------


def sha256_file(path: str | Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


#: extension -> the mutagen reader for it.  All three expose the same ``info`` attributes
#: (``length``, ``bitrate``, ``sample_rate``, ``channels``), so one code path reads them.
_MUTAGEN_READERS = {".mp3": MP3, ".m4a": MP4, ".mp4": MP4, ".wav": WAVE}


def _ffprobe_duration(path: Path) -> float | None:
    """Ask ffmpeg how long the file is — the fallback when mutagen will not read it.

    trunk-recorder's M4A comes out of whatever AAC encoder the host had, and a WAV whose
    writer was killed mid-call has a length field that lies; ffprobe decodes rather than
    trusting a header, so it answers where mutagen gives up.  Returns None when ffprobe is
    not installed or cannot make sense of the file either.
    """
    binary = shutil.which("ffprobe")
    if binary is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, the path is the only variable
            [binary, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        duration = float(out.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def probe_audio(path: str | Path) -> AudioProbe:
    """Duration/bitrate/rate/channels for an MP3, M4A or WAV, plus size, mtime and sha256.

    mutagen reads all three headers; when it refuses — a truncated file, an M4A from an
    encoder it does not know — ffprobe decodes the duration instead, because a call with
    an unknown bitrate is still a call and one with an unknown *duration* is not usable.
    """
    path = Path(path)
    stat = path.stat()
    reader = _MUTAGEN_READERS.get(path.suffix.lower())
    duration: float | None = None
    bitrate = sample_rate = channels = None

    if reader is not None:
        try:
            info = reader(str(path)).info
            duration = float(info.length)
            bitrate = int(info.bitrate) if getattr(info, "bitrate", None) else None
            sample_rate = int(info.sample_rate) if getattr(info, "sample_rate", None) else None
            channels = int(info.channels) if getattr(info, "channels", None) else None
        except (HeaderNotFoundError, ID3NoHeaderError, MutagenError, OSError, ValueError):
            duration = None

    if not duration:
        duration = _ffprobe_duration(path)
    if duration is None:
        raise ParseError(f"unreadable audio: {path.name}")

    return AudioProbe(
        duration_s=duration,
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        size=stat.st_size,
        sha256=sha256_file(path),
        mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


# --------------------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------------------


def _localize(naive: datetime, tz: ZoneInfo) -> datetime:
    """Attach the recorder's zone to a naive local stamp and convert to UTC."""
    return naive.replace(tzinfo=tz).astimezone(UTC)


def build_call_record(
    path: str | Path,
    audio_root: str | Path,
    *,
    tz: str = "UTC",
    prefer_audio: str = "m4a",
) -> CallRecord:
    """Merge whatever the recorder left behind into the record ``ingest_file`` writes.

    **The dialect switch.**  A trunk-recorder call announces itself by its ``-call_<n>``
    filename suffix or by a ``.json`` sidecar sharing its stem (§1.7), and is handed to
    :mod:`app.ingest.tr_parser`, where the sidecar is authoritative.  Everything else is
    an sdrtrunk file and is read here.

    For sdrtrunk: ID3 wins wherever it has a value; the filename fills the gaps.
    ``recorded_at`` comes from the ``COMM`` ``Date`` (milliseconds), or the filename stamp
    (whole seconds), or — if a file somehow has neither — the file's mtime.
    """
    path = Path(path)
    from app.ingest import tr_parser  # noqa: PLC0415 - it imports CallRecord from here

    if tr_parser.is_trunk_recorder_call(path):
        return tr_parser.build_tr_call_record(
            path, audio_root, tz=tz, prefer_audio=prefer_audio
        )

    audio_root = Path(audio_root)
    zone = ZoneInfo(tz)

    parts = parse_filename(path.name)
    id3 = parse_id3(path)
    probe = probe_audio(path)

    try:
        rel = str(path.resolve().relative_to(audio_root.resolve()))
    except ValueError:
        rel = str(path)

    # The alias folder is informational (docs/ARCHITECTURE.md 1.1): the parser records it
    # and never depends on it.
    folder_alias = path.parent.name if path.parent != audio_root else None

    if id3.date is not None:
        recorded_at = _localize(id3.date, zone)
        time_source = "file_write"
    elif parts.stamp is not None:
        recorded_at = _localize(parts.stamp, zone)
        time_source = "file_write"
    else:  # pragma: no cover - no file in the corpus reaches this
        recorded_at = probe.mtime
        time_source = "file_write"

    duration = probe.duration_s
    ended_at = recorded_at
    started_at = recorded_at - timedelta(seconds=duration)
    started_local = started_at.astimezone(zone).replace(tzinfo=None)

    system = id3.system or (parts.system.replace("-", " ") if parts.system else None)
    if not system:
        raise ParseError(f"no system name in ID3 or filename: {path.name}")

    patch_id = id3.patch_id if id3.patch_id is not None else parts.patch_id
    members = id3.member_tgids or parts.member_tgids
    tgid = id3.tgid if id3.tgid is not None else parts.tgid

    if patch_id is not None:
        call_type = "patch"
        talkgroup_ids = members
        # The primary talkgroup of a patch is its first member (docs 3.2).
        primary_tgid = members[0] if members else tgid
    else:
        call_type = "group"
        talkgroup_ids = (tgid,) if tgid is not None else ()
        primary_tgid = tgid

    radio = id3.radio or parts.radio

    return CallRecord(
        file_path=rel,
        file_name=path.name,
        folder_alias=folder_alias,
        system=system,
        site=id3.site or parts.site,
        channel_name=id3.channel_name or parts.channel_name,
        decoder=id3.decoder,
        channel=id3.channel,
        frequency_hz=id3.frequency_hz,
        tgid=primary_tgid,
        talkgroup_ids=talkgroup_ids,
        patch_id=patch_id,
        member_tgids=members,
        member_aliases=dict(id3.member_aliases),
        talkgroup_alias=id3.talkgroup_alias or folder_alias,
        call_type=call_type,
        radio=radio,
        radio_resolved=radio is not None and radio.radio_id is not None,
        recorded_at=recorded_at,
        started_at=started_at,
        ended_at=ended_at,
        started_local=started_local,
        time_source=time_source,
        duration_s=duration,
        bitrate=probe.bitrate,
        sample_rate=probe.sample_rate,
        channels=probe.channels,
        file_size=probe.size,
        sha256=probe.sha256,
        file_mtime=probe.mtime,
        recorder_version=id3.recorder_version,
        filename_parts=parts,
        id3=id3,
        # sdrtrunk can only ever name the *first* radio on a transmission, so the one
        # source it yields sits at offset 0 and spans the call — which is exactly what a
        # trunk-recorder single-source sidecar produces, so the two read the same way.
        sources=(
            (
                CallSourceRef(
                    src=radio.radio_id,
                    raw=radio.raw,
                    pos_s=0.0,
                    len_s=round(duration, 3),
                    tag=None,
                    seq=0,
                ),
            )
            if radio is not None and radio.radio_id is not None
            else ()
        ),
        raw_meta=dict(id3.frames),
    )
