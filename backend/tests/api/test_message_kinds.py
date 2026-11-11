"""The control-channel message registry: first sightings, changed beacons, the baseline.

docs/UNKNOWN-DATA-PLAN.md phase 5.  Everything here goes in through
``POST /api/recorder/observations`` — the same route the recorder posts — because the whole
point of the feature is that the memory is written in the same transaction as the window
that carried it, and a test that called the fold directly would not prove that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models import ActivityEvent, AlertRule, Notification, Site, SiteMessageKind
from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)


def observation(
    *,
    observed_at: datetime,
    mix: dict[str, int],
    exemplars: list[dict] | None = None,
    site: str = "Central",
) -> dict:
    """One window carrying nothing but the census — the only part this feature reads."""
    return {
        "recorder": "trunkd",
        "recorder_version": "0.0.1",
        "observed_at": observed_at.isoformat(),
        "window_s": 300,
        "system": {"label": "Example System", "wacn": 101010, "system_id": 52, "nac": 52},
        "site": {"label": site, "rfss": 10, "site": 1, "lra": 10},
        "control": {"frequency_hz": 851_050_000, "channel": "1-3", "tsbk_rate": 32.1},
        "message_mix": mix,
        "unknown_exemplars": exemplars or [],
    }


@pytest.fixture
def upload_token(session) -> str:  # noqa: ANN001
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-recorder", ["upload"], created_by="tests")
    return raw


def post(client, token: str, document: dict):  # noqa: ANN001, ANN201
    response = client.post("/api/recorder/observations", json=document, headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


def kinds(session, site_id: int) -> dict[tuple[int, int], SiteMessageKind]:  # noqa: ANN001
    session.expire_all()
    return {
        (row.mfid, row.opcode): row
        for row in session.scalars(select(SiteMessageKind).where(SiteMessageKind.site_id == site_id))
    }


def site_id_of(session, name: str = "Central") -> int:  # noqa: ANN001
    return session.scalars(select(Site).where(Site.name == name)).one().id


def unknown_activity(session) -> list[ActivityEvent]:  # noqa: ANN001
    session.expire_all()
    return list(
        session.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.kind.in_(("site.unknown_new", "site.unknown_changed")))
            .order_by(ActivityEvent.id)
        )
    )


def webhook_rule(session, kind: str) -> None:  # noqa: ANN001
    session.add(
        AlertRule(
            name=f"{kind} rule",
            kind=kind,
            channels=["webhook"],
            channel_config={"webhook": {"url": "https://example.invalid/hook"}},
        )
    )
    session.commit()


# --------------------------------------------------------------------------------------
# the fold
# --------------------------------------------------------------------------------------


def test_ingest_creates_a_row_per_kind_and_accumulates_counts(client, session, upload_token) -> None:
    post(client, upload_token, observation(observed_at=NOW, mix={"0:2": 10, "164:34": 3}))
    site = site_id_of(session)
    rows = kinds(session, site)
    assert set(rows) == {(0, 2), (164, 34)}
    assert rows[(0, 2)].count == 10
    assert rows[(0, 2)].first_seen_at == NOW
    # A standard opcode is named as it is inserted, from the built-in P25 table.
    assert rows[(0, 2)].known is True
    assert rows[(0, 2)].label == "GRP_VCH_GRNT_UPD"
    assert rows[(164, 34)].known is False  # nothing names this one

    later = NOW + timedelta(minutes=5)
    post(client, upload_token, observation(observed_at=later, mix={"0:2": 4, "0:3": 1}))
    rows = kinds(session, site)
    assert rows[(0, 2)].count == 14
    assert rows[(0, 2)].first_seen_at == NOW
    assert rows[(0, 2)].last_seen_at == later
    assert rows[(0, 3)].count == 1  # a kind the first window never carried


def test_the_first_window_into_an_empty_table_seeds_it_silently(client, session, upload_token) -> None:
    """Otherwise the first deploy would announce every kind the site has always sent."""
    webhook_rule(session, "new_kind")
    post(client, upload_token, observation(observed_at=NOW, mix={"0:2": 10, "164:34": 3}))

    assert unknown_activity(session) == []
    assert list(session.scalars(select(Notification))) == []
    assert all(row.alerted_at is None for row in kinds(session, site_id_of(session)).values())


def test_a_kind_never_heard_before_is_an_event_and_only_once(client, session, upload_token) -> None:
    post(client, upload_token, observation(observed_at=NOW, mix={"0:2": 10}))  # the baseline
    post(
        client,
        upload_token,
        observation(
            observed_at=NOW + timedelta(minutes=5),
            mix={"0:2": 10, "164:34": 2},
            exemplars=[{"mfid": 164, "opcode": 34, "exemplar": "a2a4b1c0deadbeef"}],
        ),
    )
    post(client, upload_token, observation(observed_at=NOW + timedelta(minutes=10), mix={"164:34": 2}))

    events = unknown_activity(session)
    assert [event.kind for event in events] == ["site.unknown_new"]
    assert "new control-channel message kind 164:34 on Central" in events[0].message
    assert "first exemplar a2a4b1c0…" in events[0].message
    assert events[0].source == "site"
    assert events[0].level == "warn"
    assert events[0].meta["key"] == "164:34"


def test_a_finding_delivers_on_a_rule_of_its_own_kind(client, session, upload_token, settings) -> None:
    """The outbound half: `new_kind` and `exemplar_changed` are ordinary alert rules."""
    from app.rr.message_kinds import raise_message_kind_alerts, record_message_kinds

    post(client, upload_token, observation(observed_at=NOW, mix={"0:2": 10}))  # the baseline
    site = site_id_of(session)
    webhook_rule(session, "new_kind")

    findings = record_message_kinds(
        session,
        site_id=site,
        payload={
            "message_mix": {"164:34": 2},
            "unknown_exemplars": [{"mfid": 164, "opcode": 34, "exemplar": "a2a4b1c0deadbeef"}],
        },
        observed_at=NOW + timedelta(minutes=5),
    )
    assert [finding.kind for finding in findings] == ["new_kind"]

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        written = raise_message_kind_alerts(session, findings, settings=settings, client=http)
    session.commit()

    assert len(written) == 1
    assert written[0].subject == "Keyup: new message kind 164:34 at Central"
    assert written[0].ok is True
    assert len(seen) == 1
    assert '"key": "164:34"' in seen[0]
    assert '"site": "Central"' in seen[0]
    assert '"count": 2' in seen[0]
    assert '"exemplar": "a2a4b1c0deadbeef"' in seen[0]

    # A rule of the *other* kind never sees this finding.
    session.execute(select(AlertRule))
    for rule in session.scalars(select(AlertRule)):
        rule.kind = "exemplar_changed"
    session.commit()
    assert raise_message_kind_alerts(session, findings, settings=settings) == []


def test_a_beacon_changing_its_bytes_is_an_event_but_a_rare_message_is_not(
    client, session, upload_token
) -> None:
    beacon = {"mfid": 164, "opcode": 34, "exemplar": "a" * 16}
    rare = {"mfid": 164, "opcode": 35, "exemplar": "b" * 16}
    post(
        client,
        upload_token,
        observation(observed_at=NOW, mix={"164:34": 2_000, "164:35": 5}, exemplars=[beacon, rare]),
    )
    site = site_id_of(session)
    assert kinds(session, site)[(164, 34)].exemplar == "a" * 16

    post(
        client,
        upload_token,
        observation(
            observed_at=NOW + timedelta(minutes=5),
            mix={"164:34": 2_000, "164:35": 5},
            exemplars=[
                {"mfid": 164, "opcode": 34, "exemplar": "c" * 16},
                {"mfid": 164, "opcode": 35, "exemplar": "d" * 16},
            ],
        ),
    )

    rows = kinds(session, site)
    # Both rows record the change; only the beacon is worth telling anybody about.
    assert rows[(164, 34)].exemplar == "c" * 16
    assert rows[(164, 34)].exemplar_previous == "a" * 16
    assert rows[(164, 34)].exemplar_changed_at == NOW + timedelta(minutes=5)
    assert rows[(164, 35)].exemplar_previous == "b" * 16

    events = unknown_activity(session)
    assert [event.kind for event in events] == ["site.unknown_changed"]
    assert "164:34" in events[0].message and "4,000 seen" in events[0].message
    assert rows[(164, 35)].alerted_at is None


def test_one_event_per_kind_per_24_hours(client, session, upload_token) -> None:
    """A beacon alternating between two payloads must not alert on every window."""
    post(
        client,
        upload_token,
        observation(
            observed_at=NOW,
            mix={"164:34": 2_000},
            exemplars=[{"mfid": 164, "opcode": 34, "exemplar": "a" * 16}],
        ),
    )
    for minutes, body in ((5, "c"), (10, "a"), (15, "c")):
        post(
            client,
            upload_token,
            observation(
                observed_at=NOW + timedelta(minutes=minutes),
                mix={"164:34": 2_000},
                exemplars=[{"mfid": 164, "opcode": 34, "exemplar": body * 16}],
            ),
        )
    assert len(unknown_activity(session)) == 1

    # A day later the same flap is news again — the site is still doing something.
    post(
        client,
        upload_token,
        observation(
            observed_at=NOW + timedelta(hours=25),
            mix={"164:34": 2_000},
            exemplars=[{"mfid": 164, "opcode": 34, "exemplar": "e" * 16}],
        ),
    )
    assert len(unknown_activity(session)) == 1  # the limiter runs on wall clock, not the window

    row = kinds(session, site_id_of(session))[(164, 34)]
    row.alerted_at = datetime.now(UTC) - timedelta(hours=25)
    session.commit()
    post(
        client,
        upload_token,
        observation(
            observed_at=NOW + timedelta(hours=26),
            mix={"164:34": 2_000},
            exemplars=[{"mfid": 164, "opcode": 34, "exemplar": "f" * 16}],
        ),
    )
    assert len(unknown_activity(session)) == 2


# --------------------------------------------------------------------------------------
# the baseline
# --------------------------------------------------------------------------------------


def test_seeding_replays_the_stored_windows_and_says_nothing(client, session, upload_token) -> None:
    from app.models import RecorderObservation
    from app.rr.message_kinds import seed_message_kinds

    post(client, upload_token, observation(observed_at=NOW, mix={"0:2": 10}))
    post(client, upload_token, observation(observed_at=NOW + timedelta(minutes=5), mix={"0:2": 4, "0:3": 1}))
    site = site_id_of(session)

    # Wipe the registry and rebuild it from the payloads alone.
    for row in list(kinds(session, site).values()):
        session.delete(row)
    session.commit()
    webhook_rule(session, "new_kind")

    before = len(unknown_activity(session))
    result = seed_message_kinds(session)
    assert result.windows == session.query(RecorderObservation).count()
    assert result.sites == 1
    assert result.kinds == 2
    assert result.per_site == {"Central": 2}

    rows = kinds(session, site)
    assert rows[(0, 2)].count == 14
    assert rows[(0, 2)].first_seen_at == NOW
    assert len(unknown_activity(session)) == before  # the replay says nothing new
    assert list(session.scalars(select(Notification))) == []


# --------------------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------------------


def test_the_list_route_is_newest_first_and_filters_to_the_unnamed(client, session, upload_token) -> None:
    # Neither pair is in the built-in table, so both start unnamed and the filter has
    # something to filter.
    post(client, upload_token, observation(observed_at=NOW, mix={"164:35": 10}))
    post(client, upload_token, observation(observed_at=NOW + timedelta(minutes=5), mix={"164:34": 3}))
    site = site_id_of(session)

    rows = client.get(f"/api/sites/{site}/message-kinds").json()
    assert [row["key"] for row in rows] == ["164:34", "164:35"]
    assert rows[0]["count"] == 3
    assert rows[0]["known"] is False

    named = kinds(session, site)[(164, 34)]
    named.known = True
    session.commit()
    assert [row["key"] for row in client.get(f"/api/sites/{site}/message-kinds").json()] == [
        "164:34",
        "164:35",
    ]
    unknown = client.get(f"/api/sites/{site}/message-kinds", params={"unknown_only": True}).json()
    assert [row["key"] for row in unknown] == ["164:35"]
    assert client.get("/api/sites/999999/message-kinds").status_code == 404


def test_an_admin_names_a_kind_and_a_guest_may_not(client, session, upload_token, admin_token) -> None:
    post(client, upload_token, observation(observed_at=NOW, mix={"164:34": 3}))
    site = site_id_of(session)
    path = f"/api/admin/sites/{site}/message-kinds/164/34"

    assert client.patch(path, json={"known": True}, headers=CSRF_HEADERS).status_code == 401

    body = client.patch(
        path,
        json={"known": True, "label": "Harris site descriptor"},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert body.status_code == 200, body.text
    assert body.json()["known"] is True
    assert body.json()["label"] == "Harris site descriptor"
    assert kinds(session, site)[(164, 34)].known is True

    missing = client.patch(
        f"/api/admin/sites/{site}/message-kinds/164/99",
        json={"known": True},
        headers={**bearer(admin_token), **CSRF_HEADERS},
    )
    assert missing.status_code == 404
