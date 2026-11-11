"""trunk-recorder's dialect: the ``.json`` sidecar and the filename around it (§1.7).

trunk-recorder writes, per call::

    <captureDir>/<shortName>/<YYYY>/<M>/<D>/<tg>-<epoch>.<ms>_<freqHz>[.<slot>]-call_<n>.{wav,m4a,json}

The month and day are **not** zero-padded, and a custom ``filenameFormat`` may rewrite the
stem entirely — but ``-call_<n>.<ext>`` is always appended, which is what
:func:`is_trunk_recorder_call` recognizes.  The sidecar is the point of the whole dialect:
where sdrtrunk leaves a file-write timestamp accurate to a second and one radio id, this
JSON states the call's true start and stop to the millisecond, whether it was an emergency,
whether it was encrypted, which talkgroups a patch covered, and — ``srcList`` — every radio
that keyed up, with its offset into the audio.

Three rules are worth knowing before reading the code:

**The sidecar is authoritative.**  When one exists, nothing is inferred from the filename
or measured from the audio: ``started_at`` is ``start_time_ms``, ``duration_s`` is
``call_length_ms``, and ``time_source`` is ``sidecar``.  Without one — the recorder was
killed between writing the audio and writing the JSON, or a custom format dropped it — the
filename and the audio header answer instead and ``time_source`` falls back to
``file_write``, which is honest about being approximate.

**Their JSON field names are shifted against their CSV.**  ``talkgroup_group_tag`` is the
*alpha tag* (the short display name, "RVPD DISP"), ``talkgroup_tag`` is the *service tag*
("Fire Dispatch") and ``talkgroup_group`` is the *category* ("Riverton").  So the alias
this archive shows comes from ``talkgroup_group_tag`` and the group name from
``talkgroup_group`` — reading them the way their names suggest puts "Fire Dispatch" in
front of every user as if it were a channel name.

**An encrypted call has a sidecar and no audio.**  It is still ingested: it proves an
agency was on that talkgroup at that second, which is exactly what a CAD integration
asks.  The call is written with ``audio_state='purged'`` and ``file_path`` pointing
at the JSON, so the audio route answers 410 "the recording has been purged" rather than
inventing a file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.ingest.parser import (
    AUDIO_SUFFIXES,
    AudioProbe,
    CallRecord,
    CallSourceRef,
    FilenameParts,
    Id3Meta,
    ParseError,
    RadioRef,
    probe_audio,
    sha256_file,
)

__all__ = [
    "SIDECAR_SUFFIX",
    "TrFilenameParts",
    "TrMeta",
    "TrSource",
    "audio_for_sidecar",
    "build_tr_call_record",
    "is_trunk_recorder_call",
    "is_trunk_recorder_name",
    "parse_tr_filename",
    "parse_tr_sidecar",
    "sidecar_for",
    "stem_of",
]

SIDECAR_SUFFIX = ".json"

#: The one thing trunk-recorder appends whatever ``filenameFormat`` says.
_CALL_SUFFIX_RE = re.compile(r"-call_(\d+)$")

#: The default stem: ``1101-1756483200.123_852137500.1-call_7``.  Everything after the
#: talkgroup degrades to None rather than failing — a custom format still gives us a call.
_TR_STEM_RE = re.compile(
    r"^(?P<tg>\d+)-(?P<epoch>\d+)(?:\.(?P<ms>\d+))?_(?P<freq>\d+)(?:\.(?P<slot>\d+))?-call_(?P<num>\d+)$"
)


def stem_of(path: str | Path) -> str:
    """The name with its recording extension removed — ``.json``, ``.wav``, ``.m4a``…"""
    name = Path(path).name
    lowered = name.lower()
    for suffix in (*AUDIO_SUFFIXES, SIDECAR_SUFFIX):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def is_trunk_recorder_name(path: str | Path) -> bool:
    """True when the *name alone* says trunk-recorder wrote this."""
    return _CALL_SUFFIX_RE.search(stem_of(path)) is not None


def sidecar_for(path: str | Path) -> Path:
    """The ``.json`` that would sit beside this recording (whether or not it exists)."""
    path = Path(path)
    return path.with_name(stem_of(path) + SIDECAR_SUFFIX)


def audio_for_sidecar(sidecar: str | Path, *, prefer: str = "m4a") -> Path | None:
    """The audio file beside a sidecar, or None for an encrypted metadata-only call.

    Both a ``.wav`` and a ``.m4a`` exist when trunk-recorder is told to keep the original
    alongside the compressed copy.  They are the same call, so exactly one is ingested:
    ``TR_PREFER_AUDIO`` picks which, defaulting to the M4A because it is a tenth the size
    and every browser plays it.
    """
    sidecar = Path(sidecar)
    stem = stem_of(sidecar)
    order = [f".{prefer.strip().lower().lstrip('.')}"]
    order += [s for s in AUDIO_SUFFIXES if s not in order]
    for suffix in order:
        candidate = sidecar.with_name(stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def is_trunk_recorder_call(path: str | Path) -> bool:
    """The dialect test :func:`app.ingest.parser.build_call_record` switches on.

    Either the name carries ``-call_<n>``, or a sidecar sits beside the file — the second
    half is what lets a custom ``filenameFormat`` still be recognized.
    """
    path = Path(path)
    if path.suffix.lower() == SIDECAR_SUFFIX:
        return True
    return is_trunk_recorder_name(path) or sidecar_for(path).is_file()


# --------------------------------------------------------------------------------------
# the sidecar
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrSource:
    """One ``srcList`` entry, before the parser works out how long it lasted."""

    src: int | None
    pos: float
    emergency: bool = False
    tag: str | None = None
    signal_system: str | None = None


@dataclass(frozen=True)
class TrMeta:
    """One parsed sidecar.  Every field is optional: the JSON is what the recorder felt
    like writing, and a missing key must degrade rather than fail an ingest."""

    path: Path
    short_name: str | None = None
    talkgroup: int | None = None
    patched_talkgroups: tuple[int, ...] = ()
    #: the alpha tag — ``talkgroup_group_tag`` in their JSON, not ``talkgroup_tag``
    talkgroup_alias: str | None = None
    #: the service tag ("Fire Dispatch"), kept for the raw panel
    talkgroup_tag: str | None = None
    talkgroup_group: str | None = None
    talkgroup_description: str | None = None
    frequency_hz: int | None = None
    freq_error: int | None = None
    signal_db: int | None = None
    noise_db: int | None = None
    call_num: int | None = None
    recorder_num: int | None = None
    source_num: int | None = None
    tdma_slot: int | None = None
    phase2_tdma: bool | None = None
    color_code: int | None = None
    mode: str | None = None
    duplex: int | None = None
    priority: int | None = None
    emergency: bool | None = None
    encrypted: bool | None = None
    audio_type: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float | None = None
    sources: tuple[TrSource, ...] = ()
    freq_list: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_patch(self) -> bool:
        return bool(self.patched_talkgroups)


def _int(value: Any) -> int | None:
    """The sidecar prints ints as ints and occasionally as strings; both are ints here."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool | None:
    """trunk-recorder writes flags as ``0``/``1``; be liberal about true/false too."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    number = _int(value)
    if number is not None:
        return number != 0
    return str(value).strip().lower() in {"true", "yes", "on"}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _epoch(seconds: Any, millis: Any) -> datetime | None:
    """Prefer the millisecond field; fall back to whole seconds.  Both are UTC epochs."""
    ms = _int(millis)
    if ms:
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    secs = _int(seconds)
    if secs:
        return datetime.fromtimestamp(secs, tz=UTC)
    return None


def parse_tr_sidecar(json_path: str | Path) -> TrMeta:
    """Read one ``.json`` sidecar.  Raises :class:`ParseError` when it is not JSON."""
    json_path = Path(json_path)
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParseError(f"unreadable trunk-recorder sidecar {json_path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ParseError(f"trunk-recorder sidecar {json_path.name} is not an object")

    sources: list[TrSource] = []
    for entry in raw.get("srcList") or ():
        if not isinstance(entry, dict):
            continue
        sources.append(
            TrSource(
                src=_int(entry.get("src")),
                pos=_float(entry.get("pos")) or 0.0,
                emergency=bool(_flag(entry.get("emergency"))),
                # ``tag`` is the recorder's alias for the unit; ``tag_ota`` is the talker
                # alias the radio itself transmitted, which is the better name when present.
                tag=_text(entry.get("tag_ota")) or _text(entry.get("tag")),
                signal_system=_text(entry.get("signal_system")),
            )
        )
    sources.sort(key=lambda s: s.pos)

    duration = _float(raw.get("call_length_ms"))
    duration = duration / 1000.0 if duration else _float(raw.get("call_length"))

    freq_list = tuple(f for f in (raw.get("freqList") or ()) if isinstance(f, dict))

    return TrMeta(
        path=json_path,
        short_name=_text(raw.get("short_name")),
        talkgroup=_int(raw.get("talkgroup")),
        patched_talkgroups=tuple(
            t for t in (_int(p) for p in raw.get("patched_talkgroups") or ()) if t is not None
        ),
        # §1.7: the alpha tag is ``talkgroup_group_tag``, not ``talkgroup_tag``.
        talkgroup_alias=_text(raw.get("talkgroup_group_tag")),
        talkgroup_tag=_text(raw.get("talkgroup_tag")),
        talkgroup_group=_text(raw.get("talkgroup_group")),
        talkgroup_description=_text(raw.get("talkgroup_description")),
        frequency_hz=_int(raw.get("freq")),
        freq_error=_int(raw.get("freq_error")),
        signal_db=_int(raw.get("signal")),
        noise_db=_int(raw.get("noise")),
        call_num=_int(raw.get("call_num")),
        recorder_num=_int(raw.get("recorder_num")),
        source_num=_int(raw.get("source_num")),
        tdma_slot=_int(raw.get("tdma_slot")),
        phase2_tdma=_flag(raw.get("phase2_tdma")),
        color_code=_int(raw.get("color_code")),
        mode=_text(raw.get("mode")),
        duplex=_int(raw.get("duplex")),
        priority=_int(raw.get("priority")),
        emergency=_flag(raw.get("emergency")),
        encrypted=_flag(raw.get("encrypted")),
        audio_type=_text(raw.get("audio_type")),
        started_at=_epoch(raw.get("start_time"), raw.get("start_time_ms")),
        ended_at=_epoch(raw.get("stop_time"), raw.get("stop_time_ms")),
        duration_s=duration,
        sources=tuple(sources),
        freq_list=freq_list,
        raw=raw,
    )


# --------------------------------------------------------------------------------------
# the filename (the fallback when no sidecar arrived)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrFilenameParts:
    """What a trunk-recorder path says without opening anything."""

    name: str
    #: ``<captureDir>/<shortName>/<Y>/<M>/<D>/`` — the system, four directories up
    short_name: str | None = None
    talkgroup: int | None = None
    started_at: datetime | None = None
    frequency_hz: int | None = None
    tdma_slot: int | None = None
    call_num: int | None = None


def parse_tr_filename(path: str | Path) -> TrFilenameParts:
    """``1101-1756483200.123_852137500.1-call_7.m4a`` -> its parts.

    The system name is the directory four levels above the file (``<shortName>/Y/M/D/``),
    which is the only place a path states it.  A custom ``filenameFormat`` that does not
    match leaves every field but the name and the system as None, and the caller falls
    back to probing the audio.
    """
    path = Path(path)
    parents = path.parents
    short_name = parents[3].name if len(parents) > 3 else None

    match = _TR_STEM_RE.match(stem_of(path))
    if match is None:
        return TrFilenameParts(name=path.name, short_name=short_name)

    epoch = int(match["epoch"])
    # ``.123`` is milliseconds and ``.1`` is a tenth of a second, so the fraction is padded
    # to three digits rather than read as an integer count of milliseconds.
    fraction = match["ms"]
    epoch_seconds = epoch + (int(fraction.ljust(3, "0")[:3]) / 1000.0 if fraction else 0.0)

    return TrFilenameParts(
        name=path.name,
        short_name=short_name,
        talkgroup=int(match["tg"]),
        started_at=datetime.fromtimestamp(epoch_seconds, tz=UTC),
        frequency_hz=int(match["freq"]),
        tdma_slot=int(match["slot"]) if match["slot"] else None,
        call_num=int(match["num"]),
    )


# --------------------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------------------


def _call_sources(meta: TrMeta | None, duration: float) -> tuple[CallSourceRef, ...]:
    """``srcList`` -> ``call_sources`` rows, each one's length derived from the next.

    The sidecar says when each radio *started*; how long it held the channel is the
    distance to the next one, and for the last it is whatever is left of the call.  A
    position past the end of the audio (the recorder logged a source after the last
    sample) yields no length rather than a negative one.
    """
    if meta is None or not meta.sources:
        return ()
    out: list[CallSourceRef] = []
    for index, entry in enumerate(meta.sources):
        following = meta.sources[index + 1].pos if index + 1 < len(meta.sources) else duration
        length = following - entry.pos
        out.append(
            CallSourceRef(
                src=entry.src,
                raw=str(entry.src) if entry.src is not None else "",
                pos_s=round(entry.pos, 3),
                len_s=round(length, 3) if length > 0 else None,
                emergency=entry.emergency,
                tag=entry.tag,
                seq=index,
            )
        )
    return tuple(out)


def build_tr_call_record(  # noqa: PLR0912, PLR0915 - one merge, read top to bottom
    path: str | Path,
    audio_root: str | Path,
    *,
    tz: str = "UTC",
    prefer_audio: str = "m4a",
) -> CallRecord:
    """Build the record ``ingest_file`` writes from a trunk-recorder call.

    ``path`` may be the audio or the sidecar; either way the pair is resolved first, so a
    scan that trips over the ``.json`` and a watcher that saw the ``.m4a`` produce the same
    row and the same ``file_path``, and the ledger's idempotence holds.
    """
    path = Path(path)
    audio_root = Path(audio_root)
    zone = ZoneInfo(tz)

    if path.suffix.lower() == SIDECAR_SUFFIX:
        sidecar: Path | None = path
        audio = audio_for_sidecar(path, prefer=prefer_audio)
    else:
        audio = path
        candidate = sidecar_for(path)
        sidecar = candidate if candidate.is_file() else None

    meta = parse_tr_sidecar(sidecar) if sidecar is not None else None
    parts = parse_tr_filename(audio or sidecar or path)

    # The row's identity is the audio when there is any, and the sidecar when the call was
    # encrypted and never produced a sample.
    subject = audio if audio is not None else sidecar
    if subject is None:  # pragma: no cover - one of the two always exists to get here
        raise ParseError(f"neither audio nor sidecar for {path.name}")

    probe: AudioProbe | None = None
    if audio is not None:
        probe = probe_audio(audio)

    duration = meta.duration_s if meta is not None and meta.duration_s else None
    if not duration and probe is not None:
        duration = probe.duration_s
    if not duration and meta is not None and meta.started_at and meta.ended_at:
        duration = (meta.ended_at - meta.started_at).total_seconds()
    if not duration:
        raise ParseError(f"no duration for {subject.name}")

    if meta is not None and meta.started_at is not None:
        started_at = meta.started_at
        time_source = "sidecar"
    elif parts.started_at is not None:
        # No sidecar arrived: the filename's epoch is the call start the recorder chose,
        # but nothing corroborates it, so it is labeled as approximate as a file write.
        started_at = parts.started_at
        time_source = "file_write"
    elif probe is not None:
        started_at = probe.mtime - timedelta(seconds=duration)
        time_source = "file_write"
    else:  # pragma: no cover - a sidecar with no time and no audio has nothing to place it
        raise ParseError(f"no start time for {subject.name}")

    ended_at = (
        meta.ended_at
        if meta is not None and meta.ended_at is not None
        else started_at + timedelta(seconds=duration)
    )

    system = (meta.short_name if meta is not None else None) or parts.short_name
    if not system:
        raise ParseError(f"no system (short_name) for {subject.name}")

    tgid = (meta.talkgroup if meta is not None else None) or parts.talkgroup
    members = tuple(meta.patched_talkgroups) if meta is not None else ()
    if members:
        # A patch: the call was on ``talkgroup`` and carried the patched ones too, so the
        # primary stays what the radio was actually keyed to and the array holds them all.
        talkgroup_ids = tuple(dict.fromkeys([*(t for t in (tgid,) if t is not None), *members]))
        call_type = "patch"
    elif tgid is not None:
        talkgroup_ids, call_type = (tgid,), "group"
    else:
        talkgroup_ids, call_type = (), "group"

    sources = _call_sources(meta, duration)
    radio: RadioRef | None = None
    radio_alias: str | None = None
    if sources and sources[0].src is not None:
        radio = RadioRef(raw=sources[0].raw, radio_id=sources[0].src)
        radio_alias = sources[0].tag

    try:
        rel = str(subject.resolve().relative_to(audio_root.resolve()))
    except ValueError:
        rel = str(subject)

    encrypted = meta.encrypted if meta is not None else None
    # An encrypted call has a sidecar and no sample.  The row is worth keeping — it proves
    # an agency held that talkgroup at that second — so it is written with the retention
    # vocabulary for "the metadata is right and the bytes are not there" (§1.7).
    audio_state = "present" if audio is not None else "purged"

    raw_meta: dict[str, Any] = dict(meta.raw) if meta is not None else {}
    if meta is None:
        raw_meta = {"filename": parts.name, "note": "no sidecar; parsed from the filename"}

    return CallRecord(
        file_path=rel,
        file_name=subject.name,
        folder_alias=None,
        system=system,
        # trunk-recorder records one system per capture directory and no site or channel
        # name; ``recorder_num`` is the closest thing it has to a channel and goes in the
        # raw metadata rather than pretending to be sdrtrunk's ``Channel``.
        site=None,
        channel_name=None,
        decoder=(meta.mode if meta is not None else None),
        channel=None,
        frequency_hz=(meta.frequency_hz if meta is not None else None) or parts.frequency_hz,
        tgid=tgid,
        talkgroup_ids=talkgroup_ids,
        # trunk-recorder never gives a patch an id of its own — it just lists the members —
        # so ``calls.patch_id`` stays null and the member set on ``talkgroup_ids`` is what
        # answers "calls involving talkgroup X" (§1.4).
        patch_id=None,
        member_tgids=members,
        member_aliases={},
        talkgroup_alias=(meta.talkgroup_alias if meta is not None else None),
        call_type=call_type,
        radio=radio,
        radio_resolved=radio is not None and radio.radio_id is not None,
        # There is no separate file-write fact here the way sdrtrunk's ID3 Date is one:
        # the call ended when the sidecar says it ended.
        recorded_at=ended_at,
        started_at=started_at,
        ended_at=ended_at,
        started_local=started_at.astimezone(zone).replace(tzinfo=None),
        time_source=time_source,
        duration_s=duration,
        bitrate=probe.bitrate if probe is not None else None,
        sample_rate=probe.sample_rate if probe is not None else None,
        channels=probe.channels if probe is not None else None,
        file_size=probe.size if probe is not None else subject.stat().st_size,
        sha256=probe.sha256 if probe is not None else sha256_file(subject),
        file_mtime=(
            probe.mtime
            if probe is not None
            else datetime.fromtimestamp(subject.stat().st_mtime, tz=UTC)
        ),
        recorder_version=None,
        filename_parts=FilenameParts(
            name=subject.name,
            stamp=started_at.astimezone(zone).replace(tzinfo=None),
            system=system,
            site=None,
            channel_name=None,
            to_raw=str(tgid) if tgid is not None else "",
            from_raw=radio.raw if radio is not None else None,
            tgid=tgid,
            member_tgids=members,
            radio=radio,
        ),
        id3=Id3Meta(),
        ingest_source="trunk_recorder",
        audio_state=audio_state,
        emergency=(meta.emergency if meta is not None else None),
        encrypted=encrypted,
        audio_type=(meta.audio_type if meta is not None else None),
        signal_db=(meta.signal_db if meta is not None else None),
        noise_db=(meta.noise_db if meta is not None else None),
        freq_error=(meta.freq_error if meta is not None else None),
        recorder_call_num=(meta.call_num if meta is not None else None) or parts.call_num,
        talkgroup_group=(meta.talkgroup_group if meta is not None else None),
        talkgroup_description=(meta.talkgroup_description if meta is not None else None),
        radio_alias=radio_alias,
        sources=sources,
        raw_meta=raw_meta,
    )
