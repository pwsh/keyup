"""``GET /api/status`` on its own — the health check the container and the SPA badge read.

The rest of the API is covered in ``tests/api/``; this file stays because the status probe
is what a deployment checks first and it must keep answering with the database attached.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import requires_db

pytestmark = requires_db


def test_status_reports_counts(session, settings, fixture_audio) -> None:  # noqa: ANN001
    from app.ingest.service import ingest_file
    from app.main import app

    ingest_file(session, fixture_audio / "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3", settings=settings, audio_root=fixture_audio)

    with TestClient(app) as client:
        response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["database"]["ok"] is True
    assert body["counts"]["calls"] == 1
    assert body["counts"]["talkgroups"] == 1
    assert body["counts"]["radios"] == 1
    assert body["counts"]["jobs_queued"] == 1
    assert body["tz"] == "America/Winnipeg"


def test_healthz_is_a_single_select(session) -> None:  # noqa: ANN001, ARG001
    """The liveness probe the container health check should poll instead of /api/status.

    Nothing counted, nothing cached, no ``shutil.disk_usage``: a probe running every ten
    seconds must not be the reason a five-gigabyte table gets scanned.  The assertion is on
    the statements, because "cheap" is the whole point of the route.
    """
    from sqlalchemy import event

    from app.db import engine
    from app.main import app

    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
        seen.append(" ".join(statement.split()))

    with TestClient(app) as client:
        event.listen(engine, "before_cursor_execute", before)
        try:
            response = client.get("/api/healthz")
        finally:
            event.remove(engine, "before_cursor_execute", before)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert [s for s in seen if "SELECT 1" not in s.upper()] == []


def test_status_says_how_old_its_ingest_block_is(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """``as_of``/``cached_s`` are added, optional, and beside the old fields, not instead."""
    from app.ingest.service import ingest_file
    from app.main import app

    ingest_file(session, fixture_audio / "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3", settings=settings, audio_root=fixture_audio)

    from app.auth import create_api_token

    # The ingest block is operator-only: a guest gets ``null`` (tests/api/test_admin_read_lock.py).
    _, token = create_api_token(session, "tests-read", ["read"], created_by="tests")
    with TestClient(app) as client:
        ingest = client.get("/api/status", headers={"Authorization": f"Bearer {token}"}).json()["ingest"]

    assert ingest["calls"] == 1  # the old shape is untouched
    assert ingest["ledger"] == {"ingested": 1}
    assert ingest["as_of"] is not None
    assert 0 <= ingest["cached_s"] < 60


def test_openapi_is_served_under_api(session) -> None:  # noqa: ANN001, ARG001
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/api/openapi.json").status_code == 200


def test_status_carries_the_upload_flow_gauge(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """The `queue` block is the same reading the push path decides on (docs/RECORDERS.md)."""
    from app.ingest import flow
    from app.ingest.service import ingest_file
    from app.main import app

    ingest_file(session, fixture_audio / "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3", settings=settings, audio_root=fixture_audio)
    flow.invalidate()

    with TestClient(app) as client:
        body = client.get("/api/status").json()

    queue = body["queue"]
    assert queue["live"] == 1  # the ingested call queued at the talkgroup's priority
    assert queue["backlog"] == 0
    assert queue["audio_seconds"] > 0
    assert queue["accepting"] == {"live": True, "backlog": True}
    assert queue["paused_backlog"] is False


def test_status_lag_is_one_indexed_read_of_the_newest_call(session, settings, fixture_audio) -> None:  # noqa: ANN001
    """The header badge polls this on every page, so it must stay one ``max(recorded_at)``.

    The badge used to read ``/api/stats/overview``, a dozen aggregates whose archive-wide
    half took fifteen seconds cold on production.  The assertion is on the statements, as
    for ``/api/healthz``, because cheap is the whole point of the route.
    """
    from sqlalchemy import event

    from app.db import engine
    from app.ingest.service import ingest_file
    from app.main import app

    with TestClient(app) as client:
        empty = client.get("/api/status/lag")
        assert empty.status_code == 200
        assert empty.json()["ingest_lag_s"] is None
        assert empty.json()["last_recorded_at"] is None

        ingest_file(session, fixture_audio / "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3", settings=settings, audio_root=fixture_audio)
        session.commit()

        seen: list[str] = []

        def before(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, PLR0913
            seen.append(" ".join(statement.split()))

        event.listen(engine, "before_cursor_execute", before)
        try:
            body = client.get("/api/status/lag").json()
        finally:
            event.remove(engine, "before_cursor_execute", before)

    assert body["ingest_lag_s"] is not None
    assert body["ingest_lag_s"] > 0  # the fixture call was recorded in August 2026
    assert body["last_recorded_at"] is not None
    touching_calls = [s for s in seen if "calls" in s]
    assert len(touching_calls) == 1, seen
    assert "max(calls.recorded_at)" in touching_calls[0]
