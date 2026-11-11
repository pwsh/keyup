"""Outbound webhooks (``app/webhooks.py``): the queue, the signature and the backoff.

The receiver is mocked with respx rather than a real socket, so what is asserted is what
went on the wire — headers, body bytes, and the HMAC over exactly those bytes.  Two facts
carry most of the weight: the feature is off unless somebody turns it on, and enqueueing
happens in the caller's transaction so a rolled-back ingest leaves no promise behind.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, text

from app import webhooks
from app.config import Settings
from app.models import Call, ExternalLink, Setting, System, Talkgroup, WebhookDelivery

from .conftest import requires_db

pytestmark = requires_db

URL = "https://cfs.example.test/hooks/keyup"
SECRET = "a-shared-secret"


def _configure(
    session,  # noqa: ANN001
    *,
    events: str = "call.stored",
    secret: str = SECRET,
    scope: str | None = None,
) -> None:
    """Turn webhooks on the way an operator does — through the settings table."""
    from app import runtime_settings

    rows = [("webhook_url", URL), ("webhook_events", events), ("webhook_secret", secret)]
    if scope is not None:
        rows.append(("webhook_call_scope", scope))
    for key, value in rows:
        session.merge(Setting(key=key, value=value))
    session.commit()
    runtime_settings.invalidate()


def _call(session, *, index: int = 0, priority: str = "live") -> Call:  # noqa: ANN001
    system = session.scalars(select(System)).first()
    if system is None:
        system = System(name="Example System")
        session.add(system)
        session.flush()
    tg = session.scalars(select(Talkgroup).where(Talkgroup.tgid == 1101)).first()
    if tg is None:
        tg = Talkgroup(
            system_id=system.id, tgid=1101, alias="EC LAW 1", category="law", service_tag="Law Dispatch"
        )
        session.add(tg)
        session.flush()
    started = datetime(2026, 8, 29, 9, 3, tzinfo=UTC) + timedelta(seconds=index)
    call = Call(
        system_id=system.id,
        talkgroup_id=tg.id,
        talkgroup_ids=[tg.tgid],
        ingest_source="trunkd",
        ingest_priority=priority,
        recorded_at=started + timedelta(seconds=3),
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=3),
        duration_s=3,
        ingested_at=started + timedelta(seconds=4),
        file_path=f"uploads/hook-{index}.mp3",
        file_name=f"hook-{index}.mp3",
    )
    session.add(call)
    session.commit()
    return call


# --------------------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------------------


def test_the_feature_is_off_until_a_url_is_set(session) -> None:  # noqa: ANN001
    call = _call(session)
    assert webhooks.enqueue_call_event(session, "call.stored", call.id) is None
    assert session.scalars(select(WebhookDelivery)).all() == []


def test_a_url_with_no_events_sends_nothing(session) -> None:  # noqa: ANN001
    """Half a filled-in form is not consent."""
    _configure(session, events="")
    call = _call(session)
    assert webhooks.enqueue_call_event(session, "call.stored", call.id) is None


def test_an_unsubscribed_event_is_not_queued(session) -> None:  # noqa: ANN001
    _configure(session, events="call.transcribed")
    call = _call(session)
    assert webhooks.enqueue_call_event(session, "call.stored", call.id) is None
    assert webhooks.enqueue_call_event(session, "call.transcribed", call.id) is not None


def test_an_unknown_event_name_does_not_silence_the_others(session) -> None:  # noqa: ANN001
    _configure(session, events="call.stored, call.exploded")
    settings = Settings(_env_file=None, webhook_url=URL, webhook_events="call.stored, call.exploded")
    assert webhooks.subscribed_events(settings) == {"call.stored"}


# --------------------------------------------------------------------------------------
# enqueueing
# --------------------------------------------------------------------------------------


def test_the_payload_carries_what_a_consumer_needs_to_act(session) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    delivery = webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    assert delivery is not None
    body = delivery.payload
    assert body["event"] == "call.stored"
    assert body["call"]["id"] == call.id
    assert body["call"]["tgid"] == 1101
    assert body["call"]["talkgroup_alias"] == "EC LAW 1"
    assert body["call"]["category"] == "law"
    # The two links: correlate it, or play it. Both absolute, from public_base_url.
    assert body["call"]["url"].endswith(f"/calls/{call.id}")
    assert body["call"]["audio_url"].endswith(f"/api/calls/{call.id}/audio")
    # And whether this is news or an archive migration replaying 2016.
    assert body["call"]["ingest_priority"] == "live"


def test_a_rolled_back_transaction_leaves_no_promise(session) -> None:  # noqa: ANN001
    """The whole reason enqueue does not commit: an ingest that fails owes nothing."""
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.rollback()
    assert session.scalars(select(WebhookDelivery)).all() == []


def test_a_database_error_while_queueing_does_not_abort_the_ingest(session, monkeypatch) -> None:  # noqa: ANN001
    """Never raising must also mean never poisoning the caller's transaction.

    A swallowed failed statement leaves PostgreSQL's transaction aborted, and the ingest's
    COMMIT would then roll the call back without raising.
    """
    from app.db import SessionLocal

    _configure(session)
    call = _call(session)
    call.talker_alias = "written by the ingest"  # the caller's uncommitted work…
    session.flush()  # …already sent, as an ingest's row is by the time it queues a hook

    def broken(session, _call_id, _settings):  # noqa: ANN001, ANN202
        session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(webhooks, "_call_payload", broken)
    assert webhooks.enqueue_call_event(session, "call.stored", call.id) is None
    session.commit()

    with SessionLocal() as fresh:
        assert fresh.get(Call, call.id).talker_alias == "written by the ingest"
        assert fresh.scalars(select(WebhookDelivery)).all() == []


def test_a_link_event_names_the_record_it_is_about(session) -> None:  # noqa: ANN001
    _configure(session, events="link.created,link.deleted")
    call = _call(session)
    link = ExternalLink(
        call_id=call.id, source="cad", external_id="26143278", link_confidence="probable"
    )
    session.add(link)
    session.flush()
    delivery = webhooks.enqueue_link_event(session, "link.created", link)
    session.commit()

    assert delivery is not None
    assert delivery.payload["link"]["external_id"] == "26143278"
    assert delivery.payload["link"]["call_id"] == call.id
    assert delivery.subject_id == call.id


# --------------------------------------------------------------------------------------
# which calls (``webhook_call_scope``)
# --------------------------------------------------------------------------------------


def _link(session, call: Call) -> ExternalLink:  # noqa: ANN001
    """The record that says somebody claimed this call for an outside incident."""
    link = ExternalLink(
        call_id=call.id, source="cad", external_id=f"2614{call.id}", link_confidence="probable"
    )
    session.add(link)
    session.flush()
    return link


@pytest.mark.parametrize("event", ["call.stored", "call.transcribed"])
def test_the_default_scope_sends_every_call(session, event: str) -> None:  # noqa: ANN001
    """``all`` is the shipped value and the behaviour this had before the setting existed."""
    _configure(session, events="call.stored,call.transcribed")
    backlog = _call(session, priority="backlog")
    assert webhooks.enqueue_call_event(session, event, backlog.id) is not None


def test_live_drops_the_backlog_and_keeps_the_news(session) -> None:  # noqa: ANN001
    """The 312,000-a-day case: a migration replaying 2016 stops being a delivery."""
    _configure(session, events="call.transcribed", scope="live")
    backlog = _call(session, index=1, priority="backlog")
    live = _call(session, index=2, priority="live")

    assert webhooks.enqueue_call_event(session, "call.transcribed", backlog.id) is None
    assert webhooks.enqueue_call_event(session, "call.transcribed", live.id) is not None


def test_linked_sends_only_calls_somebody_has_claimed(session) -> None:  # noqa: ANN001
    """Priority is irrelevant here: a linked backlog call is exactly what ECC acts on."""
    _configure(session, events="call.transcribed", scope="linked")
    bare = _call(session, index=1, priority="live")
    claimed = _call(session, index=2, priority="backlog")
    _link(session, claimed)

    assert webhooks.enqueue_call_event(session, "call.transcribed", bare.id) is None
    assert webhooks.enqueue_call_event(session, "call.transcribed", claimed.id) is not None


def test_live_plus_linked_is_either_condition(session) -> None:  # noqa: ANN001
    _configure(session, events="call.transcribed", scope="live+linked")
    live = _call(session, index=1, priority="live")
    claimed = _call(session, index=2, priority="backlog")
    _link(session, claimed)
    neither = _call(session, index=3, priority="backlog")

    assert webhooks.enqueue_call_event(session, "call.transcribed", live.id) is not None
    assert webhooks.enqueue_call_event(session, "call.transcribed", claimed.id) is not None
    assert webhooks.enqueue_call_event(session, "call.transcribed", neither.id) is None


def test_a_link_event_is_never_filtered_by_the_call_scope(session) -> None:  # noqa: ANN001
    """A link is a person's deliberate act on one call; the scope has nothing to say."""
    _configure(session, events="link.created", scope="live")
    backlog = _call(session, priority="backlog")
    link = _link(session, backlog)
    assert webhooks.enqueue_link_event(session, "link.created", link) is not None


def test_an_unknown_scope_sends_everything_rather_than_going_quiet(session) -> None:  # noqa: ANN001
    """Failing open: a filter nobody understands must not silently stop the feed."""
    _configure(session, events="call.transcribed", scope="live-ish")
    backlog = _call(session, priority="backlog")
    assert webhooks.call_scope(Settings(_env_file=None, webhook_call_scope="live-ish")) == "all"
    assert webhooks.enqueue_call_event(session, "call.transcribed", backlog.id) is not None


# --------------------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------------------


def _worker(session_factory) -> webhooks.WebhookWorker:  # noqa: ANN001
    return webhooks.WebhookWorker(
        settings=Settings(_env_file=None), session_factory=session_factory
    )


@pytest.fixture
def session_factory(session):  # noqa: ANN001, ANN201
    """Hand the worker the test's own session, so everything is one transaction to inspect."""

    class _Kept:
        def __call__(self):  # noqa: ANN204
            return self

        def __getattr__(self, name):  # noqa: ANN001, ANN204
            return getattr(session, name)

        def close(self) -> None:
            """The worker closes what it opened; this session belongs to the test."""

    return _Kept()


@respx.mock
def test_a_delivery_is_signed_over_the_exact_body(session, session_factory) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    route = respx.post(URL).mock(return_value=httpx.Response(204))
    counts = _worker(session_factory).tick()

    assert counts == {"delivered": 1, "failed": 0}
    request = route.calls[0].request
    assert request.headers[webhooks.EVENT_HEADER] == "call.stored"
    assert request.headers[webhooks.DELIVERY_HEADER].isdigit()
    expected = "sha256=" + hmac.new(SECRET.encode(), request.content, hashlib.sha256).hexdigest()
    assert request.headers[webhooks.SIGNATURE_HEADER] == expected
    # The delivery id is in the body too, so a receiver can dedupe without reading headers.
    assert json.loads(request.content)["delivery_id"] == int(
        request.headers[webhooks.DELIVERY_HEADER]
    )

    row = session.scalars(select(WebhookDelivery)).one()
    assert (row.status, row.attempts, row.response_status) == ("delivered", 1, 204)


@respx.mock
def test_no_secret_means_no_signature_rather_than_a_worthless_one(session, session_factory) -> None:  # noqa: ANN001
    _configure(session, secret="")
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    route = respx.post(URL).mock(return_value=httpx.Response(200))
    _worker(session_factory).tick()
    assert webhooks.SIGNATURE_HEADER not in route.calls[0].request.headers


@respx.mock
def test_a_5xx_backs_off_and_retries(session, session_factory) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    respx.post(URL).mock(return_value=httpx.Response(503, text="down for maintenance"))
    assert _worker(session_factory).tick() == {"delivered": 0, "failed": 1}

    row = session.scalars(select(WebhookDelivery)).one()
    assert (row.status, row.attempts, row.response_status) == ("pending", 1, 503)
    assert "503" in row.last_error
    # Backed off, so the very next tick does not hammer a server that is already down.
    assert row.next_at > datetime.now(UTC)
    assert _worker(session_factory).tick() == {"delivered": 0, "failed": 0}


@respx.mock
def test_a_4xx_is_parked_at_once(session, session_factory) -> None:  # noqa: ANN001
    """"Not this, ever" — four more tries only fill the receiver's log and ours."""
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    respx.post(URL).mock(return_value=httpx.Response(422, text="unknown schema"))
    _worker(session_factory).tick()

    row = session.scalars(select(WebhookDelivery)).one()
    assert (row.status, row.attempts) == ("failed", 1)


@respx.mock
def test_a_429_is_a_retry_not_a_refusal(session, session_factory) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    respx.post(URL).mock(return_value=httpx.Response(429, text="slow down"))
    _worker(session_factory).tick()
    assert session.scalars(select(WebhookDelivery)).one().status == "pending"


@respx.mock
def test_a_transport_failure_is_just_a_failure(session, session_factory) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    respx.post(URL).mock(side_effect=httpx.ConnectError("no route to host"))
    assert _worker(session_factory).tick() == {"delivered": 0, "failed": 1}
    row = session.scalars(select(WebhookDelivery)).one()
    assert row.status == "pending"
    assert row.response_status is None


@respx.mock
def test_it_parks_after_the_attempt_limit(session, session_factory) -> None:  # noqa: ANN001
    _configure(session)
    session.merge(Setting(key="webhook_max_attempts", value=2))
    session.commit()
    from app import runtime_settings

    runtime_settings.invalidate()

    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    respx.post(URL).mock(return_value=httpx.Response(500))
    worker = _worker(session_factory)
    for _ in range(2):
        row = session.scalars(select(WebhookDelivery)).one()
        session.execute(
            WebhookDelivery.__table__.update()
            .where(WebhookDelivery.id == row.id)
            .values(next_at=datetime.now(UTC))
        )
        session.commit()
        worker.tick()

    row = session.scalars(select(WebhookDelivery)).one()
    assert (row.status, row.attempts) == ("failed", 2)


@respx.mock
def test_a_parked_delivery_can_be_requeued_with_a_clean_slate(session, session_factory) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    respx.post(URL).mock(return_value=httpx.Response(400))
    _worker(session_factory).tick()
    row = session.scalars(select(WebhookDelivery)).one()
    assert row.status == "failed"

    assert webhooks.requeue(session, row.id) is True
    session.commit()
    session.refresh(row)
    assert (row.status, row.attempts, row.last_error) == ("pending", 0, None)

    respx.post(URL).mock(return_value=httpx.Response(200))
    assert _worker(session_factory).tick() == {"delivered": 1, "failed": 0}


@respx.mock
def test_deliveries_go_out_one_at_a_time_and_in_order(session, session_factory) -> None:  # noqa: ANN001
    """Ordering matters: call.stored before call.transcribed for the same call."""
    _configure(session, events="call.stored,call.transcribed")
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    webhooks.enqueue_call_event(session, "call.transcribed", call.id)
    session.commit()

    route = respx.post(URL).mock(return_value=httpx.Response(200))
    assert _worker(session_factory).tick() == {"delivered": 2, "failed": 0}
    assert [c.request.headers[webhooks.EVENT_HEADER] for c in route.calls] == [
        "call.stored",
        "call.transcribed",
    ]


@respx.mock
def test_the_body_is_frozen_at_enqueue_not_re_read_on_retry(session, session_factory) -> None:  # noqa: ANN001
    """An event log sends what was true when the event happened."""
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    talkgroup.alias = "EC LAW 1 (renamed after the fact)"
    session.commit()

    route = respx.post(URL).mock(return_value=httpx.Response(200))
    _worker(session_factory).tick()
    assert json.loads(route.calls[0].request.content)["call"]["talkgroup_alias"] == "EC LAW 1"


@respx.mock
def test_a_tick_with_the_feature_off_sends_nothing(session, session_factory) -> None:  # noqa: ANN001
    """A queue left behind by a since-disabled subscription is not drained on the sly."""
    _configure(session)
    call = _call(session)
    webhooks.enqueue_call_event(session, "call.stored", call.id)
    session.commit()

    session.merge(Setting(key="webhook_url", value=""))
    session.commit()
    from app import runtime_settings

    runtime_settings.invalidate()

    route = respx.post(URL).mock(return_value=httpx.Response(200))
    assert _worker(session_factory).tick() == {"delivered": 0, "failed": 0}
    assert route.call_count == 0


def test_prune_drops_receipts_and_keeps_what_an_operator_would_retry(session) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    old = datetime.now(UTC) - timedelta(days=webhooks.DELIVERED_KEEP_DAYS + 1)
    for status in ("delivered", "failed", "pending"):
        row = webhooks.enqueue_call_event(session, "call.stored", call.id)
        row.status = status
        row.created_at = old
    session.commit()

    assert webhooks.prune(session) == 1
    assert sorted(r.status for r in session.scalars(select(WebhookDelivery))) == [
        "failed",
        "pending",
    ]


def test_prune_keeps_a_recent_receipt(session) -> None:  # noqa: ANN001
    _configure(session)
    call = _call(session)
    row = webhooks.enqueue_call_event(session, "call.stored", call.id)
    row.status = "delivered"
    session.commit()
    assert webhooks.prune(session) == 0


def test_backoff_widens_and_is_capped() -> None:
    assert webhooks.backoff_seconds(0) == 30
    assert webhooks.backoff_seconds(1) == 60
    assert webhooks.backoff_seconds(2) == 120
    assert webhooks.backoff_seconds(99) == webhooks.MAX_BACKOFF_S
