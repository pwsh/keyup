"""The level an outbound service is sent at — separate from the level we store.

The stored file is normalized to ``audio_target_lufs`` (-16 LUFS), and that number is
chosen for **this** app's player, which runs its own playback compressor on top of it.
Every other consumer gets the file with no compressor behind it, and at -16 LUFS the
result is audibly quiet next to what OpenMHz and a Broadcastify feed carry from other
sources.  Raising the stored target to fix the exports made the in-app player far too
loud, because the compressor then had 5 dB more to work with — so the export level is its
own setting (``export_target_lufs``, -11 LUFS by default) applied **at export time, on top
of the stored file**, and the archive is left where the player wants it.

The stage is appended to whatever ``-af`` the encode already carried
(:func:`app.stt.normalize.ffmpeg_audio_filter`, which runs only on an unprocessed
original) rather than replacing it, and it always ends on a limiter so the lift cannot clip
somebody else's decoder — at -3 dBFS, with ``alimiter``'s auto-level turned off.  Both
halves of that were wrong until they were not: auto-level made the limiter push every
export *up* onto its ceiling, and a ceiling of -1 left no room for the overshoot the lossy
encode on the way out adds back.  See :data:`EXPORT_TRUE_PEAK_DB`.

Two branches, exactly as :mod:`app.audioproc.chain` has for the stored copy:

* **``loudnorm``**, single pass, for anything at least :data:`SHORT_CLIP_S` long.  A second
  measuring pass would double the ffmpeg cost of every upload to place a 2-10 s clip
  perhaps a decibel better; that is not a trade worth making on the export path.
* **a measured ``volume=``** for a clip shorter than that.  EBU R128 integrates over a
  400 ms window with a gating threshold, and on a second and a half of speech it either
  refuses to move the level or moves it by the wrong amount; measuring the mean with
  ``volumedetect`` and applying the difference is both cheaper and more predictable.  The
  same pass reports the clip's peak, which caps the difference so the lift lands under the
  ceiling rather than being clamped back down to it.

Setting ``export_target_lufs`` to **0** disables the whole stage — no loudnorm, no volume,
no limiter — and the exports go out exactly as they did before it existed.  (Clearing the
field in the Admin page is not that: a blank deletes the override row and resets the
setting to its default, as it does for every other key.)
"""

from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess  # noqa: S404 - ffmpeg is a declared system dependency, argv is never a shell string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.audioproc.chain import AudioConfig, tone_envelope
from app.audioproc.tones import ToneSegment

log = logging.getLogger("app.exporters.loudness")

__all__ = [
    "EXPORT_LRA",
    "EXPORT_TRUE_PEAK_DB",
    "LIMITER_STAGE",
    "SHORT_CLIP_S",
    "MeasuredLevels",
    "compose",
    "export_filter",
    "loudness_stage",
    "measured_levels",
    "measured_mean_db",
    "target_lufs",
    "tone_envelope_for",
]

#: The peak ceiling the export chain ends on.
#:
#: -1 dBTP is what both services' own guidance quotes, and it was wrong here for the same
#: two reasons the stored chain was (:mod:`app.audioproc.chain`): the number the limiter
#: enforces is a **sample** peak on the PCM going into the encoder, and the file that leaves
#: is lossy — AAC for OpenMHz, MP3 for the Feed — so the decoder on the far end rebuilds a
#: waveform 1.5-2 dB above the one it was handed and clips at full scale.  -3 dB is that
#: headroom.  It is shallower than the stored chain's -4 because the export is a higher
#: bitrate at a higher sample rate, where the overshoot is smaller, and because these files
#: have no player-side make-up gain behind them.
EXPORT_TRUE_PEAK_DB = -3.0

#: ``alimiter`` takes a linear amplitude, not decibels: 10 ** (-3/20) = 0.707946.
#:
#: ``level=disabled`` matters as much as the ceiling.  ffmpeg's ``alimiter`` defaults to
#: ``level=1`` — "auto level" — which *normalizes the output up* so the loudest sample lands
#: exactly on ``limit``.  A stage meant to catch an overshoot instead guaranteed one on
#: every single export, whatever the clip came in at.  Off, the filter only attenuates.
#: The 5 ms / 50 ms times are the filter's own defaults, written out so the chain recorded
#: on the upload row says what it did.
LIMITER_STAGE = "alimiter=limit=0.707946:level=disabled:attack=5:release=50"

#: ``loudnorm``'s loudness-range target.  11 LU is the EBU streaming figure and is what
#: the stored chain uses, so a call does not change character between the two.
EXPORT_LRA = 11.0

#: Under this many seconds, ``loudnorm`` is not trustworthy and the measured-gain branch
#: runs instead.  Same threshold, and the same reason, as
#: :data:`app.audioproc.chain.LOUDNORM_MIN_S`.
SHORT_CLIP_S = 3.0

#: The most the measured branch may lift or cut, dB, when the settings carry no
#: ``audio_max_gain_db`` of their own.
DEFAULT_MAX_GAIN_DB = 30.0

#: ``volumedetect`` prints this on stderr: ``[Parsed_volumedetect_0 @ …] mean_volume: -27.3 dB``
_MEAN_RE = re.compile(rb"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")

#: …and this, one line under it: the highest sample in the clip.  The same pass reports both,
#: so the peak costs nothing on top of the mean and is what caps the gain (:func:`loudness_stage`).
_MAX_RE = re.compile(rb"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


def _num(value: float, places: int = 2) -> str:
    """A filter-graph number: fixed point, no exponent, no trailing noise."""
    return f"{value:.{places}f}".rstrip("0").rstrip(".") or "0"


def target_lufs(settings: object) -> float | None:
    """``export_target_lufs`` as a usable number, or ``None`` when the stage is off.

    0 is the documented "off"; unset, empty and a value that is not a number at all are
    read the same way, because a settings object reaching here malformed must cost the
    export its gain, not its upload.
    """
    raw = getattr(settings, "export_target_lufs", None)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("export: export_target_lufs=%r is not a number; the export gain is off", raw)
        return None
    if not math.isfinite(value) or value == 0.0:
        return None
    return value


def _max_gain_db(settings: object) -> float:
    try:
        value = float(getattr(settings, "audio_max_gain_db", DEFAULT_MAX_GAIN_DB))
    except (TypeError, ValueError):
        return DEFAULT_MAX_GAIN_DB
    return abs(value) if math.isfinite(value) and value else DEFAULT_MAX_GAIN_DB


@dataclass(frozen=True)
class MeasuredLevels:
    """What one ``volumedetect`` pass says about a clip: its mean, and its loudest sample."""

    mean_db: float
    peak_db: float | None


def _detect(pattern: re.Pattern[bytes], stderr: bytes) -> float | None:
    found = pattern.search(stderr)
    if not found:
        return None
    try:
        value = float(found.group(1))
    except ValueError:  # pragma: no cover - the regex only matches numbers
        return None
    return value if math.isfinite(value) else None


def measured_levels(path: Path | str, *, timeout_s: float = 20.0) -> MeasuredLevels | None:
    """The clip's mean and peak in dBFS via ``volumedetect``, or ``None`` if they can't be had.

    One cheap ffmpeg pass that decodes to null — no encode, no temp file — reporting both
    numbers, which is why the peak is free.

    The **mean** is what sets the gain.  dBFS RMS and LUFS are not the same unit, but on a
    mono speech clip with no low end they track within a decibel or two, which is inside the
    tolerance of "as loud as the call before it".

    The **peak** is what caps it.  The limiter behind the gain will hold the ceiling either
    way, but a limiter asked to swallow 12 dB is a compressor: it flattens the transients it
    is clamping and the clip comes out dull and pumping.  Knowing where the loudest sample
    already sits lets the gain stop just under the ceiling instead, so the limiter has
    nothing left to do — which is the difference between a safety net and a sound.

    Any failure returns ``None`` and the caller falls back to ``loudnorm``.
    """
    if shutil.which("ffmpeg") is None:
        return None
    cmd = [
        "ffmpeg", "-nostdin", "-v", "info", "-y",
        "-i", str(path), "-af", "volumedetect", "-f", "null", "-",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        log.debug("export: could not measure %s for the export gain", path)
        return None
    if proc.returncode != 0:
        return None
    stderr = proc.stderr or b""
    mean = _detect(_MEAN_RE, stderr)
    # ffmpeg reports -91 dB for digital silence; lifting that only lifts hiss.
    if mean is None or mean <= -90.0:
        return None
    return MeasuredLevels(mean_db=mean, peak_db=_detect(_MAX_RE, stderr))


def measured_mean_db(path: Path | str, *, timeout_s: float = 20.0) -> float | None:
    """Just the mean, for a caller that has no use for the peak."""
    levels = measured_levels(path, timeout_s=timeout_s)
    return None if levels is None else levels.mean_db


def loudness_stage(
    target: float,
    *,
    duration_s: float | None = None,
    measured_db: float | None = None,
    measured_peak_db: float | None = None,
    max_gain_db: float = DEFAULT_MAX_GAIN_DB,
) -> str:
    """The one loudness stage — ``loudnorm=…`` or ``volume=…dB`` — for this clip.

    The measured branch is taken only when the clip is known to be shorter than
    :data:`SHORT_CLIP_S` **and** a measurement came back; an unmeasurable short clip is
    better served by a loudnorm that may under-correct than by no stage at all.  A gain
    under a tenth of a decibel is not worth a filter and returns ``""``.

    Two caps on that gain, and they are not the same cap.  ``max_gain_db`` is about the
    *source*: it stops a clip that measured as near-silence from being amplified into a
    wall of hiss.  ``measured_peak_db`` is about the *ceiling*: a lift computed from a mean
    says nothing about where the loudest sample already is, and a quiet clip with one sharp
    transient in it — a mic click, the front of a page tone — will take the whole lift and
    put that transient well over :data:`EXPORT_TRUE_PEAK_DB`.  The limiter downstream would
    catch it, but only by clamping several decibels off a single peak, which is audible.
    So when the peak is known the gain stops where ``peak + gain`` reaches the ceiling and
    the limiter is left with nothing to do.  When it is not known, the limiter is the net,
    as it always was.
    """
    short = duration_s is not None and 0.0 < duration_s < SHORT_CLIP_S
    if short and measured_db is not None:
        gain = max(-max_gain_db, min(max_gain_db, target - measured_db))
        if measured_peak_db is not None and math.isfinite(measured_peak_db):
            # …never *raise* the gain to reach the ceiling: this only ever cuts it, so a
            # clip that is already too hot is still handled by the negative gain above.
            gain = min(gain, EXPORT_TRUE_PEAK_DB - measured_peak_db)
            gain = max(gain, -max_gain_db)
        return f"volume={_num(gain)}dB" if abs(gain) >= 0.1 else ""
    # ``TP`` is spelled with its tenth and ``LRA`` without one because that is how both
    # numbers read in every reference chain; ffmpeg parses either spelling identically.
    return f"loudnorm=I={_num(target)}:TP={EXPORT_TRUE_PEAK_DB:.1f}:LRA={EXPORT_LRA:g}"


def compose(*stages: str | None) -> str | None:
    """Join non-empty ``-af`` fragments with commas; ``None`` when nothing is left."""
    parts = [part.strip() for part in stages if part and part.strip()]
    return ",".join(parts) or None


def tone_envelope_for(
    audio_meta: Mapping[str, Any] | None, settings: object, *, processed: bool
) -> list[str]:
    """The tone attenuation to re-apply after the export loudness stage, or ``[]``.

    A call that is *nothing but* a tone defeats every loudness measurement there is.
    The tone-only call (talkgroup 1001, 3.38 s) is a 2.0 s 993 Hz console tone at
    -18 dBFS and no speech at all: the §5.5 pass took the span down to about -33 dB in the
    stored file, and then :func:`loudness_stage` measured the processed copy, found nothing
    in it but the attenuated tone, and lifted the whole thing to the -11 LUFS export target —
    handing OpenMHz and the Feed a tone *louder* than the one we had just turned down.

    So the envelope goes on again, after the lift, from the spans the pass recorded
    (``audio_meta.tone_spans``, each with the ``pattern`` that decides its depth).  Nothing
    to re-apply when:

    * the pass removed the tones instead of turning them down (``tone_removed``) — they are
      not in the audio to be lifted, and the timeline was renumbered under them;
    * the deployment's ``audio_tone_handling`` is no longer ``attenuate``;
    * the call has no stored pass, or it found no tones.

    ``processed`` says which file is being encoded, because the spans are recorded in the
    **source** timebase: the processed copy has ``head_trim_s`` taken off its front and the
    spans have to move with it, while an unprocessed original is still in the timebase the
    spans were measured in.
    """
    meta = audio_meta or {}
    spans = meta.get("tone_spans") or []
    if not spans or meta.get("tone_removed"):
        return []
    config = AudioConfig.from_settings(settings)
    if config.tone_handling != "attenuate":
        return []
    segments = []
    for span in spans:
        try:
            segments.append(
                ToneSegment(
                    start_s=float(span["start_s"]),
                    end_s=float(span["end_s"]),
                    hz=float(span.get("hz") or 0.0),
                    pattern=str(span.get("pattern") or ""),
                    # The depth the stored file was actually turned down by, when the pass
                    # recorded it.  Preferred over the current setting, because the promise
                    # is "never louder in a tone span than the file it came from" and that
                    # file was made under whatever the setting was then.
                    attenuate_db=(
                        float(span["attenuate_db"]) if span.get("attenuate_db") is not None else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):  # a hand-edited or pre-v2 meta row
            log.debug("export loudness: ignoring an unreadable tone span %r", span)
    offset = float(meta.get("head_trim_s") or 0.0) if processed else 0.0
    return tone_envelope(segments, config, offset)


def export_filter(
    base: str | None,
    settings: object,
    *,
    path: Path | str | None = None,
    duration_s: float | None = None,
    audio_meta: Mapping[str, Any] | None = None,
    processed: bool = False,
) -> str | None:
    """``base`` plus the export loudness stage, the tone envelope and the limiter.

    ``base`` is whatever the encode already wanted (the §5.2 normalize filter on an
    unprocessed original, ``None`` on the processed copy, which is already even); it is
    **composed with**, never replaced by, the export stage.  When ``export_target_lufs``
    is off the base is returned untouched, which is exactly the behaviour that predates
    this module.

    ``path`` and ``duration_s`` together enable the short-clip branch: the duration says
    whether to take it, the path is what gets measured.  Neither is required — with no
    duration the ``loudnorm`` branch runs, which is the right default for an unknown clip.

    ``audio_meta`` and ``processed`` are the call's stored pass and which of its two files is
    being encoded; together they put the tone attenuation back on *after* the lift
    (:func:`tone_envelope_for`).  Omitting them is the old behaviour, tone lift included.
    """
    target = target_lufs(settings)
    if target is None:
        return base
    measured: MeasuredLevels | None = None
    if path is not None and duration_s is not None and 0.0 < duration_s < SHORT_CLIP_S:
        measured = measured_levels(path)
    stage = loudness_stage(
        target,
        duration_s=duration_s,
        measured_db=None if measured is None else measured.mean_db,
        measured_peak_db=None if measured is None else measured.peak_db,
        max_gain_db=_max_gain_db(settings),
    )
    # …the envelope between the lift and the limiter: after, so the lift cannot undo it, and
    # before the limiter, which is the one promise every outbound file makes.
    envelope = tone_envelope_for(audio_meta, settings, processed=processed)
    return compose(base, stage, *envelope, LIMITER_STAGE)
