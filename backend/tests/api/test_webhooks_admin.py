"""``GET /admin/webhooks``, the retry button, and the write-only signing secret."""

from __future__ import annotations

from sqlalchemy import select

from app import runtime_settings, webhooks
from app.models import Setting, WebhookDelivery
from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db

URL = "https://cfs.example.test/hooks/keyup"


def _configure(session, *, events: str = "call.stored") -> None:  # noqa: ANN001
    for key, value in (
        ("webhook_url", URL),
        ("webhook_events", events),
        ("webhook_secret", "a-shared-secret"),
    ):
        session.merge(Setting(key=key, value=value))
    session.commit()
    runtime_settings.invalidate()


def test_it_reports_the_feature_as_off_by_default(client, session, read_token) -> None:  # noqa: ANN001, ARG001
    body = client.get("/api/admin/webhooks", headers=bearer(read_token)).json()
    assert body["enabled"] is False
    assert body["url_set"] is False
    assert body["events"] == []
    assert body["known_events"] == [
        "call.stored",
        "call.transcribed",
        "link.created",
        "link.deleted",
    ]


def test_it_says_a_secret_is_set_without_ever_showing_it(client, session, read_token) -> None:  # noqa: ANN001
    _configure(session)
    body = client.get("/api/admin/webhooks", headers=bearer(read_token)).json()
    assert (body["enabled"], body["url_set"], body["secret_set"]) == (True, True, True)
    assert "a-shared-secret" not in client.get("/api/admin/webhooks", headers=bearer(read_token)).text


def test_the_settings_endpoint_never_hands_the_secret_back(client, session, admin_token, read_token) -> None:  # noqa: ANN001
    """Reads are open by default; a signing key that can be read is one anyone can forge with."""
    _configure(session)
    rows = {row["key"]: row for row in client.get("/api/admin/settings", headers=bearer(read_token)).json()}
    secret = rows["webhook_secret"]
    assert secret["kind"] == "secret"
    assert secret["value"] == ""
    assert secret["default_value"] == ""
    # …but it does say one is in force, which is all a reader needs.
    assert secret["is_default"] is False


def test_an_admin_can_still_set_and_clear_it(client, session, admin_token) -> None:  # noqa: ANN001
    client.put(
        "/api/admin/settings",
        json={"webhook_secret": "rotated"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert session.get(Setting, "webhook_secret").value == "rotated"

    client.put(
        "/api/admin/settings",
        json={"webhook_secret": ""},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    session.expire_all()
    assert session.get(Setting, "webhook_secret") is None


def test_a_stored_call_shows_up_in_the_queue(client, session, archive, read_token) -> None:  # noqa: ANN001
    _configure(session)
    call_id = archive.call_ids[0]
    webhooks.enqueue_call_event(session, "call.stored", call_id)
    session.commit()

    body = client.get("/api/admin/webhooks", headers=bearer(read_token)).json()
    assert body["counts"] == {"pending": 1}
    delivery = body["deliveries"][0]
    assert (delivery["event"], delivery["status"], delivery["subject_id"]) == (
        "call.stored",
        "pending",
        call_id,
    )
    assert delivery["payload"]["call"]["id"] == call_id


def test_retry_puts_a_parked_delivery_back_with_a_clean_slate(client, session, archive, admin_token) -> None:  # noqa: ANN001
    _configure(session)
    webhooks.enqueue_call_event(session, "call.stored", archive.call_ids[0])
    session.commit()
    row = session.scalars(select(WebhookDelivery)).one()
    row.status = "failed"
    row.attempts = 5
    row.last_error = "HTTP 500"
    session.commit()

    response = client.post(
        f"/api/admin/webhooks/deliveries/{row.id}/retry",
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["attempts"], body["last_error"]) == ("pending", 0, None)


def test_retrying_a_delivered_one_is_a_conflict(client, session, archive, admin_token) -> None:  # noqa: ANN001
    _configure(session)
    webhooks.enqueue_call_event(session, "call.stored", archive.call_ids[0])
    session.commit()
    row = session.scalars(select(WebhookDelivery)).one()
    row.status = "delivered"
    session.commit()

    response = client.post(
        f"/api/admin/webhooks/deliveries/{row.id}/retry",
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert response.status_code == 409


def test_retry_needs_the_admin_scope(client, session, archive, write_token) -> None:  # noqa: ANN001
    _configure(session)
    webhooks.enqueue_call_event(session, "call.stored", archive.call_ids[0])
    session.commit()
    row = session.scalars(select(WebhookDelivery)).one()

    response = client.post(
        f"/api/admin/webhooks/deliveries/{row.id}/retry",
        headers={**bearer(write_token), **CSRF_HEADERS},
    )
    assert response.status_code == 403


def test_creating_a_link_queues_one(client, session, archive, write_token, read_token) -> None:  # noqa: ANN001
    """The CAD integration write path: confirming a link tells every other consumer about it."""
    _configure(session, events="link.created,link.deleted")
    created = client.post(
        "/api/links",
        json={
            "call_id": archive.call_ids[0],
            "source": "cad",
            "external_id": "26143278",
            # A non-manual source has to name both: the defaults are a person's.
            "link_confidence": "probable",
            "link_method": "time+agency",
        },
        headers={**bearer(write_token), **CSRF_HEADERS},
    )
    assert created.status_code == 201
    link_id = created.json()["id"]

    deleted = client.delete(
        f"/api/links/{link_id}", headers={**bearer(write_token), **CSRF_HEADERS}
    )
    assert deleted.status_code == 204

    events = [d["event"] for d in client.get("/api/admin/webhooks", headers=bearer(read_token)).json()["deliveries"]]
    assert sorted(events) == ["link.created", "link.deleted"]
