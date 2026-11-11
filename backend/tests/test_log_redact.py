"""URL-borne credentials never reach an access log line (app/log_redact.py)."""

from __future__ import annotations

import logging

import pytest

from app.log_redact import MASK, RedactUrlCredentials, install, redact

TOKEN = "Zq3_x-9aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        (f"/api/feeds/{TOKEN}.rss", f"/api/feeds/{MASK}.rss"),
        (f"/api/feeds/{TOKEN}.atom?limit=5", f"/api/feeds/{MASK}.atom?limit=5"),
        (f"/api/calls/12/audio?feed={TOKEN}", f"/api/calls/12/audio?feed={MASK}"),
        (f"/api/calls/12/audio?raw=1&feed={TOKEN}&x=2", f"/api/calls/12/audio?raw=1&feed={MASK}&x=2"),
        ("/api/upload/flow?key=kup_secret", f"/api/upload/flow?key={MASK}"),
        ("/api/recorder/stream?instance=a&key=s3cret", f"/api/recorder/stream?instance=a&key={MASK}"),
        (f"/api/calls/1/audio?feed={TOKEN}&key=kup_x", f"/api/calls/1/audio?feed={MASK}&key={MASK}"),
        # left alone: the built-in feeds, the admin routes, look-alike parameter names
        ("/api/feeds/preset/emergency.rss", "/api/feeds/preset/emergency.rss"),
        ("/api/feeds/presets", "/api/feeds/presets"),
        ("/api/feeds/12/rotate", "/api/feeds/12/rotate"),
        ("/api/calls?monkey=1&feedback=2", "/api/calls?monkey=1&feedback=2"),
    ],
)
def test_redact(raw: str, masked: str) -> None:
    assert redact(raw) == masked


def test_the_uvicorn_access_record_is_masked_through_its_arguments() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.5:1234", "GET", f"/api/feeds/{TOKEN}.rss", "1.1", 200), None,
    )  # fmt: skip
    assert RedactUrlCredentials().filter(record) is True
    line = record.getMessage()
    assert TOKEN not in line
    assert f"/api/feeds/{MASK}.rss" in line


def test_install_attaches_one_filter_to_each_uvicorn_logger(caplog) -> None:  # noqa: ANN001
    install()
    install()  # every start-up calls it; it must not stack
    for name in ("uvicorn.access", "uvicorn.error"):
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, RedactUrlCredentials)]
        assert len(filters) == 1
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logging.getLogger("uvicorn.error").info(
            '%s - "WebSocket %s" [accepted]', "192.0.2.5:1", "/api/recorder/stream?key=kup_zzz"
        )
    assert "kup_zzz" not in caplog.text
    assert f"key={MASK}" in caplog.text
