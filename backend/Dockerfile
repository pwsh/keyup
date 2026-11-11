# Keyup backend: the API, the ingest worker and the transcription worker share this image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: audio decode/resample for the local faster-whisper backend, for waveform peaks and
# for the audio-processing stage (ARCHITECTURE §5.5), which needs `atrim`, `afftdn`, `agate`,
# `loudnorm`, `volume` and `alimiter` — all in Debian's build.  libpq5: psycopg 3.
# numpy is not listed here: it is a base dependency in pyproject.toml (the tone detector) and
# arrives with the pip install below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 keyup \
    && mkdir -p /app /srv/keyup/audio \
    && chown keyup:keyup /app

WORKDIR /app

# Dependencies first, from the lock and nothing else, so this layer is rebuilt only when
# requirements.lock changes — a source edit reuses it instead of re-downloading the whole
# set.  The lock pins every wheel by version *and* hash (`make lock`), so an image built
# next month has the same FastAPI/Starlette/psycopg the tests ran against, and it also
# carries the build backend (hatchling) so the package install below never reaches PyPI.
# `nice`: this runs inside the build daemon on the live host, next to ingest and STT —
# niceness on the `docker build` client does not reach it.
COPY requirements.lock ./
RUN nice -n 19 pip install --no-cache-dir --require-hashes -r requirements.lock

# Then the source.  `--no-deps --no-build-isolation`: every dependency (and hatchling) is
# already installed from the lock above, so this only builds and installs the `keyup`
# package itself (the console script `keyup seed-agencies` runs) — seconds, and offline.
COPY --chown=keyup:keyup pyproject.toml README.md alembic.ini ./
COPY --chown=keyup:keyup alembic ./alembic
COPY --chown=keyup:keyup app ./app
RUN nice -n 19 pip install --no-cache-dir --no-deps --no-build-isolation . \
    && rm -rf build

USER keyup
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
