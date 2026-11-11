"""Credentials that travel in a URL, masked before the URL reaches a log line.

Three do, because the clients that use them cannot send a header:

* a feed's token is its path — ``/api/feeds/<token>.rss`` and ``.atom`` (app/api/feeds.py);
* the audio enclosure a feed signs carries it as ``?feed=<token>``;
* an ``upload``-scope API token rides as ``?key=`` on the upload routes and the recorder
  socket (``/api/upload/flow?key=kup_…``, ``/api/recorder/stream?key=…``).

uvicorn writes the full path and query of every request to ``uvicorn.access``, and of every
WebSocket handshake to ``uvicorn.error``; :class:`RedactUrlCredentials` rewrites those
records' arguments in place.  nginx does the same for its own access log with a ``map`` and
a ``log_format`` (frontend/nginx.conf); the two patterns are kept in step by hand, and
``tests/test_log_redact.py`` pins both.
"""

from __future__ import annotations

import logging
import re

#: What a masked value becomes.  Fixed, so a log line still shows *that* a credential was
#: sent (and which route), never which one.
MASK = "REDACTED"

#: ``/api/feeds/<token>.rss|atom``.  ``[^/?.]`` keeps the built-in ``/api/feeds/preset/…``
#: routes (a second path segment) and ``/api/feeds/presets`` (no extension) unmasked.
_FEED_PATH = re.compile(r"(/api/feeds/)[^/?.\s\"]+(\.(?:rss|atom))")
#: ``feed=``, ``key=`` and ``token=`` anywhere in a query string.
_QUERY = re.compile(r"([?&](?:feed|key|token)=)[^&#\s\"]*")


def redact(text: str) -> str:
    """``text`` with every URL-borne credential replaced by :data:`MASK`."""
    if "/api/feeds/" in text:
        text = _FEED_PATH.sub(rf"\g<1>{MASK}\g<2>", text)
    if "=" in text:
        text = _QUERY.sub(rf"\g<1>{MASK}", text)
    return text


class RedactUrlCredentials(logging.Filter):
    """Mask credentials in a record's string arguments (and its message, if it has none).

    Never drops a record.  The arguments are rewritten rather than the formatted message so
    uvicorn's own formatter — which colours the request line from ``args`` — keeps working.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in args)
        elif isinstance(args, dict) and args:
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in args.items()}
        elif isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


#: The loggers uvicorn writes request URLs to: the access log, and the handshake line of a
#: WebSocket (``'%s - "WebSocket %s" [accepted]'``), which goes to ``uvicorn.error``.
UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.error")


def install(loggers: tuple[str, ...] = UVICORN_LOGGERS) -> None:
    """Attach the filter to each logger, once.  Safe to call on every start-up."""
    for name in loggers:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactUrlCredentials) for f in logger.filters):
            logger.addFilter(RedactUrlCredentials())


__all__ = ["MASK", "UVICORN_LOGGERS", "RedactUrlCredentials", "install", "redact"]
