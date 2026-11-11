"""Stored recording → the audio an export service actually wants.

Both services want a small AAC file, not our stored MP3, so every upload goes through one
ffmpeg pass.  That pass carries the **same** ``-af`` loudness filter the transcription path
uses (:func:`app.stt.normalize.ffmpeg_audio_filter`) — scanner traffic is wildly uneven in
level, and there is no reason a listener on OpenMHz should get the raw dynamic range when
the transcriber does not.  Like the STT path, this is a *preprocessing* step: the stored
MP3, its ``sha256`` and the dedupe are never touched, only the bytes handed to the service.

The filter is best-effort in exactly the way :func:`app.stt.normalize.transcode_to_wav`
makes it: if ffmpeg fails *with* the filter the encode is retried once without it and the
call is uploaded un-normalized, because a missing ffmpeg filter is not a reason to drop a
call on the floor.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # noqa: S404 - ffmpeg is a declared system dependency, argv is never a shell string
import tempfile
import threading
from pathlib import Path

from app.stt.normalize import af_flags, note_filter_failure

log = logging.getLogger("app.exporters.transcode")

__all__ = [
    "TranscodeError",
    "aac_encoder",
    "transcode_to_m4a",
    "transcode_to_mp3",
    "mp3_silence",
    "CALLS_MP3_SAMPLE_RATE",
    "CALLS_MP3_BITRATES",
]

#: Both services publish voice; 16 kHz mono at 32 kbit/s is what trunk-recorder's own
#: uploaders send and is indistinguishable from the source on P25 vocoder audio.
SAMPLE_RATE = 16_000
BITRATE = "32k"

#: Broadcastify's audio-format spec: "Encode at approximately 32 kb/s with an 18 kHz
#: cutoff using libfdk_aac or equivalent."  The cutoff is passed to the encoder rather than
#: implied by the sample rate, because they are not the same knob: at
#: :data:`SAMPLE_RATE` the Nyquist limit is already 8 kHz, so ``-cutoff 18000`` is a no-op
#: *there* and becomes load-bearing the moment somebody raises the rate.  Sending it always
#: is how the encode matches the spec at whatever rate it is run at.
AAC_CUTOFF_HZ = 18_000

#: The MP3 alternative the spec also accepts, "32 kb/s mono 22050 Hz CBR, or 16 kb/s mono
#: 22050 Hz CBR".  Both bitrates, one sample rate, mono, constant — the same encoder flags
#: the feed uses, at the Calls format instead of the mount's.
CALLS_MP3_SAMPLE_RATE = 22_050
CALLS_MP3_BITRATES = (32, 16)


class TranscodeError(RuntimeError):
    """ffmpeg could not produce an m4a for this recording."""


def _stderr(proc: subprocess.CompletedProcess[bytes]) -> str:
    return (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]


#: Memoized answer to "does this ffmpeg have libfdk_aac?".  ``None`` until asked once.
_aac_encoder: str | None = None
_encoder_lock = threading.Lock()


def aac_encoder(*, timeout_s: float = 10.0) -> str:
    """``"libfdk_aac"`` when this ffmpeg has it, else ``"aac"``.  Probed once, then cached.

    Broadcastify names libfdk_aac by name ("using libfdk_aac or equivalent"), and it is
    audibly better than ffmpeg's built-in at 32 kbit/s — but it is GPL-incompatible, so a
    distribution ffmpeg almost never carries it and a build that does is the exception.
    Hence a probe rather than a setting: an operator should not have to know which ffmpeg
    their box shipped with, and the fallback is the "or equivalent" the spec allows for.

    ``ffmpeg -hide_banner -encoders`` is one cheap process and the answer cannot change
    without the binary changing, so it is asked once per process.  Any failure — no ffmpeg,
    a timeout, an unreadable listing — resolves to ``aac``, which always exists.
    """
    global _aac_encoder
    with _encoder_lock:
        if _aac_encoder is not None:
            return _aac_encoder
    found = "aac"
    if shutil.which("ffmpeg") is not None:
        try:
            proc = subprocess.run(  # noqa: S603
                ["ffmpeg", "-hide_banner", "-v", "quiet", "-encoders"],  # noqa: S607
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
            if proc.returncode == 0 and b"libfdk_aac" in (proc.stdout or b""):
                found = "libfdk_aac"
        except (OSError, subprocess.SubprocessError):
            log.debug("export: could not probe ffmpeg for libfdk_aac; using the built-in aac encoder")
    with _encoder_lock:
        _aac_encoder = found
    log.debug("export: AAC encoder is %s", found)
    return found


def transcode_to_m4a(
    path: Path | str,
    *,
    af: str | None = None,
    timeout_s: float = 120.0,
    bitrate: str = BITRATE,
    cutoff_hz: int = AAC_CUTOFF_HZ,
) -> bytes:
    """Encode ``path`` to mono AAC-in-MP4 bytes, loudness-normalized when ``af`` is set.

    ``-movflags +faststart`` puts the moov atom first so a player can start on the first
    bytes; ``-map_metadata -1`` drops the recorder's ID3 frames rather than re-publishing
    whatever they happen to contain.  ``-cutoff`` and the encoder choice come from
    Broadcastify's audio-format spec (:data:`AAC_CUTOFF_HZ`, :func:`aac_encoder`).
    """
    source = Path(path)
    if shutil.which("ffmpeg") is None:
        raise TranscodeError("ffmpeg is not installed; it is required to encode an export")
    encoder = aac_encoder()
    with tempfile.NamedTemporaryFile(suffix=".m4a") as tmp:
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-i", str(source), *af_flags(af),
            "-map_metadata", "-1",
            "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", encoder, "-b:a", bitrate,
            "-cutoff", str(int(cutoff_hz)),
            "-movflags", "+faststart",
            "-f", "mp4", tmp.name,
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603
        except FileNotFoundError as exc:  # pragma: no cover - guarded by the which() above
            raise TranscodeError("ffmpeg is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise TranscodeError(f"ffmpeg timed out after {timeout_s:.0f}s on {source.name}") from exc
        if proc.returncode != 0:
            if af:  # the filter is the likely culprit — try again clean before giving up
                note_filter_failure(af, _stderr(proc))
                return transcode_to_m4a(
                    source, af=None, timeout_s=timeout_s, bitrate=bitrate, cutoff_hz=cutoff_hz
                )
            raise TranscodeError(f"ffmpeg failed on {source.name}: {_stderr(proc)}")
        data = Path(tmp.name).read_bytes()
    if not data:
        raise TranscodeError(f"ffmpeg produced an empty file for {source.name}")
    return data


# --------------------------------------------------------------------------------------
# the live feed's format (docs/EXPORTERS.md, "Broadcastify Feed")
# --------------------------------------------------------------------------------------
#
# The Calls exporter uploads a *file* per call and AAC is the best small container for
# that.  The Feed exporter is an Icecast source, and Icecast carries an MP3 bitstream:
# every call has to come out as CBR MP3 at exactly the mount's bitrate and sample rate, so
# that one call's frames, the next call's frames and the silence between them concatenate
# into a single legal stream a listener's decoder never notices the joins in.
#
# Hence the flags below that the m4a encode does not need: ``-write_xing 0`` (a Xing/LAME
# header is a *file* header and would land mid-stream), ``-id3v2_version 0`` and
# ``-map_metadata -1`` (no tags at all), and an explicit ``-b:a`` with no VBR.

#: What Broadcastify's own feed documentation asks for on a voice feed, and what the
#: settings default to: 16 kbit/s mono at 22.05 kHz.  MPEG-2 layer III, 576-sample frames.
FEED_BITRATE_KBPS = 16
FEED_SAMPLE_RATE = 22_050


def _mp3_flags(bitrate_kbps: int, sample_rate: int, *, xing: bool = False) -> list[str]:
    """CBR, mono, at the given rate.  ``-b:a`` with no ``-q:a`` **is** libmp3lame's CBR mode.

    ``xing`` writes the Xing/LAME frame.  Off for the feed, where the encode is a slice of
    one long bitstream and a file header landing mid-stream would be a bug; on for the
    Broadcastify **Calls** MP3, which is a standalone file that a player wants to be able
    to read a duration out of.
    """
    return [
        "-map_metadata", "-1",
        "-ac", "1", "-ar", str(int(sample_rate)),
        "-c:a", "libmp3lame", "-b:a", f"{int(bitrate_kbps)}k",
        "-write_xing", "1" if xing else "0", "-id3v2_version", "0",
        "-f", "mp3",
    ]  # fmt: skip


def transcode_to_mp3(
    path: Path | str,
    *,
    af: str | None = None,
    bitrate_kbps: int = FEED_BITRATE_KBPS,
    sample_rate: int = FEED_SAMPLE_RATE,
    timeout_s: float = 120.0,
    xing: bool = False,
) -> bytes:
    """Encode ``path`` to a CBR mono MP3 at the given format — the feed's, or Calls'.

    Broadcastify Calls accepts "32 kb/s mono 22050 Hz CBR, or 16 kb/s mono 22050 Hz CBR"
    (:data:`CALLS_MP3_BITRATES`, :data:`CALLS_MP3_SAMPLE_RATE`), and the feed wants the
    mount's own bitrate and rate; both are the same encode with different numbers, so this
    is one function with arguments rather than two that drift apart.

    Same best-effort filter contract as :func:`transcode_to_m4a`: a failure *with* ``af``
    is retried once without it rather than costing the call.
    """
    source = Path(path)
    if shutil.which("ffmpeg") is None:
        raise TranscodeError("ffmpeg is not installed; it is required to encode the feed")
    with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
        cmd = [
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-i", str(source), *af_flags(af),
            *_mp3_flags(bitrate_kbps, sample_rate, xing=xing), tmp.name,
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603
        except FileNotFoundError as exc:  # pragma: no cover - guarded by the which() above
            raise TranscodeError("ffmpeg is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise TranscodeError(f"ffmpeg timed out after {timeout_s:.0f}s on {source.name}") from exc
        if proc.returncode != 0:
            if af:
                note_filter_failure(af, _stderr(proc))
                return transcode_to_mp3(
                    source,
                    af=None,
                    bitrate_kbps=bitrate_kbps,
                    sample_rate=sample_rate,
                    timeout_s=timeout_s,
                    xing=xing,
                )
            raise TranscodeError(f"ffmpeg failed on {source.name}: {_stderr(proc)}")
        data = Path(tmp.name).read_bytes()
    if not data:
        raise TranscodeError(f"ffmpeg produced an empty file for {source.name}")
    return data


def mp3_silence(
    *,
    seconds: float = 1.0,
    bitrate_kbps: int = FEED_BITRATE_KBPS,
    sample_rate: int = FEED_SAMPLE_RATE,
    timeout_s: float = 30.0,
) -> bytes:
    """``seconds`` of encoded silence in the feed's exact format.

    An Icecast mount drops a source that stops writing, so the gaps between calls — which
    on a quiet talkgroup are most of the day — have to be *sent*, not waited out.  The
    bytes are generated once and their frames are then cycled forever, so this costs one
    ffmpeg run per connection rather than one per gap.
    """
    if shutil.which("ffmpeg") is None:
        raise TranscodeError("ffmpeg is not installed; it is required to encode the feed")
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r={int(sample_rate)}:cl=mono",
        "-t", f"{max(0.1, float(seconds)):.3f}",
        *_mp3_flags(bitrate_kbps, sample_rate), "pipe:1",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError("ffmpeg timed out generating the feed's silence") from exc
    if proc.returncode != 0 or not proc.stdout:
        raise TranscodeError(f"ffmpeg could not generate silence: {_stderr(proc)}")
    return proc.stdout
