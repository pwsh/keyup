"""Shared value types for the Keyup speech-to-text package.

Everything a backend returns is normalized into :class:`TranscriptResult` so the router,
the post-processor and the DB writer never have to care which engine produced it
(ARCHITECTURE §5.2).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Quality",
    "QualityAssessment",
    "TranscribeOptions",
    "TranscriptResult",
    "TranscriptSegment",
    "TranscriptWord",
]


class TranscriptSegment(BaseModel):
    """One whisper segment plus the confidence signals the hallucination heuristics need."""

    model_config = ConfigDict(extra="ignore")

    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None


class TranscriptWord(BaseModel):
    """One word with its timing, present only when ``word_timestamps`` was requested."""

    model_config = ConfigDict(extra="ignore")

    start: float
    end: float
    word: str
    probability: float | None = None


class TranscriptResult(BaseModel):
    """Engine-independent transcription output."""

    model_config = ConfigDict(extra="ignore")

    text: str
    language: str | None = None
    duration: float | None = None
    segments: list[TranscriptSegment] = Field(default_factory=list)
    words: list[TranscriptWord] | None = None
    engine: str
    """Backend kind, e.g. ``openai_compatible`` or ``faster_whisper_local``."""
    backend_name: str
    """Configured instance name, e.g. ``whisper``."""
    model: str
    processing_ms: int = 0
    raw: dict[str, Any] | None = None
    """Untouched engine payload, kept for debugging / re-scoring; not persisted by default."""

    @property
    def rtf(self) -> float | None:
        """Real-time factor: audio seconds transcribed per wall-clock second (higher is faster)."""
        if not self.duration or self.processing_ms <= 0:
            return None
        return self.duration / (self.processing_ms / 1000.0)


class TranscribeOptions(BaseModel):
    """Per-request knobs.  Defaults match the measured-best settings for sdrtrunk audio."""

    model_config = ConfigDict(extra="forbid")

    language: str | None = "en"
    prompt: str | None = None
    hotwords: str | None = None
    vad: bool = True
    """VAD is on by default: 14 % of the corpus is < 1 s of squelch tail and whisper
    hallucinates 'Thanks for watching!' on those without it (ARCHITECTURE §1.5)."""
    word_timestamps: bool = False
    temperature: float = 0.0
    timeout_s: float = 120.0
    normalize_filter: str | None = None
    """ffmpeg ``-af`` loudness-normalization filter applied to the audio *before* it reaches
    the engine, or ``None`` to leave the audio untouched.  The worker fills it from
    :func:`app.stt.normalize.ffmpeg_audio_filter`; every backend's audio-prep applies it so
    the requirement "normalized before transcription" holds for OVMS, openai_compatible and
    the local engines alike.  Never affects the stored recording (ARCHITECTURE §5.2)."""
    hallucination_patterns: str = ""
    """Operator-supplied hallucination patterns (``stt_hallucination_patterns``): one regex
    per line or ``;``-separated, merged with the builtin credit family by
    :func:`app.stt.postprocess.hallucination_phrase`.  Carried on the options because the
    post-processor is handed nothing else per job; no backend sends it anywhere."""


class Quality(StrEnum):
    """Transcript quality label stored on the ``transcripts`` row."""

    GOOD = "good"
    SUSPECT = "suspect"
    NOISE = "noise"


class QualityAssessment(BaseModel):
    """Result of :func:`app.stt.postprocess.assess`."""

    model_config = ConfigDict(extra="forbid")

    quality: Quality
    reasons: list[str] = Field(default_factory=list)
    cleaned_text: str = ""
