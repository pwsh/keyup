"""The ``emergency`` alert kind.

A P25 emergency flag reaches ``calls.emergency`` from trunkd and trunk-recorder, and until
now no rule could fire on it: a ``talkgroup`` rule matches ids only.  An ``emergency`` rule
fires on ``call.new`` for a flagged call, narrowed — or not — by talkgroups, categories
and exclusions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.alerts import emergency_scope, evaluate, matches, summarize
from app.config import Settings
from app.models import AlertRule, Call, Notification, System, Talkgroup
from tests.conftest import TEST_DATABASE_URL, requires_db

HOOK = "http://hooks.test/emergency"


def test_the_pattern_mixes_tgids_categories_and_exclusions() -> None:
    rule = AlertRule(name="r", kind="emergency", pattern="law, Fire 1011 -1507 -ems")
    assert emergency_scope(rule) == {
        "tgids": {1011},
        "exclude_tgids": {1507},
        "categories": {"law", "fire"},
        "exclude_categories": {"ems"},
    }
    by_filters = AlertRule(
        name="r",
        kind="emergency",
        filters={"talkgroup_ids": [1], "exclude_talkgroup_ids": "2, 3", "categories": ["LAW"]},
    )
    scope = emergency_scope(by_filters)
    assert scope["tgids"] == {1} and scope["exclude_tgids"] == {2, 3} and scope["categories"] == {"law"}


# --------------------------------------------------------------------------------------
# matching and delivery (database)
# --------------------------------------------------------------------------------------


@pytest.fixture
def alert_settings(tmp_path):  # noqa: ANN001, ANN201
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        public_base_url="http://keyup.test",
        alert_timeout_s=2.0,
    )


def seed(
    session, *, tgid: int, category: str | None, emergency: bool | None, system: str = "Example System"
) -> Call:  # noqa: ANN001
    row = session.scalar(select(System).where(System.name == system))
    if row is None:
        row = System(name=system)
        session.add(row)
        session.flush()
    talkgroup = session.scalar(select(Talkgroup).where(Talkgroup.system_id == row.id, Talkgroup.tgid == tgid))
    if talkgroup is None:
        talkgroup = Talkgroup(system_id=row.id, tgid=tgid, alias=f"TG {tgid}", category=category)
        session.add(talkgroup)
        session.flush()
    now = datetime.now(UTC)
    call = Call(
        system_id=row.id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[tgid],
        recorded_at=now,
        started_at=now,
        started_local=now.replace(tzinfo=None),
        ended_at=now + timedelta(seconds=4),
        duration_s=Decimal("4.000"),
        file_path=f"{tgid}/{now.timestamp()}.mp3",
        file_name="call.mp3",
        emergency=emergency,
    )
    session.add(call)
    session.commit()
    return call


def rule(session, **kw) -> AlertRule:  # noqa: ANN001, ANN003
    kw.setdefault("name", "emergencies")
    kw.setdefault("kind", "emergency")
    kw.setdefault("channels", ["webhook"])
    kw.setdefault("channel_config", {"webhook": {"url": HOOK}})
    row = AlertRule(**kw)
    session.add(row)
    session.commit()
    return row


@requires_db
def test_only_a_flagged_call_matches(session, alert_settings) -> None:  # noqa: ANN001
    every = rule(session)
    for flag in (False, None):
        call = seed(session, tgid=1011, category="law", emergency=flag)
        assert not matches(every, call, summarize(session, call.id, alert_settings))
    call = seed(session, tgid=1011, category="law", emergency=True)
    assert matches(every, call, summarize(session, call.id, alert_settings))


@requires_db
def test_categories_tgids_exclusions_and_systems_narrow_it(session, alert_settings) -> None:  # noqa: ANN001
    law = seed(session, tgid=1011, category="law", emergency=True)
    works = seed(session, tgid=1507, category="pw", emergency=True)
    fire = seed(session, tgid=1101, category="fire", emergency=True)
    other_system = seed(session, tgid=1011, category="law", emergency=True, system="Elsewhere")

    def fires(r: AlertRule) -> list[int]:
        return [
            c.id
            for c in (law, works, fire, other_system)
            if matches(r, c, summarize(session, c.id, alert_settings))
        ]

    assert fires(rule(session, pattern="law")) == [law.id, other_system.id]
    assert fires(rule(session, pattern="law 1101")) == [law.id, fire.id, other_system.id]
    # everything but the radio whose emergency button got stuck
    assert fires(rule(session, pattern="-1507")) == [law.id, fire.id, other_system.id]
    assert fires(rule(session, pattern="LAW", filters={"system": "Example System"})) == [law.id]


@requires_db
def test_an_emergency_is_delivered_on_call_new_and_says_so(session, alert_settings) -> None:  # noqa: ANN001
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    every = rule(session)
    call = seed(session, tgid=1011, category="law", emergency=True)
    quiet = seed(session, tgid=1011, category="law", emergency=False)

    # a transcript event does not run it; call.new does, once per cooldown
    assert evaluate(session, call.id, event="call.transcribed", settings=alert_settings, client=client) == []
    assert evaluate(session, quiet.id, event="call.new", settings=alert_settings, client=client) == []
    written = evaluate(session, call.id, event="call.new", settings=alert_settings, client=client)
    assert [(n.rule_id, n.ok) for n in written] == [(every.id, True)]
    (payload,) = sent
    assert payload["rule"]["kind"] == "emergency"
    assert payload["emergency"] is True and payload["category"] == "law"
    assert payload["talkgroup"]["tgid"] == 1011
    assert session.scalars(select(Notification)).one().subject == "Keyup: EMERGENCY on TG 1011"
