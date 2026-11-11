"""Broadcastify feed-owner statistics: the tolerant parser, the rollup, and the hot-times maths.

**One fixture is a sample of an observed shape; the rest are guesses.**  ``feed_sample.json``
reproduces the shape of an ``a=feed`` response as it was observed (with an invented feed),
and it is asserted *exactly*: the capital ``Feed`` envelope holding a list, ``listeners``
for the count, ``status: 1`` for up.  The others predate it and stay, because one account's answer is not a schema: they
assert the thing that actually has to be true regardless — that several *plausible* shapes
are all read correctly, that a shape the mapping does not recognize is stored whole rather
than dropped, and that nothing anywhere puts the password in a string.

The maths tests use synthetic history rather than fixtures: a heatmap, a peak and a
correlation are checked against numbers computed by hand in the test, which is the only way
to know the implementation and the expectation are not the same mistake twice.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.bcfy_stats import store
from app.bcfy_stats import summary as bcfy_summary
from app.bcfy_stats.client import (
    BcfyOwnerClient,
    BcfyOwnerError,
    archive_days,
    archive_rows,
    extract,
    feed_payload,
    parse_archive_range,
    parse_body,
    redact,
)
from app.bcfy_stats.poller import (
    PAGE_WATERMARK_KEY,
    FeedPoller,
    credentials,
    sync_archives,
    sync_public_page,
)
from app.bcfy_stats.public_page import (
    PublicPageError,
    fetch_public_page,
    parse_public_page,
)
from app.config import Settings
from app.models import BcfyArchive, BcfyFeedHourly, BcfyFeedSample, Setting
from tests.conftest import requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bcfy"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def stub(handler) -> httpx.Client:  # noqa: ANN001
    """An httpx client whose transport is a function, so no test touches the network."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(body: str, content_type: str = "application/json") -> object:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, text=body, headers={"content-type": content_type})

    return handler


def day_document(day: str) -> str:
    """One day's archive listing, with start instants that belong to *that* day.

    ``archives_day.json`` names the same two epochs whatever day is asked for, which is fine
    for a parser test and useless for a backfill one: ``start_at`` is an archive row's
    identity, so every day would upsert over the last day's rows and the stored-days set
    would never grow.
    """
    midnight = datetime.fromisoformat(f"{day}T00:00:00+00:00")
    return json.dumps(
        {
            "archives": [
                {
                    "start": int((midnight + timedelta(minutes=30 * index)).timestamp()),
                    "end": int((midnight + timedelta(minutes=30 * index + 30)).timestamp()),
                    "file": f"https://archive.broadcastify.com/2211/{day}-{index}.mp3",
                }
                for index in range(2)
            ]
        }
    )


def feed_settings(**overrides) -> Settings:  # noqa: ANN003
    fields = {
        "tz": "America/Winnipeg",
        "bcfy_owner_username": "operator",
        "bcfy_owner_password": "hunter2",
        "bcfy_feed_id": "2211",
        "bcfy_stats_enabled": True,
        "bcfy_stats_poll_seconds": 60,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)


def synthetic_call(system_id: int, talkgroup_id: int, moment: datetime, path: str):  # noqa: ANN201
    """One minimal call row.  Nothing here is about ingest, so only the columns the
    heatmap query reads — the time, the talkgroup — carry meaning."""
    from app.models import Call

    return Call(
        system_id=system_id,
        talkgroup_id=talkgroup_id,
        recorded_at=moment,
        started_at=moment,
        started_local=moment.replace(tzinfo=None),
        ended_at=moment + timedelta(seconds=3),
        duration_s=3.0,
        file_path=path,
        file_name=path.rsplit("/", 1)[-1],
    )


# --------------------------------------------------------------------------------------
# parsing — the guessing half
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "listeners", "online"),
    [
        ("feed_camel.json", 37, True),
        ("feed_snake.json", 12, True),
        ("feed_bare.json", 4, False),
    ],
)
def test_every_plausible_json_shape_yields_the_same_two_facts(
    fixture: str, listeners: int, online: bool
) -> None:
    document = parse_body(load(fixture))
    extracted = extract(document)
    assert extracted["listeners"] == listeners
    assert extracted["online"] is online


def test_the_observed_response_shape_is_read_exactly() -> None:
    """The observed ``a=feed`` body, asserted field by field.

    This is not a tolerance test: it is the one fixture where being wrong means the live
    page is wrong.  The envelope is a *capital* ``Feed`` holding a one-element list, the
    count is plainly ``listeners``, and "up" is ``status: 1`` and nothing else — there is no
    boolean anywhere in the response.
    """
    document = parse_body(load("feed_sample.json"))
    payload = feed_payload(document)
    # The capital-and-list envelope is unwrapped to the feed object itself.
    assert payload["id"] == 2211
    assert payload["mount"] == "/examplemount"

    extracted = extract(document)
    assert extracted["listeners"] == 12
    assert extracted["online"] is True
    assert extracted["status"] == 1
    assert extracted["name"] == "Example County Fire/EMS"
    assert extracted["genre"] == "Public Safety"
    assert extracted["mount"] == "/examplemount"
    assert extracted["bitrate"] == 32


def test_the_observed_response_carries_no_history_to_backfill() -> None:
    """The reason the listener chart starts at the first poll and can never reach back.

    Worth asserting rather than only documenting: if a future response ever *does* carry a
    series, this test fails and somebody re-reads `docs/BCFY_STATS.md`.
    """
    payload = feed_payload(parse_body(load("feed_sample.json")))
    assert set(payload) == {
        "id",
        "descr",
        "genre",
        "mount",
        "Relays",
        "status",
        "bitrate",
        "Counties",
        "listeners",
    }
    # Nothing plural-and-temporal: no series, no peak, no total, no yesterday.
    assert not [key for key in payload if "histor" in key.lower() or "peak" in key.lower()]


def test_the_nested_objects_of_the_observed_response_stay_out_of_the_extraction() -> None:
    """``Relays`` and ``Counties`` are objects, and a scalar mapping does not invent one.

    They are still in ``raw`` — that is the contract — but ``extracted`` holds only readings.
    """
    extracted = extract(parse_body(load("feed_sample.json")))
    assert "Relays" not in extracted
    assert "Counties" not in extracted
    assert all(not isinstance(value, dict | list) for value in extracted.values())


def test_the_xml_form_reads_the_same_as_the_json_one() -> None:
    document = parse_body(load("feed.xml"), "text/xml")
    extracted = extract(document)
    assert extracted["listeners"] == 21
    assert extracted["online"] is True
    assert extracted["name"] == "Example County Fire and EMS"
    # Attributes and child elements are both keys, which is what lets one mapping serve
    # both formats.
    assert feed_payload(document)["genre"] == "Fire-EMS"


def test_a_shape_nobody_predicted_is_stored_rather_than_rejected() -> None:
    """The whole design: an unrecognized response costs the *extraction*, never the sample."""
    document = parse_body('{"result": {"stats": {"nowListening": 9}}}')
    assert extract(document) == {}
    # …and the document itself is untouched, which is what goes into `raw`.
    assert document["result"]["stats"]["nowListening"] == 9


def test_a_body_that_is_neither_format_is_an_error() -> None:
    with pytest.raises(BcfyOwnerError):
        parse_body("Access denied")
    with pytest.raises(BcfyOwnerError):
        parse_body("")


def test_archive_rows_read_epochs_and_iso_alike() -> None:
    rows = archive_rows(parse_body(load("archives_day.json")), day=date(2026, 9, 1))
    assert len(rows) == 2
    assert rows[0]["start_at"] == datetime.fromtimestamp(1756702800, tz=UTC)
    assert rows[0]["size_bytes"] == 3600000
    assert rows[0]["url"].endswith(".mp3")
    # The second row names its times as ISO strings and its link `download`.
    assert rows[1]["end_at"] == datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    assert rows[1]["url"].endswith("2026090103000.mp3")


def test_archive_rows_from_the_xml_listing() -> None:
    rows = archive_rows(parse_body(load("archives_day.xml"), "text/xml"), day=date(2026, 9, 1))
    assert len(rows) == 2
    assert all(row["day"] == date(2026, 9, 1) for row in rows)
    assert rows[0]["url"].endswith("2026090100000.mp3")


def test_archive_rows_read_the_observed_clock_range_form() -> None:
    """The owner API as observed: ``{"archive": "10:35 PM - 10:50 PM", "url": …}``.

    The times are the feed's own clock (America/Winnipeg here, so 10:50 PM is 03:50 UTC the
    next day) and a block that "ends before it starts" crossed midnight.
    """
    rows = archive_rows(
        parse_body(load("archives_day_sample.json")), day=date(2026, 9, 3), tz="America/Winnipeg"
    )
    assert [row["start_at"] for row in rows] == [
        datetime(2026, 9, 4, 3, 35, tzinfo=UTC),
        datetime(2026, 9, 4, 3, 5, tzinfo=UTC),
        datetime(2026, 9, 4, 4, 45, tzinfo=UTC),
    ]
    assert rows[0]["end_at"] == datetime(2026, 9, 4, 3, 50, tzinfo=UTC)
    assert rows[2]["end_at"] == datetime(2026, 9, 4, 5, 15, tzinfo=UTC)
    assert all(row["day"] == date(2026, 9, 3) for row in rows)
    assert rows[0]["url"].endswith("100001-2211.mp3")
    assert rows[0]["raw"]["archive"] == "10:35 PM - 10:50 PM"


def test_the_range_parser_refuses_what_it_does_not_understand() -> None:
    assert parse_archive_range("garbage", day=date(2026, 9, 3), tz="UTC") is None
    assert parse_archive_range("25:00 - 26:00", day=date(2026, 9, 3), tz="UTC") is None
    assert parse_archive_range(None, day=date(2026, 9, 3), tz="UTC") is None
    # An unknown zone falls back to UTC rather than failing the whole listing.
    span = parse_archive_range("01:00 - 02:00", day=date(2026, 9, 3), tz="Mars/Olympus")
    assert span == (datetime(2026, 9, 3, 1, tzinfo=UTC), datetime(2026, 9, 3, 2, tzinfo=UTC))


def test_a_row_with_no_start_is_dropped_not_stored() -> None:
    """``start_at`` is the row's identity; a row without one cannot be upserted."""
    rows = archive_rows({"archives": [{"url": "https://example.invalid/x.mp3"}]})
    assert rows == []


def test_the_day_list_form() -> None:
    assert archive_days(parse_body(load("archives_days.json"))) == [
        date(2026, 8, 30),
        date(2026, 8, 31),
        date(2026, 9, 1),
    ]


# --------------------------------------------------------------------------------------
# the password never travels anywhere it can be read
# --------------------------------------------------------------------------------------


def test_redact_blanks_the_password_in_a_url() -> None:
    url = "https://api.broadcastify.com/owner/?a=feed&feedId=2211&u=operator&p=hunter2&type=json"
    assert "hunter2" not in redact(url)
    assert "u=operator" in redact(url)


def test_an_http_error_never_echoes_the_password() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Broadcastify's own error pages echo the request; this is the case that matters.
        return httpx.Response(403, text=f"Denied for {request.url}")

    client = BcfyOwnerClient(
        username="operator", password="hunter2", feed_id="2211", client=stub(handler)
    )
    with pytest.raises(BcfyOwnerError) as caught:
        client.feed()
    assert "hunter2" not in str(caught.value)


def test_a_transport_failure_never_echoes_the_password() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {request.url}", request=request)

    client = BcfyOwnerClient(
        username="operator", password="hunter2", feed_id="2211", client=stub(handler)
    )
    with pytest.raises(BcfyOwnerError) as caught:
        client.feed()
    assert "hunter2" not in str(caught.value)


def test_the_credentials_repr_hides_the_password() -> None:
    assert "hunter2" not in repr(credentials(feed_settings()))


def test_the_request_asks_for_json_first_and_falls_back_to_xml() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        wanted = request.url.params.get("type", "")
        seen.append(wanted)
        if wanted == "json":
            return httpx.Response(500, text="not available for this account")
        return httpx.Response(200, text=load("feed.xml"), headers={"content-type": "text/xml"})

    client = BcfyOwnerClient(
        username="operator", password="hunter2", feed_id="2211", client=stub(handler)
    )
    assert extract(client.feed())["listeners"] == 21
    assert seen == ["json", "xml"]


def test_the_request_carries_every_documented_parameter() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, text=load("feed_camel.json"))

    client = BcfyOwnerClient(
        username="operator", password="hunter2", feed_id="2211", client=stub(handler)
    )
    client.archives(date(2026, 9, 1))
    assert captured["a"] == "archives"
    assert captured["feedId"] == "2211"
    assert captured["u"] == "operator"
    assert captured["p"] == "hunter2"
    assert captured["day"] == "2026-09-01"


# --------------------------------------------------------------------------------------
# the poller: gating and backoff
# --------------------------------------------------------------------------------------


def test_the_poller_does_nothing_at_all_while_the_switch_is_off() -> None:
    poller = FeedPoller()
    result = poller.tick(None, feed_settings(bcfy_stats_enabled=False))  # type: ignore[arg-type]
    assert result.skipped == "disabled"
    assert not result.polled


def test_the_poller_does_nothing_without_credentials() -> None:
    poller = FeedPoller()
    result = poller.tick(None, feed_settings(bcfy_feed_id=""))  # type: ignore[arg-type]
    assert result.skipped == "not configured"


def test_the_backoff_widens_and_then_resets() -> None:
    poller = FeedPoller()
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    poller._fail("boom", 60.0, now)  # noqa: SLF001 - the state under test
    assert poller.backoff_seconds(60.0) == 60.0
    poller._fail("boom", 60.0, now)  # noqa: SLF001
    assert poller.backoff_seconds(60.0) == 120.0
    for _ in range(20):
        poller._fail("boom", 60.0, now)  # noqa: SLF001
    assert poller.backoff_seconds(60.0) == 600.0  # the cap
    poller._recover()  # noqa: SLF001
    assert poller.failures == 0
    assert poller.next_attempt_at is None


def test_one_warning_per_failure_streak_not_per_poll(caplog) -> None:  # noqa: ANN001
    """A hundred identical warnings an hour is how a log stops being read."""
    poller = FeedPoller()
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    with caplog.at_level("WARNING", logger="app.bcfy_stats.poller"):
        for _ in range(5):
            poller._fail("boom", 60.0, now)  # noqa: SLF001
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_poll_that_is_backing_off_is_skipped_without_a_request() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal requests
        requests += 1
        return httpx.Response(500, text="down")

    poller = FeedPoller()
    settings = feed_settings()
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    poller.tick(None, settings, now=now, http_client=stub(handler))  # type: ignore[arg-type]
    assert requests > 0
    before = requests
    poller.tick(None, settings, now=now + timedelta(seconds=1), http_client=stub(handler))  # type: ignore[arg-type]
    assert requests == before


# --------------------------------------------------------------------------------------
# storage and the rollup
# --------------------------------------------------------------------------------------


@requires_db
def test_a_sample_keeps_the_whole_response_and_fills_the_hour(session) -> None:  # noqa: ANN001
    document = parse_body(load("feed_camel.json"))
    moment = datetime(2026, 9, 1, 17, 12, tzinfo=UTC)
    store.record_sample(session, feed_id="2211", document=document, polled_at=moment)
    session.commit()

    sample = session.scalars(select(BcfyFeedSample)).one()
    assert sample.listeners == 37
    assert sample.online is True
    # `raw` is the point: a widened mapping re-derives history from this.
    assert sample.raw["feed"]["peakListeners"] == 212
    assert sample.extracted["listeners_peak"] == 212

    hour = session.scalars(select(BcfyFeedHourly)).one()
    assert hour.hour == datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
    assert hour.samples == 1
    assert hour.listeners_avg == pytest.approx(37.0)


@requires_db
def test_the_hourly_average_is_maintained_incrementally(session) -> None:  # noqa: ANN001
    base = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
    for index, listeners in enumerate([10, 20, 30, 40]):
        store.record_sample(
            session,
            feed_id="2211",
            document={"feed": {"currentListeners": listeners}},
            polled_at=base + timedelta(minutes=index * 10),
        )
    session.commit()
    hour = session.scalars(select(BcfyFeedHourly)).one()
    assert hour.samples == 4
    assert hour.listeners_avg == pytest.approx(25.0)
    assert hour.listeners_max == 40


@requires_db
def test_a_response_with_no_listener_count_is_still_stored(session) -> None:  # noqa: ANN001
    store.record_sample(session, feed_id="2211", document={"result": "who knows"})
    session.commit()
    sample = session.scalars(select(BcfyFeedSample)).one()
    assert sample.listeners is None
    assert sample.raw == {"result": "who knows"}
    # …and an unreadable sample does not move the rollup.
    assert session.scalars(select(BcfyFeedHourly)).all() == []


@requires_db
def test_archives_upsert_rather_than_duplicate(session) -> None:  # noqa: ANN001
    """The download link is time-limited, so a re-listed block must *update*."""
    rows = archive_rows(parse_body(load("archives_day.json")), day=date(2026, 9, 1))
    store.upsert_archives(session, feed_id="2211", rows=rows)
    session.commit()
    assert session.scalar(select(BcfyArchive.id).where(BcfyArchive.feed_id == "2211")) is not None

    refreshed = [dict(row, url="https://archive.broadcastify.com/new.mp3") for row in rows]
    store.upsert_archives(session, feed_id="2211", rows=refreshed)
    session.commit()
    stored = session.scalars(select(BcfyArchive).order_by(BcfyArchive.start_at)).all()
    assert len(stored) == 2
    assert all(row.url == "https://archive.broadcastify.com/new.mp3" for row in stored)


@requires_db
def test_pruning_keeps_the_rollup(session) -> None:  # noqa: ANN001
    old = datetime.now(UTC) - timedelta(days=500)
    store.record_sample(
        session, feed_id="2211", document={"feed": {"listeners": 5}}, polled_at=old
    )
    store.record_sample(session, feed_id="2211", document={"feed": {"listeners": 6}})
    session.commit()
    assert store.prune_samples(session, keep_days=400) == 1
    session.commit()
    assert len(session.scalars(select(BcfyFeedSample)).all()) == 1
    # Two hours of history, and pruning a sample took neither of them.
    assert len(session.scalars(select(BcfyFeedHourly)).all()) == 2


@requires_db
def test_the_archive_sync_records_a_watermark_and_respects_it(session) -> None:  # noqa: ANN001
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=load("archives_day.json"))

    settings = feed_settings(bcfy_archives_sync_enabled=True, bcfy_archives_sync_hours=6)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    report = sync_archives(session, settings, now=now, http_client=stub(handler), days=2)
    session.commit()
    assert report["rows"] > 0
    assert session.get(Setting, "bcfy_archives_last_sync") is not None

    before = calls
    again = sync_archives(
        session, settings, now=now + timedelta(hours=1), http_client=stub(handler), days=2
    )
    assert again == {"skipped": "not due"}
    assert calls == before


@requires_db
def test_one_bad_day_does_not_abandon_the_archive_pass(session) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("day") == "2026-08-31":
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=load("archives_day.json"))

    report = sync_archives(
        session,
        feed_settings(bcfy_archives_sync_enabled=True),
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        http_client=stub(handler),
        days=3,
    )
    assert report["rows"] > 0
    assert len(report["errors"]) == 1
    assert "2026-08-31" in report["errors"][0]


@requires_db
def test_the_backfill_fetches_listed_days_that_were_never_stored(session) -> None:  # noqa: ANN001
    """The one-time catch-up: every day the API lists and this database has never seen.

    The day list is the whole of Broadcastify's memory of the feed; the recent-days pass
    only ever touches the last week.  Without the backfill, a deployment that started on the
    1st can never show the archives of the month before it, however long it runs.
    """
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        day = request.url.params.get("day")
        if day is None:
            return httpx.Response(
                200,
                text=json.dumps(
                    {"archives": [{"date": f"2026-08-{index:02d}"} for index in range(1, 21)]}
                ),
            )
        asked.append(day)
        return httpx.Response(200, text=day_document(day))

    report = sync_archives(
        session,
        feed_settings(bcfy_archives_sync_enabled=True, bcfy_archives_backfill_days_per_sync=5),
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        http_client=stub(handler),
        days=2,
    )
    session.commit()

    assert report["days_listed"] == 20
    assert report["backfilled_days"] == 5
    # Oldest first, so an interrupted catch-up leaves a contiguous history rather than a
    # lattice of days scattered through the month.
    assert asked[2:] == [f"2026-08-{index:02d}" for index in range(1, 6)]
    assert report["backfill_remaining"] == 15


@requires_db
def test_the_backfill_does_not_refetch_a_day_it_already_stored(session) -> None:  # noqa: ANN001
    """The budget is spent on the gap, not on the days that are already here."""

    def listing(days: list[str]):  # noqa: ANN202
        def handler(request: httpx.Request) -> httpx.Response:
            day = request.url.params.get("day")
            if day is None:
                return httpx.Response(200, text=json.dumps({"archives": [{"date": d} for d in days]}))
            return httpx.Response(200, text=day_document(day))

        return handler

    days = [f"2026-08-{index:02d}" for index in range(1, 11)]
    settings = feed_settings(
        bcfy_archives_sync_enabled=True, bcfy_archives_backfill_days_per_sync=4
    )
    first = sync_archives(
        session,
        settings,
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        http_client=stub(listing(days)),
        days=1,
    )
    session.commit()
    assert first["backfilled_days"] == 4
    assert first["backfill_remaining"] == 6

    second = sync_archives(
        session,
        settings,
        now=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        force=True,
        http_client=stub(listing(days)),
        days=1,
    )
    session.commit()
    assert second["backfilled_days"] == 4
    assert second["backfill_remaining"] == 2

    stored = store.stored_archive_days(session, feed_id="2211")
    assert {date.fromisoformat(day) for day in days[:8]} <= stored


@requires_db
def test_a_full_backfill_lifts_the_budget_but_not_the_ceiling(session) -> None:  # noqa: ANN001
    """``--full`` means "do not stop at 30", not "make unbounded requests"."""
    from app.bcfy_stats.poller import FULL_BACKFILL_CEILING

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("day") is None:
            days = [
                (date(2026, 9, 1) - timedelta(days=offset)).isoformat()
                for offset in range(1, FULL_BACKFILL_CEILING + 40)
            ]
            return httpx.Response(200, text=json.dumps({"archives": [{"date": d} for d in days]}))
        return httpx.Response(200, text=day_document(request.url.params["day"]))

    report = sync_archives(
        session,
        feed_settings(bcfy_archives_sync_enabled=True, bcfy_archives_backfill_days_per_sync=5),
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        http_client=stub(handler),
        days=1,
        full=True,
    )
    session.commit()
    assert report["backfilled_days"] == FULL_BACKFILL_CEILING
    assert report["backfill_remaining"] > 0


@requires_db
def test_the_backfill_can_be_turned_off(session) -> None:  # noqa: ANN001
    """A budget of zero keeps the sync to the recent week, which is the old behaviour."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        day = request.url.params.get("day")
        if day is None:
            return httpx.Response(
                200, text=json.dumps({"archives": [{"date": "2026-07-04"}, {"date": "2026-07-05"}]})
            )
        asked.append(day)
        return httpx.Response(200, text=day_document(day))

    report = sync_archives(
        session,
        feed_settings(bcfy_archives_sync_enabled=True, bcfy_archives_backfill_days_per_sync=0),
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        http_client=stub(handler),
        days=2,
    )
    assert report["backfilled_days"] == 0
    assert report["backfill_remaining"] == 2
    assert asked == ["2026-09-01", "2026-08-31"]


@requires_db
def test_a_backfill_day_that_failed_is_retried_by_the_next_pass(session) -> None:  # noqa: ANN001
    """Nothing marks a day done except its rows, so a 503 costs a pass and not the day."""
    failing = {"2026-08-02"}

    def handler(request: httpx.Request) -> httpx.Response:
        day = request.url.params.get("day")
        if day is None:
            return httpx.Response(
                200,
                text=json.dumps(
                    {"archives": [{"date": f"2026-08-{index:02d}"} for index in range(1, 4)]}
                ),
            )
        if day in failing:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=day_document(day))

    settings = feed_settings(bcfy_archives_sync_enabled=True)
    first = sync_archives(
        session,
        settings,
        now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        http_client=stub(handler),
        days=1,
    )
    session.commit()
    assert first["backfilled_days"] == 2
    assert any("2026-08-02" in message for message in first["errors"])

    failing.clear()
    second = sync_archives(
        session,
        settings,
        now=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        force=True,
        http_client=stub(handler),
        days=1,
    )
    session.commit()
    assert second["backfilled_days"] == 1
    assert second["errors"] == []
    assert date(2026, 8, 2) in store.stored_archive_days(session, feed_id="2211")


# --------------------------------------------------------------------------------------
# hot times
# --------------------------------------------------------------------------------------


def test_correlation_is_none_until_there_is_something_to_correlate() -> None:
    assert bcfy_summary.correlation([(1.0, 1.0)] * 5) is None  # too few hours
    assert bcfy_summary.correlation([(1.0, float(i)) for i in range(30)]) is None  # x never varies


def test_correlation_recognizes_a_perfect_line_either_way() -> None:
    rising = [(float(i), 2.0 * i + 3) for i in range(30)]
    assert bcfy_summary.correlation(rising) == pytest.approx(1.0)
    falling = [(float(i), -2.0 * i + 3) for i in range(30)]
    assert bcfy_summary.correlation(falling) == pytest.approx(-1.0)


def test_correlation_against_a_hand_computed_value() -> None:
    """Four points would be refused, so the pattern is repeated to clear the floor.

    x = 1,2,3,4 and y = 2,4,5,9, worked out by hand:
    n=4, Σx=10, Σy=20, Σxy=61, Σx²=30, Σy²=126 ->
    (4·61 − 10·20) / sqrt((4·30 − 10²)(4·126 − 20²)) = 44 / sqrt(20·104) ≈ 0.9648.
    Repeating the block leaves r unchanged, which is itself worth asserting.
    """
    block = [(1.0, 2.0), (2.0, 4.0), (3.0, 5.0), (4.0, 9.0)]
    expected = 44 / (20 * 104) ** 0.5
    assert bcfy_summary.correlation(block * 8) == pytest.approx(expected)


@requires_db
def test_the_heatmap_peak_and_correlation_over_synthetic_history(session, settings) -> None:  # noqa: ANN001
    """Four weeks in which 17:00 local is always the busy hour, and nothing else is.

    The listener series is written through the real ``record_sample`` path so the rollup
    under test is the one the poller builds, and the expectation is computed here rather
    than read back from the implementation.
    """
    from app.models import System, Talkgroup

    system = System(name="Test")
    session.add(system)
    session.flush()
    talkgroup = Talkgroup(system_id=system.id, tgid=1701, alias="FIRE", export_broadcastify=True)
    session.add(talkgroup)
    session.flush()

    # 28 days ending at a known instant. 22:00 UTC is 17:00 America/Winnipeg in September.
    end = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    for day in range(28):
        for hour in range(24):
            moment = end - timedelta(days=27 - day) - timedelta(hours=23 - hour)
            listeners = 40 if hour == 22 else 5
            store.record_sample(
                session,
                feed_id="2211",
                document={"feed": {"currentListeners": listeners}},
                polled_at=moment.replace(minute=30),
            )
            for index in range(10 if hour == 22 else 1):
                session.add(
                    synthetic_call(
                        system.id,
                        talkgroup.id,
                        moment + timedelta(minutes=index),
                        f"synthetic/{day}-{hour}-{index}.mp3",
                    )
                )
    session.commit()

    report = bcfy_summary.summary(
        session, feed_id="2211", settings=settings, days=30, now=end - timedelta(minutes=1)
    )

    assert report["current_listeners"] in (5, 40)
    assert report["peak_listeners"] == 40
    assert report["peak_at"] is not None

    grid = report["listeners_hour_of_week"]
    assert len(grid) == 7
    assert all(len(row) == 24 for row in grid)
    # 17:00 local on every weekday is the hot cell, and it is 40 everywhere.
    assert all(row[17] == pytest.approx(40.0) for row in grid)
    assert all(row[3] == pytest.approx(5.0) for row in grid)

    calls_grid = report["calls_hour_of_week"]
    assert all(row[17] == pytest.approx(10.0) for row in calls_grid)

    hottest = report["hottest_hours"]
    assert len(hottest) == 10
    # There are exactly seven hot cells — 17:00 on each of the seven weekdays — so they
    # take the whole top of the ranking and the quiet hours fill the rest.
    assert [cell["hour"] for cell in hottest[:7]] == [17] * 7
    assert [cell["weekday"] for cell in hottest[:7]] == list(range(7))
    assert hottest[0]["calls"] == 10
    assert all(cell["listeners_avg"] == pytest.approx(5.0) for cell in hottest[7:])

    # Listeners and call volume move together by construction, so r is 1.
    assert report["correlation"] == pytest.approx(1.0)
    assert report["correlation_hours"] >= 24

    # Every hour of the 28 days was polled, so nothing in the grid is unmeasured and the
    # listener half covers as much of the window as the poller has been alive for.
    assert all(cell is not None for row in grid for cell in row)
    # 28 days of hourly polls, less the final one: the window ends at the top of the hour
    # `now` falls in, so the last sample of the history sits just past it.
    assert report["listeners_hours_covered"] == 28 * 24 - 1
    assert report["first_sample_at"] is not None
    assert report["window_hours"] == 30 * 24
    # Four Mondays in 28 days, so a Monday-00:00 cell is the mean of four polls and says so.
    assert all(row[0] == 4 for row in report["listeners_samples_hour_of_week"])
    assert all(row[17] == 40 for row in report["listeners_max_hour_of_week"])

    # The quiet side: 17:00 is the loudest hour, so it appears in no quiet answer at all.
    assert report["quietest_basis"] == "listeners"
    quietest = report["quietest_hours"]
    assert len(quietest) == 10
    assert all(cell["listeners_avg"] == pytest.approx(5.0) for cell in quietest)
    assert all(cell["hour"] != 17 for cell in quietest)
    assert all(cell["samples"] > 0 for cell in quietest)

    windows = report["quiet_windows"]
    assert len(windows) == 3
    for window in windows:
        assert window["listeners_avg"] == pytest.approx(5.0)
        length = (window["end_hour"] - window["start_hour"]) % 24
        assert length >= bcfy_summary.MIN_WINDOW_HOURS
        hours = {(window["start_hour"] + step) % 24 for step in range(length)}
        # A window containing the busiest hour of the week is not a maintenance window.
        assert 17 not in hours
    # …and they are three different windows rather than three views of one night.
    assert len({(window["weekday"], window["start_hour"]) for window in windows}) == 3


@requires_db
def test_an_unpolled_cell_is_null_and_never_a_quiet_hour(session, settings) -> None:  # noqa: ANN001
    """A year-long window on a two-day poller must not report 8 000 silent hours.

    This is the whole reason a listener cell is `None` rather than `0`: with zeros, every
    hour nobody has ever looked at outranks every hour that was measured and found quiet,
    and the maintenance advice becomes "take the feed down whenever you have no data",
    which is exactly backwards.
    """
    end = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    # Two days of history, and within them one genuinely quiet hour.
    for day in range(2):
        for hour in range(24):
            moment = end - timedelta(days=1 - day) - timedelta(hours=23 - hour)
            store.record_sample(
                session,
                feed_id="2211",
                document={"feed": {"currentListeners": 1 if hour in (7, 8) else 30}},
                polled_at=moment.replace(minute=30),
            )
    session.commit()

    report = bcfy_summary.summary(
        session, feed_id="2211", settings=settings, days=365, now=end - timedelta(minutes=1)
    )
    grid = report["listeners_hour_of_week"]
    measured = [cell for row in grid for cell in row if cell is not None]
    # 48 consecutive hours touch 47 distinct hour-of-week cells: the pair that shares an
    # hour of day falls on the same cell.  The other 121 cells of the week are `null` — and
    # that is the point: this window is a year and the poller has been running two days.
    assert len(measured) == 47
    assert len([cell for row in grid for cell in row if cell is None]) == 7 * 24 - 47
    assert report["listeners_hours_covered"] == 47  # the 48th hour sits past the window
    assert report["window_hours"] == 365 * 24

    # Only measured cells can be quiet, and the quietest of them is the one that was quiet.
    assert report["quietest_basis"] == "listeners"
    assert all(cell["listeners_avg"] is not None for cell in report["quietest_hours"])
    assert report["quietest_hours"][0]["listeners_avg"] == pytest.approx(1.0)
    for window in report["quiet_windows"]:
        assert window["listeners_avg"] is not None


@requires_db
def test_with_no_listener_history_the_quiet_answer_is_about_calls_and_says_so(  # noqa: ANN001
    session, settings
) -> None:
    """The honest fallback: a feed that has never been polled still has radio traffic.

    The page can then say "quietest *traffic*" instead of "quietest listening", which is a
    real difference — the first is a guess about the second.
    """
    from app.models import System, Talkgroup

    system = System(name="Test")
    session.add(system)
    session.flush()
    talkgroup = Talkgroup(system_id=system.id, tgid=1701, alias="FIRE", export_broadcastify=True)
    session.add(talkgroup)
    session.flush()

    end = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    for day in range(7):
        for hour in range(24):
            moment = end - timedelta(days=6 - day) - timedelta(hours=23 - hour)
            for index in range(1 if hour in (9, 10) else 6):
                session.add(
                    synthetic_call(
                        system.id, talkgroup.id, moment + timedelta(minutes=index),
                        f"quiet/{day}-{hour}-{index}.mp3",
                    )
                )
    session.commit()

    report = bcfy_summary.summary(
        session, feed_id="2211", settings=settings, days=14, now=end - timedelta(minutes=1)
    )
    assert report["listeners_hours_covered"] == 0
    assert report["first_sample_at"] is None
    assert report["quietest_basis"] == "calls"
    assert report["quietest_hours"]
    assert all(cell["listeners_avg"] is None for cell in report["quietest_hours"])
    assert report["quietest_hours"][0]["calls"] == 1
    windows = report["quiet_windows"]
    assert windows
    assert all(window["listeners_avg"] is None for window in windows)
    # 04:00 and 05:00 local (09/10 UTC) are the quiet pair, and a window is at least two
    # hours, so the best window is exactly that pair.
    assert (windows[0]["start_hour"], windows[0]["end_hour"]) == (4, 6)


def test_a_quiet_window_may_wrap_midnight_but_not_the_weekday() -> None:
    """22:00-02:00 is one window; a run that would spill into Tuesday is not offered.

    Wrapping matters because the quiet part of a night straddles midnight almost by
    definition, and "Monday 22:00 to Monday 02:00" is how an operator writes the window they
    mean — the same weekday's night, not the four hours between two calendar days.
    """
    listeners = [[10.0] * 24 for _ in range(7)]
    calls = [[10.0] * 24 for _ in range(7)]
    for hour in (22, 23, 0, 1):
        listeners[0][hour] = 1.0
        calls[0][hour] = 1.0

    windows = bcfy_summary.quiet_windows(listeners, calls, basis="listeners", count=1)
    assert windows[0]["weekday"] == 0
    assert windows[0]["start_hour"] == 22
    assert windows[0]["end_hour"] == 2
    assert windows[0]["listeners_avg"] == pytest.approx(1.0)


def test_a_quiet_window_needs_every_one_of_its_hours_measured() -> None:
    """An unmeasured hour is not evidence of quiet, so a run containing one is not a run.

    Otherwise the least-observed stretch of the week wins every time, which is the failure
    mode this whole `None`-not-zero business exists to prevent.
    """
    listeners: list[list[float | None]] = [[10.0] * 24 for _ in range(7)]
    calls = [[10.0] * 24 for _ in range(7)]
    listeners[0][3] = None
    listeners[0][4] = 0.0  # the only measured zero, and it has no neighbour to pair with
    listeners[1][8] = 1.0
    listeners[1][9] = 1.0

    windows = bcfy_summary.quiet_windows(listeners, calls, basis="listeners", count=1)
    assert (windows[0]["weekday"], windows[0]["start_hour"], windows[0]["end_hour"]) == (1, 8, 10)


def test_quiet_windows_do_not_overlap_each_other() -> None:
    """Three answers, not one answer three times.

    The naive ranking returns 02:00-04:00, 02:00-05:00 and 03:00-05:00 — all true, all the
    same advice, and together they fill the card without saying anything twice as useful.
    """
    listeners = [[10.0] * 24 for _ in range(7)]
    calls = [[10.0] * 24 for _ in range(7)]
    for weekday, start in ((0, 2), (3, 14), (5, 20)):
        for hour in range(start, start + 3):
            listeners[weekday][hour % 24] = 1.0

    windows = bcfy_summary.quiet_windows(listeners, calls, basis="listeners", count=3)
    assert len(windows) == 3
    cells: set[tuple[int, int]] = set()
    for window in windows:
        length = (window["end_hour"] - window["start_hour"]) % 24
        run = {(window["weekday"], (window["start_hour"] + step) % 24) for step in range(length)}
        assert not (run & cells)
        cells |= run
    assert {window["weekday"] for window in windows} == {0, 3, 5}


@requires_db
def test_only_the_selected_talkgroups_reach_the_call_heatmap(session, settings) -> None:  # noqa: ANN001
    """`allow` mode plus a cleared checkbox means that talkgroup's traffic is not this feed's."""
    from app.models import System, Talkgroup

    system = System(name="Test")
    session.add(system)
    session.flush()
    kept = Talkgroup(system_id=system.id, tgid=1701, alias="FIRE", export_broadcastify=True)
    dropped = Talkgroup(system_id=system.id, tgid=1102, alias="PUBWORKS", export_broadcastify=False)
    session.add_all([kept, dropped])
    session.flush()
    moment = datetime(2026, 9, 1, 22, 30, tzinfo=UTC)
    for index, talkgroup in enumerate([kept, dropped]):
        session.add(
            synthetic_call(system.id, talkgroup.id, moment, f"synthetic/select-{index}.mp3")
        )
    session.add(Setting(key="export_broadcastify_talkgroup_mode", value="allow"))
    session.commit()

    from app import runtime_settings

    runtime_settings.invalidate()
    effective = runtime_settings.effective_settings(session, base=settings)
    report = bcfy_summary.summary(
        session, feed_id="2211", settings=effective, days=7, now=moment + timedelta(hours=1)
    )
    assert report["calls_total"] == 1


@requires_db
def test_the_samples_series_buckets_by_hour_and_by_day(session) -> None:  # noqa: ANN001
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    for hour in range(48):
        store.record_sample(
            session,
            feed_id="2211",
            document={"feed": {"currentListeners": hour}},
            polled_at=start + timedelta(hours=hour, minutes=5),
        )
    session.commit()
    end = start + timedelta(days=2)
    hourly = bcfy_summary.series(session, feed_id="2211", start=start, end=end, step="hour")
    assert len(hourly) == 48
    assert hourly[0]["listeners_avg"] == pytest.approx(0.0)
    daily = bcfy_summary.series(session, feed_id="2211", start=start, end=end, step="day")
    assert len(daily) == 2
    # Day one is hours 0..23, whose mean is 11.5.
    assert daily[0]["listeners_avg"] == pytest.approx(11.5)
    assert daily[0]["listeners_max"] == 23
    assert daily[1]["listeners_max"] == 47


# --------------------------------------------------------------------------------------
# the public feed page — the only backfill that exists
# --------------------------------------------------------------------------------------


def public_page_html() -> str:
    """``feed_page_sample.html`` — the public feed page's markup, with an invented feed."""
    return (FIXTURES / "feed_page_sample.html").read_text()


def test_the_public_page_yields_a_day_of_hourly_listeners() -> None:
    """The whole point: 24 hourly counts, oldest first, from a page anybody can fetch."""
    page = parse_public_page(public_page_html())
    assert len(page.bars) == 24
    # Oldest first, and the last bar is the hour in progress — which is the "Now" figure.
    assert page.bars[0] == 4
    assert page.bars[-1] == 0
    # The page's own Min/Max/Now summary agrees with the bars, which is the cheapest
    # available check that the bars were read in the right order and none was dropped.
    assert (page.min, page.max, page.now) == (0, 17, 0)
    assert min(page.bars) == page.min
    assert max(page.bars) == page.max
    assert page.bars[-1] == page.now


def test_the_public_page_yields_the_status_and_the_listener_count() -> None:
    page = parse_public_page(public_page_html())
    assert page.online is True
    assert page.since == datetime.fromtimestamp(1788495581, tz=UTC)
    assert page.listeners == 0


def test_a_page_missing_every_piece_yields_nones_rather_than_raising() -> None:
    """This parses somebody else's markup, so a redesign must be an ordinary outcome."""
    for html in ("", "<html><body>nothing here</body></html>", "<rect class='other'>"):
        page = parse_public_page(html)
        assert page.bars == []
        assert (page.online, page.since, page.listeners) == (None, None, None)
        assert page.empty


def test_a_page_with_only_a_sparkline_still_yields_its_bars() -> None:
    """Every piece is independently optional: bars without a status badge are still bars."""
    html = (
        '<svg class="listen-sparkline-svg">'
        '<rect class="listen-sparkline-bar" x="0"><title>3 listeners</title></rect>'
        '<rect class="listen-sparkline-bar" x="10"><title>4 listeners</title></rect>'
        "</svg>"
    )
    page = parse_public_page(html)
    assert page.bars == [3, 4]
    assert page.online is None


def test_the_bars_are_ordered_by_their_x_not_by_the_document() -> None:
    """`x` is the time axis; a page that emits them out of order still reads left to right."""
    html = (
        '<rect class="listen-sparkline-bar" x="20"><title>3 listeners</title></rect>'
        '<rect class="listen-sparkline-bar" x="0"><title>1 listeners</title></rect>'
        '<rect class="listen-sparkline-bar" x="10"><title>2 listeners</title></rect>'
    )
    assert parse_public_page(html).bars == [1, 2, 3]


def test_a_page_may_draw_more_or_fewer_than_a_day_of_bars() -> None:
    """The bar count is reported, never asserted: 24 is their rendering decision."""
    bars = "".join(
        f'<rect class="listen-sparkline-bar" x="{index * 10}"><title>{index} listeners</title></rect>'
        for index in range(5)
    )
    assert parse_public_page(bars).bars == [0, 1, 2, 3, 4]


def test_fetching_the_page_asks_https_with_a_browser_agent() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["agent"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text="<html></html>")

    fetch_public_page("2211", stub(handler))
    assert seen["url"] == "https://www.broadcastify.com/listen/feed/2211"
    assert "Mozilla" in str(seen["agent"])


def test_a_page_that_will_not_load_is_an_error_not_an_exception_upward() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(503, text="down")

    with pytest.raises(PublicPageError):
        fetch_public_page("2211", stub(handler))


@requires_db
def test_the_page_backfills_the_hours_nobody_polled(session) -> None:  # noqa: ANN001
    now = datetime(2026, 9, 3, 12, 34, tzinfo=UTC)
    written = store.record_page_hours(session, feed_id="2211", bars=[5, 6, 7], now=now)
    session.commit()
    assert written == 3
    rows = session.scalars(select(BcfyFeedHourly).order_by(BcfyFeedHourly.hour)).all()
    # Bar k of n lands on floor(now) - (n-1-k) hours: the last bar is the hour in progress.
    assert [row.hour for row in rows] == [
        datetime(2026, 9, 3, 10, tzinfo=UTC),
        datetime(2026, 9, 3, 11, tzinfo=UTC),
        datetime(2026, 9, 3, 12, tzinfo=UTC),
    ]
    assert [row.listeners_avg for row in rows] == [5.0, 6.0, 7.0]
    assert [row.listeners_max for row in rows] == [5, 6, 7]
    # samples stays 0: these are not polls, and every sample-weighted mean must know it.
    assert [row.samples for row in rows] == [0, 0, 0]
    assert [row.source for row in rows] == ["page", "page", "page"]


@requires_db
def test_a_page_value_never_overwrites_an_hour_this_deployment_polled(session) -> None:  # noqa: ANN001
    """Our own polls are the truth; a scrape fills gaps and never edits a measurement."""
    hour = datetime(2026, 9, 3, 11, tzinfo=UTC)
    store.record_sample(
        session, feed_id="2211", document={"feed": {"listeners": 41}}, polled_at=hour + timedelta(minutes=5)
    )
    session.commit()

    written = store.record_page_hours(
        session, feed_id="2211", bars=[5, 6, 7], now=datetime(2026, 9, 3, 12, 34, tzinfo=UTC)
    )
    session.commit()
    # The polled hour was skipped entirely — not averaged, not maxed, not counted.
    assert written == 2
    polled = session.scalars(select(BcfyFeedHourly).where(BcfyFeedHourly.hour == hour)).one()
    assert polled.listeners_avg == pytest.approx(41.0)
    assert polled.listeners_max == 41
    assert polled.samples == 1
    assert polled.source is None


@requires_db
def test_a_later_page_fetch_refreshes_an_hour_an_earlier_one_filled(session) -> None:  # noqa: ANN001
    now = datetime(2026, 9, 3, 12, 34, tzinfo=UTC)
    store.record_page_hours(session, feed_id="2211", bars=[5], now=now)
    session.commit()
    assert store.record_page_hours(session, feed_id="2211", bars=[9], now=now) == 1
    session.commit()
    session.expire_all()  # the rollup is written in SQL, not through the identity map
    row = session.scalars(select(BcfyFeedHourly)).one()
    assert row.listeners_avg == pytest.approx(9.0)
    assert row.source == "page"


@requires_db
def test_the_first_real_poll_replaces_a_page_hour_rather_than_averaging_with_it(session) -> None:  # noqa: ANN001
    """A page figure must not survive inside this deployment's own average, or its peak."""
    hour = datetime(2026, 9, 3, 12, tzinfo=UTC)
    store.record_page_hours(session, feed_id="2211", bars=[20], now=hour)
    session.commit()

    store.record_sample(
        session, feed_id="2211", document={"feed": {"listeners": 4}}, polled_at=hour + timedelta(minutes=10)
    )
    session.commit()
    session.expire_all()  # the rollup is written in SQL, not through the identity map
    row = session.scalars(select(BcfyFeedHourly)).one()
    assert row.listeners_avg == pytest.approx(4.0)  # not (20 + 4) / 2, and not 20
    assert row.listeners_max == 4  # not GREATEST(20, 4)
    assert row.samples == 1
    assert row.source is None

    # …and from there the ordinary incremental average takes over again.
    store.record_sample(
        session, feed_id="2211", document={"feed": {"listeners": 6}}, polled_at=hour + timedelta(minutes=20)
    )
    session.commit()
    session.expire_all()
    row = session.scalars(select(BcfyFeedHourly)).one()
    assert row.listeners_avg == pytest.approx(5.0)
    assert row.listeners_max == 6
    assert row.samples == 2


@requires_db
def test_the_public_page_pass_writes_a_watermark_and_respects_it(session) -> None:  # noqa: ANN001
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=public_page_html())

    settings = feed_settings(bcfy_public_page_poll_seconds=3600)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    report = sync_public_page(session, settings, now=now, http_client=stub(handler))
    session.commit()
    assert len(report["bars"]) == 24
    assert report["hours_written"] == 24
    assert report["online"] is True
    assert len(calls) == 1
    assert session.get(Setting, PAGE_WATERMARK_KEY) is not None

    # Half an hour later it is not due, and no request is made.
    again = sync_public_page(session, settings, now=now + timedelta(minutes=30), http_client=stub(handler))
    assert again == {"skipped": "not due"}
    assert len(calls) == 1

    # An hour later it is.
    sync_public_page(session, settings, now=now + timedelta(hours=2), http_client=stub(handler))
    assert len(calls) == 2


@requires_db
def test_the_public_page_pass_is_off_with_the_switch_or_the_interval(session) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        raise AssertionError("no request should be made")

    off = feed_settings(bcfy_stats_enabled=False)
    assert sync_public_page(session, off, http_client=stub(handler)) == {"skipped": "disabled"}
    zero = feed_settings(bcfy_public_page_poll_seconds=0)
    assert sync_public_page(session, zero, http_client=stub(handler)) == {"skipped": "disabled"}
    blank = feed_settings(bcfy_feed_id="")
    assert sync_public_page(session, blank, http_client=stub(handler)) == {"skipped": "not configured"}
    # …but the operator asking for it now overrides the switch and the interval, not the id.
    assert sync_public_page(session, blank, force=True, http_client=stub(handler)) == {
        "skipped": "not configured"
    }


@requires_db
def test_a_public_page_that_will_not_load_writes_nothing_and_no_watermark(session) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, text="oops")

    report = sync_public_page(session, feed_settings(), force=True, http_client=stub(handler))
    session.commit()
    assert report["error"]
    assert report["hours_written"] == 0
    assert session.scalars(select(BcfyFeedHourly)).all() == []
    # No watermark, so the next tick tries again: a page that would not load is cheap to retry.
    assert session.get(Setting, PAGE_WATERMARK_KEY) is None


@requires_db
def test_a_redesigned_page_is_not_an_error_and_writes_nothing(session) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, text="<html><body>we redesigned everything</body></html>")

    report = sync_public_page(session, feed_settings(), force=True, http_client=stub(handler))
    session.commit()
    assert "error" not in report
    assert report["bars"] == []
    assert report["hours_written"] == 0
    assert session.scalars(select(BcfyFeedHourly)).all() == []


@requires_db
def test_page_hours_count_towards_coverage_and_say_so(session, settings) -> None:  # noqa: ANN001
    now = datetime(2026, 9, 3, 12, 34, tzinfo=UTC)
    store.record_page_hours(session, feed_id="2211", bars=[5, 6, 7], now=now)
    store.record_sample(session, feed_id="2211", document={"feed": {"listeners": 40}}, polled_at=now)
    session.commit()
    result = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=7, now=now)
    # Three hours of history, one of which the poll took over from the page.
    assert result["listeners_hours_covered"] == 3
    assert result["hours_from_page"] == 2


# --------------------------------------------------------------------------------------
# what the imported history does to the summary
# --------------------------------------------------------------------------------------


@requires_db
def test_imported_hours_are_counted_apart_from_page_hours_and_polls(session, settings) -> None:  # noqa: ANN001
    """Three sources, three counts, one coverage line — and none of them passing as a poll."""
    from app.bcfy_stats.importer import import_csv

    now = datetime(2026, 9, 4, 17, 30, tzinfo=UTC)
    import_csv(session, "2211", (FIXTURES / "owner_hourly_sample.csv").read_text())
    store.record_page_hours(session, feed_id="2211", bars=[5], now=now)
    store.record_sample(session, feed_id="2211", document={"feed": {"listeners": 9}}, polled_at=now)
    session.commit()

    result = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=7, now=now)
    # The export's 179 hours, minus the ones outside a 7-day window, plus the polled hour.
    assert result["hours_from_import"] > 150
    assert result["hours_from_page"] == 0  # the page's only hour was taken over by the poll
    assert result["listeners_hours_covered"] == result["hours_from_import"] + 1


@requires_db
def test_the_daily_export_becomes_a_daily_series(session, settings) -> None:  # noqa: ANN001
    from app.bcfy_stats.importer import import_csv

    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    import_csv(session, "2211", (FIXTURES / "owner_uptime_sample.csv").read_text())
    session.commit()

    result = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=180, now=now)
    days = {row["day"]: row for row in result["daily"]}
    assert result["daily_days_covered"] == len(result["daily"]) > 170
    assert days[date(2026, 9, 3)] == {
        "day": date(2026, 9, 3),
        "uptime_pct": 99.72,
        "listeners_max": 31,
    }
    # Oldest first, like every other series on this page.
    assert [row["day"] for row in result["daily"]] == sorted(days)
    # …and a 7-day window carries only the last week of them.
    week = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=7, now=now)
    assert len(week["daily"]) < 10


@requires_db
def test_the_peak_comes_from_the_daily_export_when_that_is_where_it_is(session, settings) -> None:  # noqa: ANN001
    """The May incident: 412 listeners on a day no sample and no hour row has ever seen.

    Before the import the peak was whatever this deployment happened to poll, which on a
    young archive is a small number that reads as the feed's ceiling.  ``peak_source`` is
    what stops the answer from also implying a precision it does not have: a daily row knows
    the day and not the minute, so it is reported at that day's UTC midnight and *labelled*.
    """
    from app.bcfy_stats.importer import import_csv

    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    store.record_sample(
        session,
        feed_id="2211",
        document={"feed": {"listeners": 12}},
        polled_at=now - timedelta(hours=1),
    )
    session.commit()
    before = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=180, now=now)
    assert (before["peak_listeners"], before["peak_source"]) == (12, "samples")

    import_csv(session, "2211", (FIXTURES / "owner_uptime_sample.csv").read_text())
    session.commit()
    after = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=180, now=now)
    assert after["peak_listeners"] == 412
    assert after["peak_source"] == "daily"
    assert after["peak_at"] == datetime(2026, 5, 17, tzinfo=UTC)


@requires_db
def test_a_sample_still_wins_the_peak_when_it_is_the_higher_one(session, settings) -> None:  # noqa: ANN001
    """The order is largest-first; the source only breaks a tie, towards the sharper clock."""
    from app.bcfy_stats.importer import import_csv

    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    import_csv(session, "2211", (FIXTURES / "owner_uptime_sample.csv").read_text())
    store.record_sample(
        session,
        feed_id="2211",
        document={"feed": {"listeners": 500}},
        polled_at=now - timedelta(hours=1),
    )
    session.commit()
    result = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=180, now=now)
    assert (result["peak_listeners"], result["peak_source"]) == (500, "samples")
    assert result["peak_at"] == now - timedelta(hours=1)


@requires_db
def test_history_from_reaches_back_further_than_first_sample_at(session, settings) -> None:  # noqa: ANN001
    """Two dates that are months apart after an import, which is why they are two fields."""
    from app.bcfy_stats.importer import import_csv

    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    store.record_sample(session, feed_id="2211", document={"feed": {"listeners": 4}}, polled_at=now)
    import_csv(session, "2211", (FIXTURES / "owner_uptime_sample.csv").read_text())
    session.commit()

    result = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=180, now=now)
    assert result["history_from"] == date(2026, 3, 9)
    assert result["first_sample_at"].date() == date(2026, 9, 4)

    # The hourly export moves the *hourly* start back a week; the daily one still wins.
    import_csv(session, "2211", (FIXTURES / "owner_hourly_sample.csv").read_text())
    session.commit()
    widened = bcfy_summary.summary(session, feed_id="2211", settings=settings, days=180, now=now)
    assert widened["history_from"] == date(2026, 3, 9)
    assert widened["first_sample_at"].date() == date(2026, 8, 28)


def test_the_fixtures_say_they_are_guesses() -> None:
    """A guard against these inventions being mistaken for observed responses."""
    readme = (FIXTURES / "README.md").read_text().lower()
    assert "guess" in readme
    # …and they are at least well-formed, which is the only claim they make.
    for path in FIXTURES.glob("*.json"):
        json.loads(path.read_text())
