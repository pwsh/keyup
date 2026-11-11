"""Ingest sources: the CRUD, the path check, and what a source has actually done.

The path check is the part with judgement in it.  A directory that does not exist is *not*
a reason to refuse the save — configuring a spool before its mount comes up is a normal
thing to do — so the endpoint stores it and reports it red, and the watcher skips it until
it appears.  That distinction is what most of this suite is about.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from app.ingest import sources as source_store
from app.models import ActivityEvent, IngestSource
from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


def admin(token: str) -> dict[str, str]:
    return {**bearer(token), **CSRF_HEADERS}


@pytest.fixture
def spool(tmp_path):  # noqa: ANN001, ANN201
    directory = tmp_path / "spool"
    directory.mkdir()
    return directory


# --------------------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------------------


def test_a_watch_dir_is_created_and_listed(client, admin_token, session, spool) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "second recorder", "path": str(spool)},
        headers=admin(admin_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["path_state"] == "ok"
    assert body["enabled"] is True

    page = client.get("/api/admin/ingest/sources", headers=bearer(admin_token)).json()
    assert [s["label"] for s in page["sources"]] == ["second recorder"]
    assert page["config_version"] >= 1

    assert session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "ingest.source_created")
    ).one()


def test_a_missing_path_is_saved_and_shown_red(client, admin_token, tmp_path) -> None:  # noqa: ANN001
    """Not a rejection: the mount may simply not be up yet."""
    response = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "nfs", "path": str(tmp_path / "not-yet")},
        headers=admin(admin_token),
    )
    assert response.status_code == 201, response.text
    assert response.json()["path_state"] == "missing"


def test_a_file_where_a_directory_belongs_is_named_as_such(client, admin_token, tmp_path) -> None:  # noqa: ANN001
    target = tmp_path / "a-file"
    target.write_text("not a directory")
    body = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "oops", "path": str(target)},
        headers=admin(admin_token),
    ).json()
    assert body["path_state"] == "not_a_directory"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_directory_is_named_as_such(tmp_path) -> None:  # noqa: ANN001
    directory = tmp_path / "locked"
    directory.mkdir(mode=0o000)
    try:
        state, detail = source_store.path_state(directory)
        assert state == "unreadable"
        assert "not readable" in detail
    finally:
        directory.chmod(0o755)


def test_a_watch_dir_needs_a_path_and_the_others_must_not_have_one(client, admin_token) -> None:  # noqa: ANN001
    no_path = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "x"},
        headers=admin(admin_token),
    )
    assert no_path.status_code == 422
    stray = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "migration", "label": "y", "path": "/tmp"},
        headers=admin(admin_token),
    )
    assert stray.status_code == 422


def test_two_sources_cannot_watch_the_same_directory(client, admin_token, spool) -> None:  # noqa: ANN001
    first = {"kind": "watch_dir", "label": "one", "path": str(spool)}
    assert client.post("/api/admin/ingest/sources", json=first, headers=admin(admin_token)).status_code == 201
    again = client.post(
        "/api/admin/ingest/sources",
        json={**first, "label": "two"},
        headers=admin(admin_token),
    )
    assert again.status_code == 409


def test_a_duplicate_label_is_a_conflict(client, admin_token, spool, tmp_path) -> None:  # noqa: ANN001
    other = tmp_path / "other"
    other.mkdir()
    client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "same", "path": str(spool)},
        headers=admin(admin_token),
    )
    clash = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "same", "path": str(other)},
        headers=admin(admin_token),
    )
    assert clash.status_code == 409


def test_editing_a_source_bumps_the_version_and_clears_a_stale_error(
    client, admin_token, session, spool, tmp_path
) -> None:  # noqa: ANN001
    created = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "one", "path": str(tmp_path / "gone")},
        headers=admin(admin_token),
    ).json()
    source_store.record_error(session, created["id"], "missing: it was not there")
    session.commit()
    before = source_store.version(session)

    updated = client.put(
        f"/api/admin/ingest/sources/{created['id']}",
        json={"kind": "watch_dir", "label": "one", "path": str(spool), "enabled": True},
        headers=admin(admin_token),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["path_state"] == "ok"
    assert updated.json()["last_error"] is None, "the old path's failure is not the new one's"
    session.expire_all()
    assert source_store.version(session) == before + 1


def test_a_sources_kind_cannot_be_changed(client, admin_token, spool) -> None:  # noqa: ANN001
    created = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "one", "path": str(spool)},
        headers=admin(admin_token),
    ).json()
    response = client.put(
        f"/api/admin/ingest/sources/{created['id']}",
        json={"kind": "migration", "label": "one"},
        headers=admin(admin_token),
    )
    assert response.status_code == 422


def test_deleting_a_source_bumps_the_version(client, admin_token, session, spool) -> None:  # noqa: ANN001
    created = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "one", "path": str(spool)},
        headers=admin(admin_token),
    ).json()
    before = source_store.version(session)
    response = client.delete(
        f"/api/admin/ingest/sources/{created['id']}", headers=admin(admin_token)
    )
    assert response.status_code == 204
    session.expire_all()
    assert session.get(IngestSource, created["id"]) is None
    assert source_store.version(session) == before + 1
    assert session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "ingest.source_deleted")
    ).one()


# --------------------------------------------------------------------------------------
# push tokens
# --------------------------------------------------------------------------------------


def test_a_push_source_names_a_token(client, admin_token, session) -> None:  # noqa: ANN001
    from app.auth import create_api_token

    token, _ = create_api_token(session, "recorder-2", ["upload"], created_by="tests")
    session.commit()
    response = client.post(
        "/api/admin/ingest/sources",
        json={
            "kind": "push_token",
            "label": "rec-02 (garage)",
            "token_id": token.id,
            "notes": "sdrtrunk on the attic box",
        },
        headers=admin(admin_token),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["token_label"] == "recorder-2"
    assert body["token_active"] is True
    assert body["path_state"] == "n/a"


def test_a_push_source_needs_a_real_token(client, admin_token) -> None:  # noqa: ANN001
    missing = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "push_token", "label": "x"},
        headers=admin(admin_token),
    )
    assert missing.status_code == 422
    unknown = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "push_token", "label": "x", "token_id": 9999},
        headers=admin(admin_token),
    )
    assert unknown.status_code == 404


def test_a_revoked_token_shows_the_source_as_inactive(client, admin_token, session) -> None:  # noqa: ANN001
    from datetime import UTC, datetime

    from app.auth import create_api_token

    token, _ = create_api_token(session, "old-recorder", ["upload"], created_by="tests")
    session.commit()
    client.post(
        "/api/admin/ingest/sources",
        json={"kind": "push_token", "label": "retired", "token_id": token.id},
        headers=admin(admin_token),
    )
    token.revoked_at = datetime.now(UTC)
    session.commit()
    page = client.get("/api/admin/ingest/sources", headers=bearer(admin_token)).json()
    assert page["sources"][0]["token_active"] is False


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------


def test_status_counts_the_ledger_under_a_watched_path(
    client, admin_token, session, fixture_audio, archive
) -> None:  # noqa: ANN001, ARG001
    """The counts come from ``ingest_files``, so they survive a watcher restart."""
    created = client.post(
        "/api/admin/ingest/sources",
        json={"kind": "watch_dir", "label": "archive", "path": str(fixture_audio / "20260828")},
        headers=admin(admin_token),
    ).json()
    body = client.get(
        f"/api/admin/ingest/sources/{created['id']}/status", headers=bearer(admin_token)
    ).json()
    assert body["path_state"] == "ok"
    assert body["files_new_24h"] >= 1, "the fixture ingest wrote ledger rows under this path"
    assert body["files_failed_24h"] == 0


def test_status_of_an_unknown_source_is_a_404(client, admin_token) -> None:  # noqa: ANN001
    assert (
        client.get("/api/admin/ingest/sources/999/status", headers=bearer(admin_token)).status_code
        == 404
    )


# --------------------------------------------------------------------------------------
# migration progress
# --------------------------------------------------------------------------------------


def test_a_migration_reports_its_own_progress(client, admin_token, session) -> None:  # noqa: ANN001
    first = client.post(
        "/api/admin/ingest/migration-progress",
        json={"label": "2019 archive", "state": "running", "files_total": 900, "files_done": 12},
        headers=admin(admin_token),
    )
    assert first.status_code == 200, first.text
    assert first.json()["kind"] == "migration"
    assert first.json()["meta"]["files_done"] == 12

    version = source_store.version(session)
    second = client.post(
        "/api/admin/ingest/migration-progress",
        json={"label": "2019 archive", "files_done": 900, "finished": True},
        headers=admin(admin_token),
    ).json()
    assert second["id"] == first.json()["id"], "the label names one row, not a new one each time"
    assert second["meta"]["files_done"] == 900
    assert second["meta"]["files_total"] == 900, "an omitted field is not a cleared one"
    assert second["meta"]["finished"] is True
    session.expire_all()
    assert source_store.version(session) == version, (
        "a progress report is not a configuration change and must not make the watcher reload"
    )


# --------------------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/admin/ingest/sources", None),
        ("post", "/api/admin/ingest/sources", {"kind": "migration", "label": "x"}),
        ("put", "/api/admin/ingest/sources/1", {"kind": "migration", "label": "x"}),
        ("delete", "/api/admin/ingest/sources/1", None),
        ("get", "/api/admin/ingest/sources/1/status", None),
        ("post", "/api/admin/ingest/migration-progress", {"label": "x"}),
    ],
)
def test_the_source_endpoints_need_an_admin(client, write_token, method, path, body) -> None:  # noqa: ANN001
    call = getattr(client, method)
    kwargs = {"json": body} if body is not None else {}
    assert call(path, headers=CSRF_HEADERS, **kwargs).status_code == 401
    assert call(path, headers=admin(write_token), **kwargs).status_code == 403
