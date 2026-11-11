"""Archiving a recorder's raw event log: the upload, the verification, and the read side.

The bytes are the point of this endpoint, so the tests are about the bytes: what is on disk
afterwards, what is not on disk after a failure, and what the receipt says either way.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


@pytest.fixture
def event_log_root(tmp_path: Path, monkeypatch) -> Path:  # noqa: ANN001
    """Point ``EVENT_LOG_ROOT`` at a temporary tree for the duration of one test.

    The settings object is ``lru_cache``d, so the cache is cleared either side: a test that
    left a temporary path in it would make every later test write into a directory pytest
    has already deleted.
    """
    from app.config import get_settings

    root = tmp_path / "eventlogs"
    monkeypatch.setenv("EVENT_LOG_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _day(lines: int = 3) -> bytes:
    """A plausible ``.jsonl.gz`` day: one JSON object per decoded message."""
    body = "\n".join(
        json.dumps({"at": f"2026-09-01T00:0{i}:00Z", "op": "unit_registration", "radio": 5200000 + i})
        for i in range(lines)
    ).encode()
    return gzip.compress(body)


def _post(client, token, root, *, date="2026-09-01", instance="trunkd-garage", blob=None, sha=None, size=None):  # noqa: ANN001, PLR0913
    blob = _day() if blob is None else blob
    return client.post(
        "/api/recorder/event-log",
        data={
            "instance": instance,
            "date": date,
            "sha256": hashlib.sha256(blob).hexdigest() if sha is None else sha,
            "bytes": str(len(blob)) if size is None else str(size),
        },
        files={"file": (f"{date}.jsonl.gz", blob, "application/gzip")},
        headers=bearer(token),
    )


# ------------------------------------------------------------------------- the upload ---


def test_a_good_day_is_stored_and_verified(client, upload_token, event_log_root, session) -> None:
    from app.models import EventLogArchive

    blob = _day()
    resp = _post(client, upload_token, event_log_root, blob=blob)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"stored": True, "bytes": len(blob)}

    stored = event_log_root / "trunkd-garage" / "2026-09-01.jsonl.gz"
    assert stored.is_file()
    assert stored.read_bytes() == blob
    # …and it is still a readable gzip, which is the whole reason to keep it.
    assert gzip.decompress(stored.read_bytes()).count(b"\n") == 2

    session.expire_all()
    row = session.scalars(session.query(EventLogArchive).statement).one()
    assert row.instance == "trunkd-garage"
    assert row.date.isoformat() == "2026-09-01"
    assert row.bytes == len(blob)
    assert row.sha256 == hashlib.sha256(blob).hexdigest()
    assert row.path == str(stored)


def test_it_needs_the_upload_scope(client, read_token, event_log_root) -> None:
    assert _post(client, read_token, event_log_root).status_code == 403


def test_it_needs_a_token_at_all(client, event_log_root) -> None:
    blob = _day()
    resp = client.post(
        "/api/recorder/event-log",
        data={
            "instance": "trunkd-garage",
            "date": "2026-09-01",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": str(len(blob)),
        },
        files={"file": ("2026-09-01.jsonl.gz", blob, "application/gzip")},
    )
    assert resp.status_code == 401


def test_a_bad_digest_is_refused_and_leaves_nothing_behind(
    client, upload_token, event_log_root, session
) -> None:
    from app.models import EventLogArchive

    resp = _post(client, upload_token, event_log_root, sha="0" * 64)
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("sha256:")

    # No file, no receipt, and not even the temporary part file.
    directory = event_log_root / "trunkd-garage"
    assert list(directory.iterdir()) == []
    assert session.scalars(session.query(EventLogArchive).statement).all() == []


def test_a_digest_that_is_not_a_digest_is_refused(client, upload_token, event_log_root) -> None:
    resp = _post(client, upload_token, event_log_root, sha="not-a-sha")
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("sha256:")


def test_a_wrong_length_is_refused(client, upload_token, event_log_root) -> None:
    resp = _post(client, upload_token, event_log_root, size=999_999)
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("bytes:")
    assert list((event_log_root / "trunkd-garage").iterdir()) == []


def test_an_oversize_day_is_refused(client, upload_token, event_log_root, monkeypatch) -> None:
    """Over the limit is a 413, and the declared length is checked before a byte is read."""
    from app.config import get_settings

    monkeypatch.setenv("EVENT_LOG_MAX_BYTES", "512")
    get_settings.cache_clear()
    try:
        resp = _post(client, upload_token, event_log_root, blob=b"x" * 4096)
        assert resp.status_code == 413, resp.text
    finally:
        get_settings.cache_clear()


def test_an_oversize_body_is_refused_even_when_the_recorder_lies_about_it(
    client, upload_token, event_log_root, monkeypatch
) -> None:
    """The declared length is a courtesy; the stream is what is actually counted."""
    from app.config import get_settings

    monkeypatch.setenv("EVENT_LOG_MAX_BYTES", "512")
    get_settings.cache_clear()
    try:
        resp = _post(client, upload_token, event_log_root, blob=b"x" * 4096, size=10)
        assert resp.status_code == 413, resp.text
        assert list((event_log_root / "trunkd-garage").iterdir()) == []
    finally:
        get_settings.cache_clear()


def test_the_same_day_with_the_same_digest_is_a_409(client, upload_token, event_log_root) -> None:
    blob = _day()
    assert _post(client, upload_token, event_log_root, blob=blob).status_code == 200
    again = _post(client, upload_token, event_log_root, blob=blob)
    assert again.status_code == 409
    assert "already archived" in again.json()["detail"]


def test_the_same_day_rebuilt_replaces_the_stored_file(
    client, upload_token, event_log_root, session
) -> None:
    """A recorder that rebuilt a partly-written day must be able to ship the better copy."""
    from app.models import EventLogArchive

    first = _day(lines=2)
    second = _day(lines=9)
    assert _post(client, upload_token, event_log_root, blob=first).status_code == 200
    assert _post(client, upload_token, event_log_root, blob=second).status_code == 200

    stored = event_log_root / "trunkd-garage" / "2026-09-01.jsonl.gz"
    assert stored.read_bytes() == second
    session.expire_all()
    rows = session.scalars(session.query(EventLogArchive).statement).all()
    # One day, one row — the receipt followed the file rather than doubling up.
    assert len(rows) == 1
    assert rows[0].sha256 == hashlib.sha256(second).hexdigest()
    assert rows[0].bytes == len(second)


@pytest.mark.parametrize("date", ["not-a-date", "2026-13-01", "20260901", ""])
def test_a_bad_date_is_refused(client, upload_token, event_log_root, date) -> None:
    resp = _post(client, upload_token, event_log_root, date=date)
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("date:")


@pytest.mark.parametrize("instance", ["../escape", "a/b", ".hidden", "", "with space"])
def test_a_path_shaped_instance_is_refused(client, upload_token, event_log_root, instance) -> None:
    """The instance becomes a directory name, so it is checked as one rather than sanitized."""
    resp = _post(client, upload_token, event_log_root, instance=instance)
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("instance:")
    assert not event_log_root.exists() or list(event_log_root.iterdir()) == []


# --------------------------------------------------------------------------- the read ---


def test_admin_lists_and_downloads(client, upload_token, admin_token, event_log_root) -> None:
    blob = _day()
    _post(client, upload_token, event_log_root, blob=blob, date="2026-09-01")
    _post(client, upload_token, event_log_root, date="2026-08-31")
    _post(client, upload_token, event_log_root, instance="trunkd-tower", date="2026-09-01")

    rows = client.get("/api/admin/event-logs", headers=bearer(admin_token)).json()
    assert len(rows) == 3
    # Newest first.
    assert rows[0]["date"] == "2026-09-01"
    assert {r["instance"] for r in rows} == {"trunkd-garage", "trunkd-tower"}
    assert "path" not in rows[0]

    only = client.get(
        "/api/admin/event-logs", params={"instance": "trunkd-tower"}, headers=bearer(admin_token)
    ).json()
    assert [r["instance"] for r in only] == ["trunkd-tower"]

    target = next(r for r in rows if r["instance"] == "trunkd-garage" and r["date"] == "2026-09-01")
    download = client.get(
        f"/api/admin/event-logs/{target['id']}/download", headers=bearer(admin_token)
    )
    assert download.status_code == 200
    assert download.content == blob
    assert "trunkd-garage-2026-09-01.jsonl.gz" in download.headers["content-disposition"]


def test_the_read_side_is_admin_only(client, upload_token, read_token, event_log_root) -> None:
    _post(client, upload_token, event_log_root)
    assert client.get("/api/admin/event-logs", headers=bearer(read_token)).status_code == 403


def test_downloading_an_unknown_archive_is_a_404(client, admin_token, event_log_root) -> None:
    assert client.get("/api/admin/event-logs/9999/download", headers=bearer(admin_token)).status_code == 404


def test_a_receipt_whose_file_is_gone_is_a_410(
    client, upload_token, admin_token, event_log_root
) -> None:
    """The row is the record that the day *was* archived; it outlives the bytes on purpose."""
    _post(client, upload_token, event_log_root)
    archive_id = client.get("/api/admin/event-logs", headers=bearer(admin_token)).json()[0]["id"]
    (event_log_root / "trunkd-garage" / "2026-09-01.jsonl.gz").unlink()
    resp = client.get(f"/api/admin/event-logs/{archive_id}/download", headers=bearer(admin_token))
    assert resp.status_code == 410


def test_the_upload_needs_no_csrf_proof(client, upload_token, event_log_root) -> None:
    """A recorder is a daemon, not a browser: the route is in NO_CSRF_PATHS like its siblings."""
    from app.auth import NO_CSRF_PATHS, OPEN_MUTATION_PATHS

    assert "/api/recorder/event-log" in NO_CSRF_PATHS
    assert "/api/recorder/event-log" in OPEN_MUTATION_PATHS
    # …and it really does go through without the header a browser mutation needs.
    assert _post(client, upload_token, event_log_root).status_code == 200
    assert CSRF_HEADERS  # the browser form exists; this route simply does not need it
