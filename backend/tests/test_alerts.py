"""Alert matching, cooldown and delivery.

Deliveries are mocked with respx: the point is the request Keyup *would* send and the
``notifications`` row it writes either way, not whether ntfy is up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select

from app.alerts import evaluate, matches, summarize
from app.config import Settings
from app.models import AlertRule, Call, Notification, Radio, System, Talkgroup
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

WEBHOOK = "https://hooks.example.test/keyup"
DISCORD = "https://discord.example.test/api/webhooks/1/abc"
NTFY = "https://ntfy.example.test"


@pytest.fixture
def alert_settings(tmp_path):  # noqa: ANN001, ANN201
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        public_base_url="http://192.0.2.60",
        ntfy_url=NTFY,
        alert_timeout_s=2.0,
    )


def seed_call(session, *, tgid: int = 1101, text: str | None = None, radio_id: int = 5200007) -> Call:  # noqa: ANN001
    system = session.execute(select(System).where(System.name == "Example System")).scalar_one_or_none()
    if system is None:
        system = System(name="Example System")
        session.add(system)
        session.flush()
    talkgroup = session.execute(
        select(Talkgroup).where(Talkgroup.system_id == system.id, Talkgroup.tgid == tgid)
    ).scalar_one_or_none()
    if talkgroup is None:
        talkgroup = Talkgroup(system_id=system.id, tgid=tgid, alias="EC FIRE DISPATCH")
        session.add(talkgroup)
        session.flush()
    radio = session.execute(
        select(Radio).where(Radio.system_id == system.id, Radio.radio_id == radio_id)
    ).scalar_one_or_none()
    if radio is None:
        radio = Radio(system_id=system.id, radio_id=radio_id, unit_id="E1")
        session.add(radio)
        session.flush()

    started = datetime.now(UTC)
    call = Call(
        system_id=system.id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[tgid],
        radio_id=radio.id,
        recorded_at=started,
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=4),
        duration_s=Decimal("4.000"),
        file_path=f"{tgid}/{started.timestamp()}.mp3",
        file_name="call.mp3",
        talkgroup_alias_at_record="EC FIRE DISPATCH",
        transcript_text=text,
        transcript_status="done" if text else "queued",
        transcript_quality="good" if text else None,
    )
    session.add(call)
    session.commit()
    return call


def rule(session, **kw) -> AlertRule:  # noqa: ANN001, ANN003
    kw.setdefault("name", "test rule")
    kw.setdefault("channels", ["webhook"])
    kw.setdefault("channel_config", {"webhook": {"url": WEBHOOK}})
    row = AlertRule(**kw)
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------------------


def test_a_talkgroup_rule_matches_by_tgid(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    summary = summarize(session, call.id, alert_settings)
    assert matches(rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]}), call, summary)
    assert not matches(rule(session, kind="talkgroup", filters={"talkgroup_ids": [999]}), call, summary)


def test_a_talkgroup_rule_accepts_a_comma_separated_pattern(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    summary = summarize(session, call.id, alert_settings)
    assert matches(rule(session, kind="talkgroup", pattern="1101, 1102"), call, summary)


def test_a_talkgroup_rule_never_matches_the_row_id(session, alert_settings) -> None:  # noqa: ANN001
    """The pk and the tgid are different number spaces that overlap; a rule naming a number
    means the channel on the air with that number, never the row that happens to carry it
    as its id."""
    call = seed_call(session)
    summary = summarize(session, call.id, alert_settings)
    pk = call.talkgroup_id
    assert pk != 1101
    assert not matches(rule(session, kind="talkgroup", filters={"talkgroup_ids": [pk]}), call, summary)
    assert not matches(rule(session, kind="talkgroup", pattern=str(pk)), call, summary)
    assert not matches(rule(session, kind="talkgroup", filters={"talkgroups": [pk]}), call, summary)
    assert matches(rule(session, kind="talkgroup", filters={"tgids": [1101]}), call, summary)


def test_a_talkgroup_rule_can_be_scoped_to_a_system(session, alert_settings) -> None:  # noqa: ANN001
    """A tgid two systems both use is told apart by the rule's ``system`` — id or name, the
    same vocabulary as ``GET /calls?system=``."""
    call = seed_call(session)
    summary = summarize(session, call.id, alert_settings)
    other = System(name="Other Network")
    session.add(other)
    session.commit()

    def scoped(system):  # noqa: ANN001, ANN202
        return rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101], "system": system})

    assert matches(scoped([call.system_id]), call, summary)
    assert matches(scoped("example system"), call, summary)
    assert matches(scoped(f"{other.id}, {call.system_id}"), call, summary)
    assert not matches(scoped([other.id]), call, summary)
    assert not matches(scoped("Other Network"), call, summary)
    assert matches(scoped([]), call, summary)  # an empty scope is no scope


def test_a_radio_rule_matches_the_unit_number(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    summary = summarize(session, call.id, alert_settings)
    assert matches(rule(session, kind="radio", filters={"radio_ids": [5200007]}), call, summary)
    assert not matches(rule(session, kind="radio", filters={"radio_ids": [4831120]}), call, summary)


def test_a_radio_rule_never_matches_the_row_id(session, alert_settings) -> None:  # noqa: ANN001
    """``calls.radio_id`` is a row id and radio numbers overlap it.

    Radio 5200007 is keyed up on a call; another radio's on-air number is set to the first
    radio's *row id*.  A rule typed with that number names the other radio, so it must not
    fire on the first radio's call — and it must fire on the other radio's own call.
    """
    call = seed_call(session)
    summary = summarize(session, call.id, alert_settings)
    row_id = call.radio_id
    assert row_id != 5200007
    colliding = seed_call(session, radio_id=row_id)  # a radio whose number is that row id
    colliding_summary = summarize(session, colliding.id, alert_settings)
    assert colliding.radio_id != row_id  # …and whose own row id is something else

    by_row_id = rule(session, kind="radio", filters={"radio_ids": [row_id]})
    assert not matches(by_row_id, call, summary)
    assert not matches(rule(session, kind="radio", pattern=str(row_id)), call, summary)
    assert not matches(rule(session, kind="radio", filters={"radios": [row_id]}), call, summary)
    assert matches(by_row_id, colliding, colliding_summary)
    # The colliding radio's own row id names nothing on the air.
    assert not matches(
        rule(session, kind="radio", filters={"radio_ids": [colliding.radio_id]}), colliding, colliding_summary
    )
    assert matches(rule(session, kind="radio", filters={"radio_ids": [5200007]}), call, summary)
    assert summary.as_dict()["radio"] == {"id": row_id, "number": 5200007, "label": "E1"}


def test_a_radio_rule_can_be_scoped_to_a_system(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    summary = summarize(session, call.id, alert_settings)
    other = System(name="Other Network")
    session.add(other)
    session.commit()

    def scoped(system):  # noqa: ANN001, ANN202
        return rule(session, kind="radio", filters={"radio_ids": [5200007], "system": system})

    assert matches(scoped("Example System"), call, summary)
    assert not matches(scoped([other.id]), call, summary)


def test_a_radio_rule_never_matches_a_call_without_a_radio(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    call.radio_id = None
    session.commit()
    summary = summarize(session, call.id, alert_settings)
    assert not matches(rule(session, kind="radio", filters={"radio_ids": [5200007]}), call, summary)


def test_keyword_matching_ignores_case(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session, text="Structure FIRE at 400 block of Alder")
    summary = summarize(session, call.id, alert_settings)
    assert matches(rule(session, kind="keyword", pattern="structure fire"), call, summary)
    assert not matches(rule(session, kind="keyword", pattern="water rescue"), call, summary)


def test_regex_matching_and_a_broken_pattern(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session, text="Engine 1 en route, 10-4")
    summary = summarize(session, call.id, alert_settings)
    assert matches(rule(session, kind="regex", pattern=r"\b10-\d\b"), call, summary)
    assert not matches(rule(session, kind="regex", pattern="([unclosed"), call, summary)


def test_a_text_rule_never_matches_a_call_without_a_transcript(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session, text=None)
    summary = summarize(session, call.id, alert_settings)
    assert not matches(rule(session, kind="keyword", pattern="fire"), call, summary)


# --------------------------------------------------------------------------------------
# which event fires which kind
# --------------------------------------------------------------------------------------


@respx.mock
def test_metadata_rules_fire_on_call_new(session, alert_settings) -> None:  # noqa: ANN001
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]})

    written = evaluate(session, call.id, event="call.new", settings=alert_settings)

    assert len(written) == 1 and written[0].ok is True
    assert route.called
    payload = json.loads(route.calls[0].request.content)
    assert payload["call_id"] == call.id
    assert payload["talkgroup"]["tgid"] == 1101
    assert payload["url"] == f"http://192.0.2.60/calls/{call.id}"
    assert payload["audio_url"] == f"http://192.0.2.60/api/calls/{call.id}/audio"
    assert payload["rule"]["kind"] == "talkgroup"


@respx.mock
def test_text_rules_wait_for_call_transcribed(session, alert_settings) -> None:  # noqa: ANN001
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    call = seed_call(session, text="working structure fire")
    rule(session, kind="keyword", pattern="structure fire")

    assert evaluate(session, call.id, event="call.new", settings=alert_settings) == []
    assert len(evaluate(session, call.id, event="call.transcribed", settings=alert_settings)) == 1


def test_an_unknown_event_is_ignored(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]})
    assert evaluate(session, call.id, event="call.updated", settings=alert_settings) == []


# --------------------------------------------------------------------------------------
# cooldown
# --------------------------------------------------------------------------------------


@respx.mock
def test_the_cooldown_suppresses_the_second_call(session, alert_settings) -> None:  # noqa: ANN001
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    first = seed_call(session)
    second = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]}, cooldown_seconds=300)

    evaluate(session, first.id, event="call.new", settings=alert_settings)
    evaluate(session, second.id, event="call.new", settings=alert_settings)

    assert route.call_count == 1


@respx.mock
def test_the_cooldown_expires(session, alert_settings) -> None:  # noqa: ANN001
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    call = seed_call(session)
    row = rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]}, cooldown_seconds=60)

    now = datetime.now(UTC)
    evaluate(session, call.id, event="call.new", settings=alert_settings, now=now)
    evaluate(
        session, call.id, event="call.new", settings=alert_settings, now=now + timedelta(seconds=61)
    )

    assert route.call_count == 2
    session.refresh(row)
    assert row.last_fired_at is not None


@respx.mock
def test_a_zero_cooldown_never_suppresses(session, alert_settings) -> None:  # noqa: ANN001
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]}, cooldown_seconds=0)

    evaluate(session, call.id, event="call.new", settings=alert_settings)
    evaluate(session, call.id, event="call.new", settings=alert_settings)

    assert route.call_count == 2


# --------------------------------------------------------------------------------------
# channels
# --------------------------------------------------------------------------------------


@respx.mock
def test_ntfy_carries_the_title_click_and_body(session, alert_settings) -> None:  # noqa: ANN001
    route = respx.post(f"{NTFY}/dispatch").mock(return_value=httpx.Response(200))
    call = seed_call(session, text="engine one responding")
    rule(
        session,
        kind="keyword",
        pattern="engine one",
        channels=["ntfy"],
        channel_config={"ntfy": {"topic": "dispatch"}},
    )

    evaluate(session, call.id, event="call.transcribed", settings=alert_settings)

    request = route.calls[0].request
    assert request.headers["Title"] == "Keyup: EC FIRE DISPATCH"
    assert request.headers["Click"] == f"http://192.0.2.60/calls/{call.id}"
    assert b"engine one responding" in request.content


@respx.mock
def test_discord_sends_an_embed(session, alert_settings) -> None:  # noqa: ANN001
    route = respx.post(DISCORD).mock(return_value=httpx.Response(204))
    call = seed_call(session)
    rule(
        session,
        kind="talkgroup",
        filters={"talkgroup_ids": [1101]},
        channels=["discord"],
        channel_config={"discord": {"webhook_url": DISCORD}},
    )

    evaluate(session, call.id, event="call.new", settings=alert_settings)

    embed = json.loads(route.calls[0].request.content)["embeds"][0]
    assert embed["title"] == "Keyup: EC FIRE DISPATCH"
    assert embed["url"].endswith(f"/calls/{call.id}")


@respx.mock
def test_one_rule_can_use_several_channels(session, alert_settings) -> None:  # noqa: ANN001
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200))
    ntfy = respx.post(f"{NTFY}/dispatch").mock(return_value=httpx.Response(200))
    call = seed_call(session)
    rule(
        session,
        kind="talkgroup",
        filters={"talkgroup_ids": [1101]},
        channels=["webhook", "ntfy"],
        channel_config={"webhook": {"url": WEBHOOK}, "ntfy": {"topic": "dispatch"}},
    )

    written = evaluate(session, call.id, event="call.new", settings=alert_settings)

    assert hook.called and ntfy.called
    assert {n.channel for n in written} == {"webhook", "ntfy"}


# --------------------------------------------------------------------------------------
# failures are data, not exceptions
# --------------------------------------------------------------------------------------


@respx.mock
def test_a_failing_webhook_is_logged_on_the_notification(session, alert_settings) -> None:  # noqa: ANN001
    respx.post(WEBHOOK).mock(return_value=httpx.Response(500, text="boom"))
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]})

    written = evaluate(session, call.id, event="call.new", settings=alert_settings)

    assert len(written) == 1
    assert written[0].ok is False
    assert "HTTPStatusError" in written[0].error
    stored = session.execute(select(Notification)).scalars().all()
    assert len(stored) == 1 and stored[0].ok is False


@respx.mock
def test_an_unreachable_host_never_raises(session, alert_settings) -> None:  # noqa: ANN001
    respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("no route to host"))
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]})

    written = evaluate(session, call.id, event="call.new", settings=alert_settings)

    assert written[0].ok is False and "ConnectError" in written[0].error


def test_a_misconfigured_channel_is_recorded(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]}, channel_config={})

    written = evaluate(session, call.id, event="call.new", settings=alert_settings)

    assert written[0].ok is False
    assert "webhook channel has no url" in written[0].error


def test_a_disabled_rule_never_fires(session, alert_settings) -> None:  # noqa: ANN001
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]}, enabled=False)
    assert evaluate(session, call.id, event="call.new", settings=alert_settings) == []


def test_a_vanished_call_is_not_an_error(session, alert_settings) -> None:  # noqa: ANN001
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]})
    assert evaluate(session, 999999, event="call.new", settings=alert_settings) == []


# --------------------------------------------------------------------------------------
# secrets, and a regex that will not finish (H1, M2)
# --------------------------------------------------------------------------------------


@respx.mock
def test_a_delivery_error_never_quotes_the_endpoint(session, alert_settings) -> None:  # noqa: ANN001
    """The delivery log is readable by anybody; a webhook URL is a credential."""
    respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError(f"cannot connect to {WEBHOOK}"))
    call = seed_call(session)
    rule(session, kind="talkgroup", filters={"talkgroup_ids": [1101]})

    written = evaluate(session, call.id, event="call.new", settings=alert_settings)

    assert written[0].ok is False
    assert WEBHOOK not in written[0].error
    assert "<url>" in written[0].error


def test_a_runaway_regex_is_skipped_and_recorded(session, alert_settings) -> None:  # noqa: ANN001
    """M2: the rule loses this call, the worker does not lose the thread."""
    call = seed_call(session, text="a" * 40 + "!")
    rule(session, kind="regex", pattern=r"(a|a)+$", channels=["webhook"], filters={})

    written = evaluate(session, call.id, event="call.transcribed", settings=alert_settings)

    assert len(written) == 1
    assert written[0].ok is False
    assert written[0].severity == "skipped"
    assert "did not finish" in written[0].error
