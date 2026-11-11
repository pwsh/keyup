"""The FastAPI application: routers, middleware, error handling and OpenAPI (§4).

Everything is mounted under ``/api`` so one nginx serves the SPA at ``/`` and proxies the
API and the audio under the same origin — which is what makes the session cookie
same-site and the CSRF rule above enforceable.

Two application-wide decisions live here:

* :func:`app.auth.mutation_guard` is installed as a dependency of the *application*, so
  every route — including any added later — is covered by default.  Safe methods pass
  through while ``AUTH_REQUIRE_READ`` is false; everything else needs a credential and,
  for a cookie, a CSRF proof.  ``tests/api/test_auth.py`` walks ``app.routes`` and fails
  if a non-GET route ever escapes it.
* errors are normalized to ``{"detail": "..."}`` for every status, so a client has one
  shape to parse.
* :func:`app.api.uploads.enforce_body_limit` is installed as middleware rather than as a
  route dependency, because Starlette parses ``multipart/form-data`` *before* FastAPI
  solves the route's dependencies — a dependency would run after the bytes had landed.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import orjson
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app import log_redact
from app.api import activity as activity_api
from app.api import admin as admin_api
from app.api import admin_config as admin_config_api
from app.api import agencies as agencies_api
from app.api import auth as auth_api
from app.api import bcfy_stats as bcfy_stats_api
from app.api import calls as calls_api
from app.api import channel_audit as channel_audit_api
from app.api import directory as directory_api
from app.api import edge as edge_api
from app.api import event_logs as event_logs_api
from app.api import feeds as feeds_api
from app.api import links as links_api
from app.api import live as live_api
from app.api import observations as observations_api
from app.api import prefs as prefs_api
from app.api import radio_events as radio_events_api
from app.api import recorder_stream as recorder_stream_api
from app.api import recorders as recorders_api
from app.api import roster as roster_api
from app.api import search as search_api
from app.api import stats as stats_api
from app.api import status as status_api
from app.api import status_activity as status_activity_api
from app.api import transcripts as transcripts_api
from app.api.uploads import body_size_guard
from app.auth import mutation_guard
from app.config import get_settings
from app.integrations import register_all as register_integrations
from app.live import bus
from app.rr import client as rr_client

log = logging.getLogger("app.main")

VERSION = status_api.VERSION

DESCRIPTION = """
Searchable archive of the radio traffic an [sdrtrunk](https://github.com/dsheirer/sdrtrunk)
recorder captures: every call, its metadata, its transcript, and the tools to find, play,
correlate and export it.

* **Reading needs no account.** Everything that *changes* data needs a session
  (`POST /api/auth/login`) or an API token (`Authorization: Bearer kup_…`); a cookie-authenticated
  mutation must also send `X-Requested-With: keyup`.
* **One filter set.** The parameters of `GET /api/calls` work identically on `/calls/facets`,
  `/calls/export`, `/calls/playlist.m3u`, `/search`, `/stats/*`, `/timeline` and the live feed.
  Array filters repeat (`?talkgroup=1101&talkgroup=1102`).
* **Patch-aware.** A patch is a temporary supergroup of talkgroups; every call stores *all*
  the talkgroups it involved, so `talkgroup=1101` finds patched traffic carrying 1101 too.
* **Two clocks, always.** Every call carries `started_at` (ISO 8601 UTC) and `started_local`
  (naive local time, the `TZ` setting), because the recorder, the dispatchers and any CAD system all think
  in local time. Time-of-day and day-of-week filters work on the local clock.
* **Pagination.** `page`/`page_size` (≤ 500) for tables, or `cursor=` keyset paging for long
  scrolls, which cannot skip or repeat a row while new calls arrive. A paged list also returns
  the matching row count, in `total` and in the **`X-Total-Count`** header — capped at 50 000,
  where `total_capped` becomes true. A cursor page returns `total: null`: an infinite scroll
  shows no total, and counting for one meant a full scan of the filtered set per page.
* **Audio** is served from `GET /api/calls/{id}/audio` with HTTP `Range`, a strong `ETag`
  (the file's sha256) and a long private `Cache-Control`; `GET /api/calls/{id}/waveform` gives
  a cached peak envelope for a player.
* **Links** (`/api/links`) tie a call or a conversation to a record in another system — a CAD
  incident, a case number — and outbound webhooks tell that system when calls and links change
  (docs/WEBHOOKS.md).
* **Live**: `WS /api/live` (send a filter object as the first frame) or `GET /api/live/sse`.
"""

OPENAPI_TAGS = [
    {
        "name": "calls",
        "description": (
            "The archive itself: list, facets, detail, audio, waveform, transcripts, edits, "
            "streamed exports and asynchronous bundles."
        ),
    },
    {
        "name": "search",
        "description": "Ranked full-text search with `<mark>` highlights, and prefix suggestions.",
    },
    {
        "name": "directory",
        "description": (
            "Who is who: talkgroups, radios and units, systems, sites, patches, conversation "
            "threads and the agency map."
        ),
    },
    {
        "name": "roster",
        "description": (
            "Who was that: the units a callsign names, the personnel who carry the radios, and "
            "the assignments that tie a radio to either of them over a stretch of time."
        ),
    },
    {
        "name": "live",
        "description": (
            "Calls as they land, over WebSocket or Server-Sent Events, filtered exactly like "
            "`GET /calls`."
        ),
    },
    {
        "name": "stats",
        "description": (
            "Volume, activity heatmaps, per-talkgroup and per-radio tables, transcription "
            "throughput and recorder gaps."
        ),
    },
    {
        "name": "correlation",
        "description": (
            "Tying radio traffic to records in another system: the per-minute timeline and the "
            "links that record a confirmed match."
        ),
    },
    {
        "name": "alerts",
        "description": "Alert rules, the delivery log, saved searches and favorite talkgroups.",
    },
    {
        "name": "admin",
        "description": (
            "Operations: ingest health and rescan, the transcription queue, STT backend health, "
            "runtime settings, retention, API tokens, users and Prometheus metrics."
        ),
    },
    {
        "name": "bcfy stats",
        "description": (
            "Broadcastify **feed owner** statistics: listener history, hot hours and archive "
            "listings for the operator's own live feed. Admin-only — this is account data, not "
            "archive data."
        ),
    },
    {
        "name": "feeds",
        "description": (
            "Syndication: a saved call filter published as RSS 2.0 and Atom 1.0. The token "
            "in a feed URL is the whole credential, so the URL is a secret; an unknown or "
            "disabled one is a 404. Managing feeds needs an account."
        ),
    },
    {"name": "auth", "description": "Sign in, sign out, and who the caller is."},
    {
        "name": "activity",
        "description": (
            "The operational activity log of the ingest and transcription pipeline: uploads "
            "accepted, duplicates rejected, no-audio calls, transcriptions done and failed, and "
            "errors. A paged backfill and a live SSE tail; admin only."
        ),
    },
    {"name": "status", "description": "Health and readiness probe."},
    {
        "name": "recorder",
        "description": (
            "Multi-tuner management: a recorder polls its desired tuner configuration and posts "
            "the live status of each tuner. Authenticated with the recorder's own `upload` token; "
            "the operator side of this lives under `admin`."
        ),
    },
    {
        "name": "upload",
        "description": (
            "Rdio-Scanner-compatible push ingest from a recorder, and the site observations a "
            "recorder posts for the channel audit."
        ),
    },
    {
        "name": "radio events",
        "description": (
            "What the control channel says about subscribers — registration, affiliation, "
            "location, SNDCP data — pushed in batches by a recorder, folded into the "
            "directory, and read back per radio, per site, or as the site's message census. "
            "Presence data about identifiable people: purged on its own clock "
            "(`RADIO_EVENTS_KEEP_DAYS`, 90 days). See docs/RADIO_EVENTS.md."
        ),
    },
]


class ORJSONResponse(JSONResponse):
    """JSON rendered with orjson — fast dates and no ``ensure_ascii``.

    Deliberately a ``JSONResponse`` subclass: that is how FastAPI recognizes a route as
    returning JSON, and only then does it put the route's ``response_model`` schema into
    the OpenAPI document instead of a bare ``{"type": "string"}``.
    """

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        if isinstance(content, bytes):  # already serialized by FastAPI
            return content
        return orjson.dumps(content, default=str)


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
        stream=sys.stdout,
        level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
        force=True,
    )
    # uvicorn logs every request's path *and query* — which is where a feed token and an
    # upload key travel (app/log_redact.py).  In-app rather than a ``--log-config`` flag, so
    # it holds however the server is started.
    log_redact.install()


def _apply_edge_configuration() -> None:
    """Push the HTTPS settings into the edge at start-up; never raises (docs/HTTPS.md).

    A deploy restarts the edge, which comes back on whatever ``entrypoint.sh`` read from the
    environment.  This is what makes the settings table, rather than ``.env``, the thing
    that decides — a second or two later.  It is best effort: the edge may not be listening
    yet (compose starts it after the API), and the worker's five-minute reconcile is the
    net under that.
    """
    from app.db import SessionLocal  # noqa: PLC0415
    from app.edge import service as edge_service  # noqa: PLC0415

    session = SessionLocal()
    try:
        edge_service.apply_now(session, reason="api-startup")
    except Exception:  # noqa: BLE001 - the front door is not worth the API for
        log.warning("api.startup could not apply the HTTPS configuration", exc_info=True)
        session.rollback()
    finally:
        session.close()


@asynccontextmanager
async def lifespan(application: FastAPI) -> Any:
    configure_logging()
    settings = get_settings()
    log.info("api.startup version=%s audio_root=%s tz=%s", VERSION, settings.audio_root, settings.tz)
    # Said once, at startup, because "the channel audit shows no RadioReference data" is
    # otherwise indistinguishable from "RadioReference has nothing" (docs/CHANNEL_AUDIT.md).
    if not rr_client.credentials_present(settings):
        log.info(
            "api.startup radioreference sync is disabled (RR_APP_KEY/RR_USERNAME/RR_PASSWORD unset); "
            "the channel audit still reports observed and configured channels"
        )
    # The LISTEN/NOTIFY bridge behind WS /api/live.  It reconnects on its own and never
    # raises into the request path, so a database that is briefly away costs the live feed
    # and nothing else.
    bus.start()
    _apply_edge_configuration()
    try:
        yield
    finally:
        bus.stop()
        log.info("api.shutdown")


app = FastAPI(
    title="Keyup",
    version=VERSION,
    description=DESCRIPTION,
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_tags=OPENAPI_TAGS,
    # One dependency on the application itself, so every route is guarded by default.
    dependencies=[Depends(mutation_guard)],
)

#: In development the Vite dev server is a different origin to this API and needs a CORS
#: allowance that carries the session cookie.  A deployment does not — nginx serves both
#: under one origin — and an unconditional credentialed allowance for localhost would let
#: any page on the operator's machine read this API as them.  So it is installed only when
#: PUBLIC_URL says this process *is* the local one.
_public_host = (urlsplit(get_settings().public_url).hostname or "").lower()
if _public_host in {"localhost", "127.0.0.1", "::1"}:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count", "X-Job-Counts", "Content-Disposition", "X-Request-ID"],
    )


#: Installed before anything reads a body, so an oversized upload is refused at the door
#: (H4).  Registered here rather than decorated in ``app.api.uploads`` so the module stays
#: importable — and reusable — without this application object.
app.middleware("http")(body_size_guard)


@app.middleware("http")
async def request_logger(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    admin_api.count_request(request.method, response.status_code)
    log.info(
        "http %s %s -> %s in %sms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> ORJSONResponse:
    """One `{"detail": "..."}` body for every HTTP error."""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return ORJSONResponse(
        {"detail": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None)
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> ORJSONResponse:
    """Flatten pydantic's error list into the same shape."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error.get("loc", ()) if p not in ("query", "body", "path"))
        parts.append(f"{location or 'request'}: {error.get('msg', 'invalid')}")
    detail = "; ".join(parts) or "invalid request"
    # A browser shows the user its 422; a recorder logs "http status: 422" and nothing
    # else, so a schema drift between the two ends is invisible from this side unless it
    # is written down here.  Machine routes only: a person's typo in the SPA is not news.
    if request.url.path.startswith(("/api/recorder/", "/api/call-upload", "/api/upload/")):
        log.warning("validation.error path=%s: %s", log_redact.redact(request.url.path), detail[:1000])
    return ORJSONResponse({"detail": detail}, status_code=422)


@app.exception_handler(SQLAlchemyError)
async def database_exception_handler(request: Request, exc: SQLAlchemyError) -> ORJSONResponse:
    log.error("db.error path=%s: %s", log_redact.redact(request.url.path), exc, exc_info=exc)
    return ORJSONResponse({"detail": "database error"}, status_code=500)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> ORJSONResponse:
    log.error("unhandled.error path=%s: %s", log_redact.redact(request.url.path), exc, exc_info=exc)
    return ORJSONResponse({"detail": "internal server error"}, status_code=500)


ROUTERS = [
    auth_api.router,
    calls_api.router,
    calls_api.exports_router,
    search_api.router,
    directory_api.router,
    roster_api.router,
    channel_audit_api.router,
    live_api.router,
    observations_api.router,
    radio_events_api.router,
    stats_api.router,
    stats_api.timeline_router,
    links_api.links_router,
    prefs_api.router,
    feeds_api.router,
    transcripts_api.router,
    admin_api.router,
    admin_api.metrics_router,
    admin_config_api.router,
    agencies_api.router,
    edge_api.router,
    bcfy_stats_api.router,
    activity_api.router,
    status_api.router,
    status_activity_api.router,
    recorders_api.router,
    recorders_api.admin_router,
    recorder_stream_api.router,
    event_logs_api.router,
    event_logs_api.admin_router,
]

# The recorder push endpoint (§1.6) is owned by the ingest side of the project; the API
# mounts it when it is present so this module does not depend on it existing yet.
try:  # pragma: no cover - exercised by whichever half of the tree is checked out
    from app.api.call_upload import router as call_upload_router
except ImportError:  # pragma: no cover
    call_upload_router = None
    log.info("api: app.api.call_upload is not present; /api/call-upload is not mounted")
else:
    ROUTERS.append(call_upload_router)

for _router in ROUTERS:
    app.include_router(_router, prefix="/api")

# Optional integrations (app/integrations/*/): each mounts its own routes when its package is
# present, and an absent one is simply not there — no import, no routes, no error.
INTEGRATIONS = register_integrations(app)


# --------------------------------------------------------------------- OpenAPI security ---

_BEARER_SCHEME = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "kup_<40 url-safe characters>",
    "description": (
        "An API token from the Admin page or `keyup token create`. Send it as "
        "`Authorization: Bearer kup_…`. It authenticates as itself and needs no CSRF header; "
        "the session cookie still does. Reading the archive needs no token at all. Scopes: "
        "`read`, `write`, `admin`, `stream`, `upload` — `admin` implies the rest. Tokens "
    ),
}


def custom_openapi() -> dict[str, Any]:
    """The generated document, plus the bearer scheme and the `X-Total-Count` header.

    The security scheme is declared at the document root as *optional* auth: every read
    works without it, and the guarded routes document their own 401 — this is about making
    the "Authorize" button in `/api/docs` actually send the header.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["BearerAuth"] = _BEARER_SCHEME
    schema["security"] = [{"BearerAuth": []}]

    total_count = {
        "description": "Rows matching the filter, ignoring pagination.",
        "schema": {"type": "integer", "example": 8027},
    }
    for path, operations in schema.get("paths", {}).items():
        get_op = operations.get("get")
        if not get_op:
            continue
        response = (get_op.get("responses") or {}).get("200")
        if response is None:
            continue
        # Every list endpoint returns a Page envelope; those are the ones that set the header.
        schema_ref = str((response.get("content") or {}).get("application/json", {}).get("schema", ""))
        if "Page_" in schema_ref or path.endswith(("/calls", "/talkgroups", "/radios", "/links")):
            response.setdefault("headers", {})["X-Total-Count"] = total_count

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]
