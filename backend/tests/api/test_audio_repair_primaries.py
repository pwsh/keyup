"""``POST /api/admin/audio/repair-primaries`` — the one-shot sweep for demoted primaries.

Reconciliation queues the processing pass for whichever copy is primary *at ingest*.  When
a better copy arrives later and takes the seat, the row now being played was never queued:
``processed_path IS NULL`` on the primary, and the file that does exist belongs to a hidden
copy.  ``app/ingest/reconcile.py`` re-queues on the swap itself now; this endpoint is for
the rows that were swapped before it did.
"""

from __future__ import annotations

from sqlalchemy import select

from tests.api.conftest import CSRF_HEADERS


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **CSRF_HEADERS}


def clear_jobs(session) -> None:  # noqa: ANN001
    from app.models import AudioJob

    session.query(AudioJob).delete()
    session.commit()


def _demoted_pair(session):  # noqa: ANN001, ANN202
    """Make one primary that has a copy and no processed file, and one that has neither.

    Returns ``(needs_repair_id, untouched_id)``.  The second is the control: a primary with
    no copies is simply a call the queue has not reached, and sweeping it in would make the
    count meaningless as a measure of the bug.
    """
    from app.models import Call

    calls = list(session.scalars(select(Call).order_by(Call.id)).all())
    primary, copy, control = calls[0], calls[1], calls[2]
    copy.duplicate_of = primary.id
    primary.processed_path = None
    control.duplicate_of = None
    control.processed_path = None
    session.commit()
    return primary.id, control.id


def test_the_sweep_queues_a_primary_left_without_processed_audio(
    client, admin_token, archive, session
) -> None:  # noqa: ANN001
    from app.models import AudioJob

    clear_jobs(session)
    target_id, control_id = _demoted_pair(session)

    response = client.post(
        "/api/admin/audio/repair-primaries", json={}, headers=auth(admin_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] == 1
    assert body["queued"] == 1
    assert body["already_open"] == 0
    assert body["priority"] == 600

    job = session.scalars(select(AudioJob).where(AudioJob.call_id == target_id)).one()
    assert (job.status, job.priority) == ("queued", 600)
    # the primary with no copies is not the bug and is not swept in
    assert session.scalars(select(AudioJob).where(AudioJob.call_id == control_id)).all() == []


def test_the_sweep_is_safe_to_repeat(client, admin_token, archive, session) -> None:  # noqa: ANN001
    clear_jobs(session)
    _demoted_pair(session)
    client.post("/api/admin/audio/repair-primaries", json={}, headers=auth(admin_token))

    again = client.post(
        "/api/admin/audio/repair-primaries", json={}, headers=auth(admin_token)
    ).json()
    assert again["matched"] == 1
    assert again["queued"] == 0
    assert again["already_open"] == 1


def test_since_narrows_the_window(client, admin_token, archive, session) -> None:  # noqa: ANN001
    clear_jobs(session)
    _demoted_pair(session)
    body = client.post(
        "/api/admin/audio/repair-primaries",
        json={"since": "2099-01-01T00:00:00Z"},
        headers=auth(admin_token),
    ).json()
    assert (body["matched"], body["queued"]) == (0, 0)


def test_a_bad_since_is_a_422(client, admin_token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/audio/repair-primaries", json={"since": "yesterday"}, headers=auth(admin_token)
    )
    assert response.status_code == 422


def test_a_reader_may_not_run_the_sweep(client, read_token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/admin/audio/repair-primaries", json={}, headers=auth(read_token)
    )
    assert response.status_code == 403
