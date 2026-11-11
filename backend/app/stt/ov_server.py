"""An OpenAI-compatible micro-server in front of :class:`~app.stt.openvino_local.OpenVINOLocalBackend`.

Why this exists: the API/worker image is ``python:3.12-slim`` and has none of the Intel
userspace (OpenCL ICD, Level Zero, the NPU driver) that OpenVINO needs to reach an iGPU or an
NPU.  Rather than dragging that stack into the image everything else runs in, the accelerator
gets its own container — ``backend/Dockerfile.openvino``, compose profile ``stt-openvino`` —
and this module is what it runs.  The worker then talks to it with the ``openai_compatible``
backend it already has, pointed at ``http://stt-openvino:8000``, and nothing about the main
image changes.  ``docs/STT.md`` has the deployment side.

The wire format is the subset of the OpenAI Audio API that
:class:`~app.stt.openai_compatible.OpenAICompatibleBackend` actually sends and reads:

* ``POST /v1/audio/transcriptions`` — multipart ``file`` plus ``model``, ``language``,
  ``prompt``, ``hotwords``, ``temperature``, ``response_format`` and ``vad_filter``.
  ``vad_filter`` is accepted and ignored (openvino-genai has no VAD; the backend's own
  short/silent-clip guard stands in for it — see ``openvino_local``), as is
  ``timestamp_granularities[]``, because word timings need the pipeline to be *built* for them.
* ``GET /v1/models`` — the one model this process serves, which is what the client's health
  probe looks for.
* ``GET /health`` — the container healthcheck, and the place the OpenVINO device list surfaces.

A model id that is not the one this process loaded answers **404** with a Speaches-shaped
``detail``, because that is what makes the client raise ``ModelNotInstalledError`` and the
router disqualify the backend instead of retrying it for ever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from .openvino_local import OpenVINOLocalBackend, available_devices
from .types import TranscribeOptions, TranscriptResult

log = logging.getLogger("app.stt.ov_server")

__all__ = ["backend_from_env", "create_app"]

#: Uploads larger than this are refused before they touch the disk.  An hour of the corpus's
#: 32 kbps mono MP3 is ~14 MB, so 256 MB is generous for a single call recording.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

_CHUNK = 1024 * 1024


def backend_from_env() -> OpenVINOLocalBackend:
    """Build the served backend from ``OV_*`` environment variables (compose sets these)."""
    return OpenVINOLocalBackend(
        name=os.environ.get("OV_NAME", "openvino"),
        model=os.environ.get("OV_MODEL", "OpenVINO/whisper-large-v3-turbo-int8-ov"),
        device=os.environ.get("OV_DEVICE", "CPU"),
        cache_dir=os.environ.get("OV_CACHE_DIR") or None,
        cpu_threads=int(os.environ.get("OV_CPU_THREADS", "4")),
        concurrency=int(os.environ.get("OV_CONCURRENCY", "1")),
    )


def _segment_payload(result: TranscriptResult) -> list[dict[str, Any]]:
    return [
        {
            "id": index,
            "seek": 0,
            "start": seg.start,
            "end": seg.end,
            "text": seg.text,
            "temperature": 0.0,
            "avg_logprob": seg.avg_logprob,
            "compression_ratio": seg.compression_ratio,
            "no_speech_prob": seg.no_speech_prob,
            "words": None,
        }
        for index, seg in enumerate(result.segments)
    ]


def verbose_json(result: TranscriptResult) -> dict[str, Any]:
    """The ``verbose_json`` body, shaped exactly as the client's ``_to_result`` expects."""
    return {
        "task": "transcribe",
        "language": result.language,
        "duration": result.duration,
        "text": result.text,
        "segments": _segment_payload(result),
        "words": [w.model_dump() for w in result.words] if result.words else None,
    }


def create_app(backend: OpenVINOLocalBackend | None = None) -> FastAPI:
    """Build the app around ``backend`` (a mock in tests, the real thing in the container)."""
    engine = backend if backend is not None else backend_from_env()
    # One accelerator, one decode at a time: the semaphore keeps a burst of queued jobs from
    # thrashing the same compiled model, and matches the backend's own concurrency setting.
    limiter = asyncio.Semaphore(max(1, int(getattr(engine, "concurrency", 1))))

    app = FastAPI(
        title="Keyup OpenVINO STT",
        version="1",
        description=f"whisper on OpenVINO ({engine.device}) behind the OpenAI Audio API",
    )
    app.state.backend = engine

    @app.get("/health")
    async def health() -> JSONResponse:
        probe = await run_in_threadpool(engine.health)
        body = {
            "ok": probe.ok,
            "detail": probe.detail,
            "latency_ms": probe.latency_ms,
            "model": engine.model,
            "device": engine.device,
            "devices": available_devices(),
        }
        return JSONResponse(body, status_code=200 if probe.ok else 503)

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": engine.model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "openvino",
                    "device": engine.device,
                }
            ],
        }

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(  # noqa: PLR0913 - this is a wire format, not a call site
        file: Annotated[UploadFile, File()],
        model: Annotated[str | None, Form()] = None,
        language: Annotated[str | None, Form()] = None,
        prompt: Annotated[str | None, Form()] = None,
        hotwords: Annotated[str | None, Form()] = None,
        response_format: Annotated[str, Form()] = "verbose_json",
        temperature: Annotated[float, Form()] = 0.0,
        vad_filter: Annotated[str | None, Form()] = None,  # accepted, no-op (see module docstring)
    ):
        if model and model != engine.model and model != Path(engine.model).name:
            raise HTTPException(
                status_code=404,
                detail=f"Model {model!r} is not installed locally. This server serves {engine.model!r}.",
            )
        path = await _spool(file)
        options = TranscribeOptions(
            language=language or None,
            prompt=prompt or None,
            hotwords=hotwords or None,
            temperature=temperature,
        )
        started = time.perf_counter()
        try:
            async with limiter:
                result = await run_in_threadpool(engine.transcribe, path, options)
        except Exception as exc:  # noqa: BLE001 - one failed clip must not kill the server
            log.exception("transcription failed for %s", file.filename)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
        log.info(
            "transcribed %s (%.1fs audio) on %s in %.2fs",
            file.filename,
            result.duration or 0.0,
            engine.device,
            time.perf_counter() - started,
        )
        if response_format == "text":
            return PlainTextResponse(result.text)
        if response_format == "json":
            return {"text": result.text}
        return verbose_json(result)

    return app


async def _spool(file: UploadFile) -> Path:
    """Write the upload to a private temp directory, refusing anything oversized.

    The backend hands the file to ffmpeg, which wants a path; the directory (not just the
    file) is temporary so the caller can delete the whole thing in one call.
    """
    directory = Path(tempfile.mkdtemp(prefix="keyup-ov-"))
    target = directory / (Path(file.filename or "audio").name or "audio")
    written = 0
    try:
        with target.open("wb") as handle:
            while chunk := await file.read(_CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="audio upload too large")
                handle.write(chunk)
    except HTTPException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return target


# Deliberately no module-level ``app``: importing this module must never compile a model.
# The container runs ``python -m app.stt.ov_server``; ``uvicorn --factory
# app.stt.ov_server:create_app`` is the equivalent if you want uvicorn's own CLI.


def main() -> None:  # pragma: no cover - container entrypoint
    """``python -m app.stt.ov_server`` — what the ``stt-openvino`` container runs."""
    import uvicorn  # noqa: PLC0415 - only the server entrypoint needs it

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        create_app(),
        host=os.environ.get("OV_HOST", "0.0.0.0"),  # noqa: S104 - container port, published by compose
        port=int(os.environ.get("OV_PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
