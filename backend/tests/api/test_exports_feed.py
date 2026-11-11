"""``GET /admin/exports``'s Broadcastify **Feed** block and its mount test.

The feed is the *live audio stream* product, not the per-call Calls archive, and this
suite's job is the same as the Exports tab's: prove that reading the page cannot start a
stream, that the mount password never comes back out, and that the test button says what
it actually did.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

FEED_SETTINGS = {
    "broadcastify_feed_server": "audio9.broadcastify.test",
    "broadcastify_feed_port": "80",
    "broadcastify_feed_mount": "testmount",
    "broadcastify_feed_password": "hunter2",
}


def test_the_feed_block_ships_off_and_unconfigured(client, read_token) -> None:  # noqa: ANN001
    feed = client.get("/api/admin/exports", headers=bearer(read_token)).json()["broadcastify_feed"]
    assert feed["enabled"] is False
    assert feed["configured"] is False
    assert feed["connected"] is False
    assert feed["enabled_at"] is None
    assert (feed["server"], feed["mount"]) == ("", "")
    assert feed["username"] == "source"
    assert (feed["bitrate_kbps"], feed["sample_rate"]) == (16, 22_050)
    assert feed["max_backlog_s"] == 120.0
    assert feed["calls_played"] == 0
    assert "password" not in feed


def test_the_feed_block_reports_what_broadcastifys_page_calls_the_fields(client, admin_token, read_token) -> None:  # noqa: ANN001
    """Server, Port, Mount, Username — the operator's feed page, field for field."""
    put = client.put("/api/admin/settings", json=FEED_SETTINGS, headers=bearer(admin_token))
    assert put.status_code == 200

    feed = client.get("/api/admin/exports", headers=bearer(read_token)).json()["broadcastify_feed"]
    assert feed["server"] == "audio9.broadcastify.test"
    assert feed["port"] == 80
    assert feed["mount"] == "/testmount"  # the leading slash is supplied for you
    assert feed["configured"] is True
    # …and still off. Configuring a feed is not starting one.
    assert feed["enabled"] is False
    assert feed["connected"] is False


def test_the_mount_password_is_never_read_back(client, admin_token, read_token) -> None:  # noqa: ANN001
    client.put("/api/admin/settings", json=FEED_SETTINGS, headers=bearer(admin_token))
    rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    assert rows["broadcastify_feed_password"]["kind"] == "secret"
    assert rows["broadcastify_feed_password"]["value"] == ""
    assert rows["broadcastify_feed_password"]["is_default"] is False
    body = client.get("/api/admin/exports", headers=bearer(read_token)).text
    assert "hunter2" not in body


def test_the_feed_switch_is_reported_from_the_runtime_setting(client, admin_token, read_token) -> None:  # noqa: ANN001
    put = client.put(
        "/api/admin/settings",
        json={**FEED_SETTINGS, "export_broadcastify_feed_enabled": "true"},
        headers=bearer(admin_token),
    )
    assert put.status_code == 200
    feed = client.get("/api/admin/exports", headers=bearer(read_token)).json()["broadcastify_feed"]
    assert feed["enabled"] is True
    # The watermark and the connection belong to the worker: the API stamps neither.
    assert feed["enabled_at"] is None
    assert feed["connected"] is False
    # The Calls exporter is a different product and is untouched by the feed's switch.
    services = {s["service"]: s for s in client.get("/api/admin/exports", headers=bearer(read_token)).json()["services"]}
    assert services["broadcastify"]["enabled"] is False


def test_the_feed_status_written_by_the_worker_is_reported(client, session, admin_token, read_token) -> None:  # noqa: ANN001
    from app.exporters.broadcastify_feed import FeedStatus, write_status

    client.put("/api/admin/settings", json=FEED_SETTINGS, headers=bearer(admin_token))
    write_status(
        session,
        FeedStatus(
            connected=True,
            since=datetime.now(UTC).isoformat(),
            bytes_sent=4096,
            calls_played=12,
            dropped=2,
            queued_s=3.5,
            last_error="the mount dropped the connection",
            last_error_at=datetime.now(UTC).isoformat(),
        ),
    )

    feed = client.get("/api/admin/exports", headers=bearer(read_token)).json()["broadcastify_feed"]
    assert feed["connected"] is True
    assert feed["bytes_sent"] == 4096
    assert feed["calls_played"] == 12
    assert feed["dropped"] == 2
    assert feed["queued_s"] == 3.5
    assert feed["last_error"] == "the mount dropped the connection"
    assert feed["since"] and feed["last_error_at"]


def test_testing_an_unconfigured_feed_dials_nothing(client, admin_token) -> None:  # noqa: ANN001
    response = client.post("/api/admin/exports/broadcastify-feed/test", headers=bearer(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "broadcastify_feed"
    assert body["ok"] is False
    assert "blank" in body["checked"]


def test_testing_the_feed_needs_admin(client) -> None:  # noqa: ANN001
    assert client.post("/api/admin/exports/broadcastify-feed/test").status_code in (401, 403)


def test_the_feed_test_reports_a_mount_that_will_not_take_a_source(client, admin_token, monkeypatch) -> None:  # noqa: ANN001
    """A refused mount is the answer an operator most often needs: somebody else has it."""
    from app.exporters import broadcastify_feed as module

    monkeypatch.setattr(module, "mp3_silence", lambda **_kw: b"\xff\xf3\x20\xc0" + b"\x00" * 48)
    client.put(
        "/api/admin/settings",
        json={**FEED_SETTINGS, "broadcastify_feed_server": "127.0.0.1", "broadcastify_feed_port": "9"},
        headers=bearer(admin_token),
    )
    body = client.post("/api/admin/exports/broadcastify-feed/test", headers=bearer(admin_token)).json()
    assert body["ok"] is False
    assert "/testmount" in body["checked"]
    assert body["detail"]


def test_the_feed_test_trusts_its_own_worker_rather_than_probing_twice(
    client, session, admin_token, monkeypatch
) -> None:  # noqa: ANN001
    """While the worker holds the mount a probe would be refused "Mountpoint in use" — by us."""
    from app.exporters import broadcastify_feed as module
    from app.exporters.broadcastify_feed import FeedStatus, write_status

    client.put(
        "/api/admin/settings",
        json={**FEED_SETTINGS, "broadcastify_feed_server": "127.0.0.1", "broadcastify_feed_port": "9"},
        headers=bearer(admin_token),
    )
    write_status(session, FeedStatus(connected=True, since="2026-09-04T04:18:25+00:00"))
    session.commit()

    def never(*_a, **_kw):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("the mount must not be probed while the worker is on it")

    monkeypatch.setattr(module, "probe_mount", never)
    body = client.post("/api/admin/exports/broadcastify-feed/test", headers=bearer(admin_token)).json()
    assert body["ok"] is True
    assert "/testmount" in body["checked"]
    assert "2026-09-04T04:18:25" in body["detail"]


def test_the_feed_is_not_a_ledger_service(client, admin_token, read_token) -> None:  # noqa: ANN001
    """It has no queue, no retries and no `call_exports` rows — and no `/test` of its own."""
    body = client.get("/api/admin/exports", headers=bearer(read_token)).json()
    assert {s["service"] for s in body["services"]} == {"broadcastify", "openmhz"}
    assert (
        client.post("/api/admin/exports/broadcastify_feed/test", headers=bearer(admin_token)).status_code
        == 404
    )
