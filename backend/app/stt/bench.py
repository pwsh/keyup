"""Benchmark CLI: ``python -m app.stt.bench --backend whisper --files 40``.

Also ``--backend local`` (faster-whisper on the CPU) and ``--backend openvino --device GPU``
(OpenVINO GenAI on an Intel iGPU/NPU; ``--model OpenVINO/whisper-large-v3-turbo-int8-ov``).

Transcribes a duration-stratified sample of the real sdrtrunk corpus through one backend and
reports per-file real-time factor, quality label and text, plus a summary.  Used to compare the
GPU server against local CPU inference before choosing defaults for a deployment.

Audio files are opened read-only; nothing under ``AUDIO_ROOT`` is ever modified.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from .base import BackendError
from .postprocess import assess
from .prompt import build_hotwords, build_prompt
from .types import Quality, TranscribeOptions, TranscriptResult

DEFAULT_AUDIO_ROOT = Path(__file__).resolve().parents[3] / "audio"
DEFAULT_BASE_URL = "http://whisper:8000"
DEFAULT_REMOTE_MODEL = "Systran/faster-whisper-large-v3"
DEFAULT_LOCAL_MODEL = "Systran/faster-whisper-small"
DEFAULT_OPENVINO_MODEL = "OpenVINO/whisper-base-fp16-ov"

# 32 kbps CBR mono (ARCHITECTURE §1.5) -> ~4000 bytes of audio per second, plus a fixed ID3 tag.
_BYTES_PER_SECOND = 4000.0
_TAG_BYTES = 500.0


def _approx_duration(path: Path) -> float:
    """Cheap duration estimate from file size, good enough to stratify the sample."""
    return max(0.0, (path.stat().st_size - _TAG_BYTES) / _BYTES_PER_SECOND)


def pick_files(audio_root: Path, count: int) -> list[Path]:
    """Pick ``count`` files spread evenly across the corpus's duration range, deterministically."""
    files = sorted(p for p in audio_root.rglob("*.mp3") if p.is_file())
    if not files:
        raise SystemExit(f"no .mp3 files under {audio_root}")
    files.sort(key=_approx_duration)
    if count >= len(files):
        return files
    # Even strides through the size-sorted list give a mix of squelch tails and long dispatches
    # without any randomness, so two runs compare like for like.
    step = (len(files) - 1) / max(1, count - 1) if count > 1 else 1.0
    picked: list[Path] = []
    seen: set[Path] = set()
    for i in range(count):
        candidate = files[min(len(files) - 1, round(i * step))]
        if candidate not in seen:
            seen.add(candidate)
            picked.append(candidate)
    return picked


def _make_backend(args: argparse.Namespace):
    if args.backend == "local":
        from .faster_whisper_local import FasterWhisperLocalBackend

        return FasterWhisperLocalBackend(
            name="local",
            model=args.model or DEFAULT_LOCAL_MODEL,
            compute_type=args.compute_type,
            cpu_threads=args.cpu_threads,
        )
    if args.backend == "openvino":
        from .openvino_local import OpenVINOLocalBackend

        return OpenVINOLocalBackend(
            name="openvino",
            model=args.model or DEFAULT_OPENVINO_MODEL,
            device=args.device,
            cache_dir=args.cache_dir,
            cpu_threads=args.cpu_threads,
        )
    from .openai_compatible import OpenAICompatibleBackend

    return OpenAICompatibleBackend(
        name=args.backend,
        base_url=args.base_url,
        model=args.model or DEFAULT_REMOTE_MODEL,
        api_key=args.api_key,
    )


def _options(args: argparse.Namespace) -> TranscribeOptions:
    prompt = None
    hotwords = None
    if not args.no_prompt:
        prompt = build_prompt(system_name="Example System")
        hotwords = build_hotwords()
    return TranscribeOptions(
        language="en",
        prompt=prompt,
        hotwords=hotwords,
        vad=not args.no_vad,
        word_timestamps=args.words,
        timeout_s=args.timeout,
    )


def _row(path: Path, result: TranscriptResult | None, label: str, wall_s: float, audio_s: float) -> str:
    rtf = (audio_s / wall_s) if wall_s > 0 and audio_s else 0.0
    text = (result.text if result else "").replace("\n", " ")
    if len(text) > 96:
        text = text[:93] + "..."
    return f"{audio_s:7.2f}s {wall_s:7.2f}s {rtf:7.1f}x  {label:<8} {text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.stt.bench", description=__doc__)
    parser.add_argument(
        "--backend",
        default="whisper",
        help="'local' = faster-whisper CPU, 'openvino' = OpenVINO GenAI (see --device), "
        "any other name = remote OpenAI-compatible server",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--files", type=int, default=20, help="number of sample files")
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--device", default="CPU", help="openvino backend: CPU | GPU | NPU | AUTO")
    parser.add_argument("--cache-dir", default=None, help="openvino backend: model + blob cache")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--no-vad", action="store_true", help="disable vad_filter (hallucination demo)")
    parser.add_argument("--no-prompt", action="store_true", help="omit the domain prompt/hotwords")
    parser.add_argument("--words", action="store_true", help="request word timestamps")
    parser.add_argument("--json", type=Path, default=None, help="also write raw results here")
    args = parser.parse_args(argv)

    backend = _make_backend(args)
    options = _options(args)
    health = backend.health()
    device = getattr(backend, "device", "")
    print(f"backend={backend.name} kind={backend.kind} model={backend.model} device={device or 'n/a'}")
    print(f"health: ok={health.ok} latency={health.latency_ms:.0f}ms {health.detail}\n")
    if not health.ok:
        print("backend unhealthy; aborting", file=sys.stderr)
        return 2

    files = pick_files(args.audio_root, args.files)
    print(f"{'audio':>8} {'wall':>8} {'rtf':>8}  {'quality':<8} text")
    print("-" * 110)

    rows: list[dict] = []
    rtfs: list[float] = []
    labels: Counter[str] = Counter()
    decoded_audio = 0.0
    decoded_wall = 0.0
    reasons: Counter[str] = Counter()
    errors = 0

    for path in files:
        started = time.perf_counter()
        try:
            result = backend.transcribe(path, options)
        except BackendError as exc:
            errors += 1
            print(f"{'':>8} {'':>8} {'':>8}  {'ERROR':<8} {path.name}: {exc}")
            rows.append({"file": str(path), "error": str(exc)})
            continue
        wall_s = time.perf_counter() - started
        audio_s = result.duration or _approx_duration(path)
        quality = assess(result, audio_s, options)
        labels[quality.quality.value] += 1
        for reason in quality.reasons:
            reasons[reason.split("=")[0]] += 1
        # A clip VAD emptied out never reaches the decoder, so its "RTF" is request overhead,
        # not throughput.  Those files are counted separately instead of inflating the mean.
        if wall_s > 0 and audio_s and result.text.strip():
            rtfs.append(audio_s / wall_s)
            decoded_audio += audio_s
            decoded_wall += wall_s
        print(_row(path, result, quality.quality.value, wall_s, audio_s))
        rows.append(
            {
                "file": str(path.relative_to(args.audio_root)),
                "audio_s": round(audio_s, 3),
                "wall_s": round(wall_s, 3),
                "rtf": round(audio_s / wall_s, 2) if wall_s else None,
                "quality": quality.quality.value,
                "reasons": quality.reasons,
                "text": result.text,
                "cleaned_text": quality.cleaned_text,
            }
        )

    print("-" * 110)
    total_audio = sum(r.get("audio_s") or 0 for r in rows)
    total_wall = sum(r.get("wall_s") or 0 for r in rows)
    empty = len(files) - errors - len(rtfs)
    print(
        f"files={len(files)} errors={errors} vad_empty={empty} "
        f"audio={total_audio:.1f}s wall={total_wall:.1f}s"
    )
    if rtfs:
        print(
            f"rtf (decoded only): mean={statistics.fmean(rtfs):.1f}x "
            f"median={statistics.median(rtfs):.1f}x min={min(rtfs):.1f}x max={max(rtfs):.1f}x"
        )
        # Aggregate is the number that matters for a queue: total audio / total wall clock.
        print(
            f"throughput: decoded={decoded_audio / decoded_wall:.1f}x  "
            f"overall={total_audio / total_wall:.1f}x (incl. vad-empty clips)"
        )
    print("quality: " + ", ".join(f"{q}={labels.get(q, 0)}" for q in (x.value for x in Quality)))
    if reasons:
        print("reasons: " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
