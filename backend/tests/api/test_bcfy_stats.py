"""``/api/admin/bcfy-stats`` — authorization first, then the shapes.

This router is stricter than the rest of the API on purpose: reads of the archive are open
because a recording of a public radio system is public, and a feed's listener numbers are
the operator's Broadcastify account data.  So the first tests here are the ones that check
an anonymous caller and a non-admin token are both refused on *every* route, including the
read-only ones.

The one live-HTTP route (``probe``) is exercised through an ``httpx.MockTransport``; nothing
in this file reaches Broadcastify.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session

from app.models import Setting
from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "bcfy"

ROUTES = [
    ("GET", "/api/admin/bcfy-stats/summary"),
    ("GET", "/api/admin/bcfy-stats/samples"),
    ("GET", "/api/admin/bcfy-stats/archives"),
    ("POST", "/api/admin/bcfy-stats/probe"),
    ("POST", "/api/admin/bcfy-stats/poll"),
    ("POST", "/api/admin/bcfy-stats/sync-archives"),
    ("POST", "/api/admin/bcfy-stats/poll-page"),
    ("POST", "/api/admin/bcfy-stats/import"),
]


@pytest.fixture
def configured(session: Session) -> None:
    """A feed configured through the ``settings`` table, as an operator would."""
    from app import runtime_settings

    for key, value in {
        "bcfy_owner_username": "operator",
        "bcfy_owner_password": "hunter2",
        "bcfy_feed_id": "2211",
    }.items():
        session.add(Setting(key=key, value=value))
    session.commit()
    runtime_settings.invalidate()


# --------------------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_refuses_an_anonymous_caller(client, method: str, path: str) -> None:  # noqa: ANN001
    response = client.request(method, path)
    assert response.status_code in (401, 403), path


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_refuses_a_non_admin_token(client, write_token, method, path) -> None:  # noqa: ANN001
    response = client.request(method, path, headers=bearer(write_token))
    assert response.status_code == 403, path


def test_an_admin_gets_a_clear_answer_when_no_feed_is_configured(client, admin_token) -> None:  # noqa: ANN001
    response = client.get("/api/admin/bcfy-stats/summary", headers=bearer(admin_token))
    assert response.status_code == 409
    assert "bcfy_feed_id" in response.json()["detail"]


# --------------------------------------------------------------------------------------
# the secret
# --------------------------------------------------------------------------------------


def test_the_password_is_never_echoed_by_the_settings_endpoint(  # noqa: ANN001
    client, admin_token, configured, read_token) -> None:
    """`secret` kind: stored, never read back — and `is_default` still says one is set."""
    rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    password = rows["bcfy_owner_password"]
    assert password["kind"] == "secret"
    assert password["value"] == ""
    assert password["is_default"] is False
    # The username and the feed id are not secrets and do come back.
    assert rows["bcfy_owner_username"]["value"] == "operator"
    assert rows["bcfy_feed_id"]["value"] == "2211"
    assert "hunter2" not in client.get("/api/admin/settings", headers=bearer(read_token)).text


def test_the_password_can_be_set_through_the_settings_endpoint(client, admin_token) -> None:  # noqa: ANN001
    response = client.put(
        "/api/admin/settings",
        json={"bcfy_owner_password": "s3cret", "bcfy_stats_enabled": "true"},
        headers=bearer(admin_token),
    )
    assert response.status_code == 200
    assert "s3cret" not in response.text
    rows = {row["key"]: row for row in response.json()}
    assert rows["bcfy_stats_enabled"]["value"] == "true"
    assert rows["bcfy_owner_password"]["is_default"] is False


def test_the_feed_settings_ship_off_and_empty(client, read_token) -> None:  # noqa: ANN001
    rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    assert rows["bcfy_stats_enabled"]["default_value"] == "false"
    assert rows["bcfy_archives_sync_enabled"]["default_value"] == "false"
    assert rows["bcfy_feed_id"]["default_value"] == ""
    assert rows["bcfy_stats_poll_seconds"]["default_value"] == "60.0"
    assert all(rows[key]["is_default"] for key in rows if key.startswith("bcfy_"))


# --------------------------------------------------------------------------------------
# the read routes
# --------------------------------------------------------------------------------------


def test_the_summary_of_an_empty_history(client, admin_token, configured) -> None:  # noqa: ANN001
    body = client.get(
        "/api/admin/bcfy-stats/summary", params={"days": 30}, headers=bearer(admin_token)
    ).json()
    assert body["feed_id"] == "2211"
    assert body["current_listeners"] is None
    assert body["correlation"] is None
    assert len(body["listeners_hour_of_week"]) == 7
    assert all(len(row) == 24 for row in body["listeners_hour_of_week"])
    assert body["from"] < body["to"]


def test_the_summary_reports_the_stored_history(  # noqa: ANN001
    client, admin_token, configured, session
) -> None:
    from app.bcfy_stats import store

    now = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
    for hours_ago, listeners in enumerate([9, 31, 7]):
        store.record_sample(
            session,
            feed_id="2211",
            document={"feed": {"currentListeners": listeners, "status": 1}},
            polled_at=now - timedelta(hours=hours_ago),
        )
    session.commit()

    body = client.get("/api/admin/bcfy-stats/summary", headers=bearer(admin_token)).json()
    assert body["current_listeners"] == 9
    assert body["online"] is True
    assert body["peak_listeners"] == 31
    assert body["hours_sampled"] == 3
    # Three hours is well under the floor, so no correlation is claimed.
    assert body["correlation"] is None
    assert body["correlation_hours"] == 3


def test_an_empty_history_reports_no_coverage_rather_than_a_grid_of_zeros(  # noqa: ANN001
    client, admin_token, configured
) -> None:
    """A page with nothing to say must say nothing, not "nobody listens, all week"."""
    body = client.get(
        "/api/admin/bcfy-stats/summary", params={"days": 365}, headers=bearer(admin_token)
    ).json()
    assert body["listeners_hours_covered"] == 0
    assert body["first_sample_at"] is None
    assert body["window_hours"] == 365 * 24
    assert all(cell is None for row in body["listeners_hour_of_week"] for cell in row)
    # With no listener sample anywhere, the quiet answer is about calls and admits it.
    assert body["quietest_basis"] == "calls"


def test_a_year_long_window_is_accepted(client, admin_token, configured) -> None:  # noqa: ANN001
    """The calls half really does go back that far, so the window really is offered."""
    for days in (180, 365, 730):
        response = client.get(
            "/api/admin/bcfy-stats/summary", params={"days": days}, headers=bearer(admin_token)
        )
        assert response.status_code == 200, days
        assert response.json()["days"] == days
    assert (
        client.get(
            "/api/admin/bcfy-stats/summary", params={"days": 731}, headers=bearer(admin_token)
        ).status_code
        == 422
    )


def test_the_summary_carries_the_quiet_answer_and_its_basis(  # noqa: ANN001
    client, admin_token, configured, session
) -> None:
    """The maintenance cards' whole payload, over a couple of days of real polls."""
    from app.bcfy_stats import store

    now = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
    for hours_ago in range(48):
        store.record_sample(
            session,
            feed_id="2211",
            document={"feed": {"currentListeners": 1 if hours_ago % 24 in (3, 4) else 25}},
            polled_at=now - timedelta(hours=hours_ago),
        )
    session.commit()

    body = client.get(
        "/api/admin/bcfy-stats/summary", params={"days": 30}, headers=bearer(admin_token)
    ).json()
    assert body["quietest_basis"] == "listeners"
    assert body["listeners_hours_covered"] == 48
    assert body["first_sample_at"] is not None
    assert len(body["quietest_hours"]) == 10
    assert body["quietest_hours"][0]["listeners_avg"] == 1.0
    assert body["quietest_hours"][0]["samples"] >= 1
    assert body["hottest_hours"][0]["listeners_avg"] == 25.0
    # Two adjacent quiet hours, so exactly one two-hour window sits at the top.
    assert body["quiet_windows"]
    best = body["quiet_windows"][0]
    assert best["listeners_avg"] == 1.0
    assert (best["end_hour"] - best["start_hour"]) % 24 == 2
    # The grid shape is unchanged, and the two companion grids match it.
    for key in (
        "listeners_hour_of_week",
        "listeners_max_hour_of_week",
        "listeners_samples_hour_of_week",
        "calls_hour_of_week",
    ):
        assert len(body[key]) == 7, key
        assert all(len(row) == 24 for row in body[key]), key


def test_the_samples_series(client, admin_token, configured, session) -> None:  # noqa: ANN001
    from app.bcfy_stats import store

    now = datetime.now(UTC).replace(minute=15, second=0, microsecond=0)
    for hours_ago in range(5):
        store.record_sample(
            session,
            feed_id="2211",
            document={"feed": {"currentListeners": hours_ago * 2}},
            polled_at=now - timedelta(hours=hours_ago),
        )
    session.commit()

    points = client.get(
        "/api/admin/bcfy-stats/samples",
        params={"from": (now - timedelta(days=1)).isoformat(), "step": "hour"},
        headers=bearer(admin_token),
    ).json()
    assert len(points) == 5
    assert points[0]["bucket"] < points[-1]["bucket"]
    assert points[-1]["listeners_avg"] == 0.0


def test_a_backwards_window_is_refused(client, admin_token, configured) -> None:  # noqa: ANN001
    response = client.get(
        "/api/admin/bcfy-stats/samples",
        params={"from": "2026-09-02", "to": "2026-09-01"},
        headers=bearer(admin_token),
    )
    assert response.status_code == 422


def test_the_archive_listing(client, admin_token, configured, session) -> None:  # noqa: ANN001
    from datetime import date

    from app.bcfy_stats import store
    from app.bcfy_stats.client import archive_rows, parse_body

    rows = archive_rows(
        parse_body((FIXTURES / "archives_day.json").read_text()), day=date(2026, 9, 1)
    )
    store.upsert_archives(session, feed_id="2211", rows=rows)
    session.commit()

    listed = client.get("/api/admin/bcfy-stats/archives", headers=bearer(admin_token)).json()
    assert len(listed) == 2
    assert listed[0]["url"].endswith(".mp3")
    # Newest first.
    assert listed[0]["start_at"] > listed[1]["start_at"]

    one_day = client.get(
        "/api/admin/bcfy-stats/archives",
        params={"day": "2026-09-02"},
        headers=bearer(admin_token),
    ).json()
    assert one_day == []


# --------------------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------------------


def test_probe_returns_the_raw_response_and_names_what_it_did_not_map(  # noqa: ANN001
    client, admin_token, configured, monkeypatch
) -> None:
    """The whole reason the probe exists: `unmapped_fields` is where the real names are."""
    import app.bcfy_stats.poller as poller_module
    from app.bcfy_stats.client import BcfyOwnerClient

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            json={"feed": {"currentListeners": 12, "somethingNobodyGuessed": "x", "descr": "F"}},
        )

    def make_client(settings, *, http_client=None):  # noqa: ANN001, ANN202, ARG001
        return BcfyOwnerClient(
            username="operator",
            password="hunter2",
            feed_id="2211",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(poller_module, "make_client", make_client)

    body = client.post("/api/admin/bcfy-stats/probe", headers=bearer(admin_token)).json()
    assert body["ok"] is True
    assert body["raw"]["feed"]["somethingNobodyGuessed"] == "x"
    assert body["extracted"]["listeners"] == 12
    assert "somethingNobodyGuessed" in body["unmapped_fields"]
    assert "currentListeners" not in body["unmapped_fields"]
    assert "hunter2" not in str(body)


def test_probe_reports_a_failure_without_leaking_the_password(  # noqa: ANN001
    client, admin_token, configured, monkeypatch
) -> None:
    import app.bcfy_stats.poller as poller_module
    from app.bcfy_stats.client import BcfyOwnerClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad credentials for {request.url}")

    def make_client(settings, *, http_client=None):  # noqa: ANN001, ANN202, ARG001
        return BcfyOwnerClient(
            username="operator",
            password="hunter2",
            feed_id="2211",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(poller_module, "make_client", make_client)
    body = client.post("/api/admin/bcfy-stats/probe", headers=bearer(admin_token)).json()
    assert body["ok"] is False
    assert "hunter2" not in body["error"]
    assert "401" in body["error"]


def test_the_sync_route_reports_the_backfill_and_takes_a_full_flag(  # noqa: ANN001
    client, admin_token, configured, session, monkeypatch
) -> None:
    """The route's contract: it says how much backlog it caught up on and how much is left.

    The sync itself is exercised against a stub transport in ``tests/test_bcfy_stats.py``;
    what is under test here is that ``full`` reaches the sync and that the report is not
    swallowed on its way back out.
    """
    from app.bcfy_stats import poller as bcfy_poller

    seen: dict[str, object] = {}

    def fake_sync(_session, _settings, **kwargs):  # noqa: ANN001, ANN202
        seen.update(kwargs)
        return {"rows": 4, "backfilled_days": 3, "backfill_remaining": 27, "errors": []}

    monkeypatch.setattr(bcfy_poller, "sync_archives", fake_sync)

    body = client.post("/api/admin/bcfy-stats/sync-archives", headers=bearer(admin_token)).json()
    assert body["backfilled_days"] == 3
    assert body["backfill_remaining"] == 27
    assert seen == {"force": True, "full": False}

    client.post(
        "/api/admin/bcfy-stats/sync-archives",
        params={"full": "true"},
        headers=bearer(admin_token),
    )
    assert seen["full"] is True


# --------------------------------------------------------------------------------------
# the public page
# --------------------------------------------------------------------------------------


def test_the_page_route_backfills_a_day_and_says_what_it_wrote(  # noqa: ANN001
    client, admin_token, configured, session, monkeypatch
) -> None:
    """One fetch of the public page, and the only backfill this feature has."""
    import app.bcfy_stats.public_page as page_module

    html = (FIXTURES / "feed_page_sample.html").read_text()
    monkeypatch.setattr(page_module, "fetch_public_page", lambda feed_id, http_client=None: html)  # noqa: ARG005

    body = client.post("/api/admin/bcfy-stats/poll-page", headers=bearer(admin_token)).json()
    assert body["ok"] is True
    assert len(body["bars"]) == 24
    assert body["bars"][0] == 4
    assert body["hours_written"] == 24
    assert body["online"] is True
    assert body["listeners"] == 0
    assert body["since"].startswith("2026-09-04")

    # …and the summary now counts those hours and says where they came from.
    summary = client.get(
        "/api/admin/bcfy-stats/summary", params={"days": 7}, headers=bearer(admin_token)
    ).json()
    assert summary["hours_from_page"] == 24
    assert summary["listeners_hours_covered"] == 24


def test_the_page_route_reports_a_page_that_would_not_load(  # noqa: ANN001
    client, admin_token, configured, monkeypatch
) -> None:
    import app.bcfy_stats.public_page as page_module

    def boom(feed_id, http_client=None):  # noqa: ANN001, ANN202, ARG001
        raise page_module.PublicPageError("HTTP 503")

    monkeypatch.setattr(page_module, "fetch_public_page", boom)
    body = client.post("/api/admin/bcfy-stats/poll-page", headers=bearer(admin_token)).json()
    assert body["ok"] is False
    assert body["error"] == "HTTP 503"
    assert body["bars"] == []
    assert body["hours_written"] == 0


def test_the_page_route_needs_only_a_feed_id_not_credentials(  # noqa: ANN001
    client, admin_token, session, monkeypatch
) -> None:
    """The page is public: a deployment with no Broadcastify login can still backfill."""
    import app.bcfy_stats.public_page as page_module
    from app import runtime_settings

    session.add(Setting(key="bcfy_feed_id", value="2211"))
    session.commit()
    runtime_settings.invalidate()

    html = (FIXTURES / "feed_page_sample.html").read_text()
    monkeypatch.setattr(page_module, "fetch_public_page", lambda feed_id, http_client=None: html)  # noqa: ARG005
    body = client.post("/api/admin/bcfy-stats/poll-page", headers=bearer(admin_token)).json()
    assert body["ok"] is True
    assert body["hours_written"] == 24

    # …but with no feed id at all there is nothing to fetch, and it says so.
    session.query(Setting).filter(Setting.key == "bcfy_feed_id").delete()
    session.commit()
    runtime_settings.invalidate()
    response = client.post("/api/admin/bcfy-stats/poll-page", headers=bearer(admin_token))
    assert response.status_code == 409


def test_the_page_cli_fetches_once_and_prints_what_it_wrote(monkeypatch, session) -> None:  # noqa: ANN001
    """`keyup bcfy-stats page` — the same pass, for a deployment without the web UI open."""
    import json as json_module

    from typer.testing import CliRunner

    import app.bcfy_stats.public_page as page_module
    from app import cli, runtime_settings

    session.add(Setting(key="bcfy_feed_id", value="2211"))
    session.commit()
    runtime_settings.invalidate()

    html = (FIXTURES / "feed_page_sample.html").read_text()
    monkeypatch.setattr(page_module, "fetch_public_page", lambda feed_id, http_client=None: html)  # noqa: ARG005

    result = CliRunner().invoke(cli.app, ["bcfy-stats", "page"])
    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["hours_written"] == 24
    assert len(payload["bars"]) == 24


def test_the_page_cli_exits_non_zero_when_the_page_will_not_load(monkeypatch, session) -> None:  # noqa: ANN001
    import json as json_module

    from typer.testing import CliRunner

    import app.bcfy_stats.public_page as page_module
    from app import cli, runtime_settings

    session.add(Setting(key="bcfy_feed_id", value="2211"))
    session.commit()
    runtime_settings.invalidate()

    def boom(feed_id, http_client=None):  # noqa: ANN001, ANN202, ARG001
        raise page_module.PublicPageError("HTTP 403")

    monkeypatch.setattr(page_module, "fetch_public_page", boom)
    result = CliRunner().invoke(cli.app, ["bcfy-stats", "page"])
    assert result.exit_code == 1
    assert json_module.loads(result.stdout)["error"] == "HTTP 403"


def test_probe_refuses_when_the_credentials_are_incomplete(  # noqa: ANN001
    client, admin_token, session
) -> None:
    from app import runtime_settings

    session.add(Setting(key="bcfy_feed_id", value="2211"))
    session.commit()
    runtime_settings.invalidate()
    response = client.post("/api/admin/bcfy-stats/probe", headers=bearer(admin_token))
    assert response.status_code == 409
    assert "bcfy_owner_username" in response.json()["detail"]


# --------------------------------------------------------------------------------------
# importing the dashboard exports
# --------------------------------------------------------------------------------------


HOURLY_CSV = FIXTURES / "owner_hourly_sample.csv"
UPTIME_CSV = FIXTURES / "owner_uptime_sample.csv"


def test_importing_the_hourly_export_reports_what_it_wrote(  # noqa: ANN001
    client, admin_token, configured
) -> None:
    with HOURLY_CSV.open("rb") as handle:
        response = client.post(
            "/api/admin/bcfy-stats/import",
            files={"file": ("owner_hourly_sample.csv", handle, "text/csv")},
            headers=bearer(admin_token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "hourly"
    assert body["rows_parsed"] == body["rows_written"] == 179
    assert body["rows_skipped"] == 0
    assert body["skipped_reasons"] == {}
    assert body["first"].startswith("2026-08-28T06:00")
    assert body["last"].startswith("2026-09-04T16:00")


def test_importing_the_daily_export_lands_in_the_summary(client, admin_token, configured) -> None:  # noqa: ANN001
    """The point of the whole feature, asserted end to end: a summary with months in it."""
    with UPTIME_CSV.open("rb") as handle:
        response = client.post(
            "/api/admin/bcfy-stats/import",
            files={"file": ("owner_uptime_sample.csv", handle, "text/csv")},
            headers=bearer(admin_token),
        )
    assert response.status_code == 200
    assert response.json()["format"] == "daily"

    body = client.get(
        "/api/admin/bcfy-stats/summary", params={"days": 365}, headers=bearer(admin_token)
    ).json()
    assert body["daily_days_covered"] == 179
    assert body["history_from"] == "2026-03-09"
    # The May incident, which nothing this deployment ever polled has seen.
    assert body["peak_listeners"] == 412
    assert body["peak_source"] == "daily"
    assert body["daily"][0] == {"day": "2026-03-09", "uptime_pct": 100.0, "listeners_max": 24}


def test_the_import_accepts_the_text_as_json(client, admin_token, configured) -> None:  # noqa: ANN001
    """The way the SPA sends it: the browser has already read the file."""
    response = client.post(
        "/api/admin/bcfy-stats/import",
        json={"content": HOURLY_CSV.read_text()},
        headers=bearer(admin_token),
    )
    assert response.status_code == 200
    assert response.json()["rows_written"] == 179


def test_a_file_that_is_not_an_export_is_refused_with_a_reason(  # noqa: ANN001
    client, admin_token, configured
) -> None:
    response = client.post(
        "/api/admin/bcfy-stats/import",
        json={"content": "Something,Else\n1,2\n"},
        headers=bearer(admin_token),
    )
    assert response.status_code == 422
    assert "dashboard export" in response.json()["detail"]


def test_an_empty_import_body_says_what_to_send(client, admin_token, configured) -> None:  # noqa: ANN001
    response = client.post("/api/admin/bcfy-stats/import", json={}, headers=bearer(admin_token))
    assert response.status_code == 422
    assert "content" in response.json()["detail"]


def test_the_import_is_capped_at_two_megabytes(client, admin_token, configured) -> None:  # noqa: ANN001
    """The cap is on `Content-Length` first, so the bytes are refused before they are read."""
    response = client.post(
        "/api/admin/bcfy-stats/import",
        content=b"x" * (2 * 1024 * 1024 + 1),
        headers={"content-type": "application/json", **bearer(admin_token)},
    )
    assert response.status_code == 413


def test_importing_requires_a_configured_feed(client, admin_token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/bcfy-stats/import",
        json={"content": UPTIME_CSV.read_text()},
        headers=bearer(admin_token),
    )
    assert response.status_code == 409
