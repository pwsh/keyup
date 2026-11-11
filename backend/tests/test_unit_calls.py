"""Unit-to-unit (private) calls — the push contract, reconciliation, and the export gate.

A P25 private call is one radio talking to one radio.  It is granted a traffic channel and
recorded like anything else, and it is on **no talkgroup at all** — which is the fact every
test here is about, because until now "a call with no talkgroup" meant conventional traffic
keyed by its frequency, and the two must never be confused (docs/RECORDERS.md,
"Unit-to-unit calls"; docs/EXPORTERS.md).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import call_upload as upload_module
from app.api.call_upload import router
from app.api.uploads import body_size_guard
from app.config import Settings
from app.db import get_session
from app.exporters.gate import UNIT_CALL, excluded_reason, unit_calls_ok
from app.exporters.ledger import skip_reason_for
from app.ingest.events import normalize_kind, record_events
from app.ingest.reconcile import Interval, ReconcileConfig, find_match, matches
from app.models import ApiToken, Call, Radio, RadioEvent, Talkgroup
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

KEY = "test-upload-key"
OK = "Call imported successfully."
BASE = datetime(2026, 8, 29, 20, 9, 45, tzinfo=UTC)
#: the pair the fixtures use throughout: a caller and the unit it called
CALLER, CALLEE = 5238001, 5238109


@pytest.fixture
def upload_settings(tmp_path):  # noqa: ANN001, ANN201
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        transcribe_min_seconds=0.75,
        conversation_gap_seconds=20,
    )


@pytest.fixture
def client(session, upload_settings, monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(upload_module, "get_settings", lambda: upload_settings)
    app = FastAPI()
    app.middleware("http")(body_size_guard)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token(session):  # noqa: ANN001, ANN201
    row = ApiToken(
        label="trunkd",
        token_hash=hashlib.sha256(KEY.encode()).hexdigest(),
        token_prefix=KEY[:8],
        scopes=["upload"],
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def mp3(fixture_audio) -> Path:  # noqa: ANN001
    path = fixture_audio / (
        "20260829/EXAMPLE TRANSIT/20260829_015213_Example-System_Central_T-Control"
        "__TO_1753_FROM_9250041.mp3"
    )
    if not path.is_file():  # pragma: no cover - the fixture tree ships with the repo
        pytest.skip(f"fixture recording missing: {path}")
    return path


def post(client, audio: Path, **fields):  # noqa: ANN001, ANN003
    """One trunkd push.  ``call_kind``/``target_radio_id`` are the new half of the form."""
    form = {
        "key": KEY,
        "system": "1",
        "dateTime": str(int(BASE.timestamp())),
        "source": str(CALLER),
        "frequency": "852162500",
        "systemLabel": "Example System",
        "recorder": "trunkd",
        "site": "Central",
        "call_kind": "unit",
        "target_radio_id": str(CALLEE),
    }
    form.update({k: v for k, v in fields.items() if v is not None})
    for key in [k for k, v in fields.items() if v is None]:
        form.pop(key, None)
    return client.post(
        "/api/call-upload",
        data=form,
        files={"audio": ("call.mp3", audio.read_bytes(), "audio/mpeg")},
    )


# --------------------------------------------------------------------------------------
# the push contract
# --------------------------------------------------------------------------------------


def test_a_unit_call_is_stored_with_its_kind_and_both_radios(client, session, token, mp3) -> None:  # noqa: ANN001
    assert post(client, mp3).text == OK

    call = session.scalars(select(Call)).one()
    assert call.kind == "unit"
    # ``call_type`` follows: the enum has always had ``individual`` for exactly this, and
    # nothing was able to set it until the recorder could say so.
    assert call.call_type == "individual"
    assert call.talkgroup_id is None
    assert call.talkgroup_ids == []

    radios = {
        row.radio_id: row for row in session.scalars(select(Radio)).all()
    }
    assert set(radios) == {CALLER, CALLEE}
    assert call.radio_id == radios[CALLER].id
    assert call.target_radio_id == radios[CALLEE].id
    # The far end did not key up, so the call is not counted against its transmissions.
    assert radios[CALLEE].call_count == 0


def test_a_unit_call_creates_no_placeholder_talkgroup(client, session, token, mp3) -> None:  # noqa: ANN001
    assert post(client, mp3).text == OK
    assert session.scalars(select(Talkgroup)).all() == []


def test_a_unit_call_that_carries_a_talkgroup_is_refused(client, session, token, mp3) -> None:  # noqa: ANN001
    response = post(client, mp3, talkgroup="1701")
    assert response.status_code == 400
    assert response.text == "incomplete call data: a unit call has no talkgroup"
    assert session.scalars(select(Call)).all() == []


def test_a_unit_call_with_no_target_is_refused(client, session, token, mp3) -> None:  # noqa: ANN001
    response = post(client, mp3, target_radio_id=None)
    assert response.status_code == 400
    assert response.text == "incomplete call data: no target radio"
    assert session.scalars(select(Call)).all() == []


def test_an_unknown_kind_is_refused(client, session, token, mp3) -> None:  # noqa: ANN001
    response = post(client, mp3, call_kind="telepathy")
    assert response.status_code == 400
    assert response.text == "incomplete call data: unknown call kind"
    assert session.scalars(select(Call)).all() == []


def test_a_group_push_is_unchanged_by_any_of_this(client, session, token, mp3) -> None:  # noqa: ANN001
    """The contract every recorder already speaks: no ``call_kind``, a talkgroup, done."""
    assert post(client, mp3, call_kind=None, target_radio_id=None, talkgroup="1701").text == OK
    call = session.scalars(select(Call)).one()
    assert call.kind == "group"
    assert call.target_radio_id is None
    assert call.talkgroup_ids == [1701]


def test_a_group_push_with_no_talkgroup_still_gets_the_probe_answer(client, token, mp3) -> None:  # noqa: ANN001
    """sdrtrunk string-matches this body, and a unit call must not have changed it."""
    response = post(client, mp3, call_kind=None, target_radio_id=None)
    assert response.status_code == 200
    assert response.text == "incomplete call data: no talkgroup"


def test_a_unit_call_is_queued_for_transcription_on_the_global_policy(client, session, token, mp3) -> None:  # noqa: ANN001
    """No talkgroup means no per-talkgroup policy, so the global default decides — and the
    default is to transcribe, which is what makes a private call searchable at all."""
    assert post(client, mp3).text == OK
    assert session.scalars(select(Call)).one().transcript_status == "queued"


# --------------------------------------------------------------------------------------
# reconciliation keys on the pair, not the talkgroup
# --------------------------------------------------------------------------------------


def _system(session) -> int:  # noqa: ANN001
    from app.ingest.service import upsert_system

    return upsert_system(session, "Example System", protocol="P25 Phase 1", wacn=None)


def _radio(session, system_id: int, radio_id: int) -> int:  # noqa: ANN001
    from app.ingest.service import upsert_radio

    return upsert_radio(
        session, system_id, radio_id=radio_id, wacn=0, home_system=0, seen_at=BASE
    )


def _call(  # noqa: ANN001, PLR0913
    session,
    *,
    system_id: int,
    kind: str = "group",
    tgids: tuple[int, ...] = (1701,),
    radio_pk: int | None = None,
    target_pk: int | None = None,
    started_at: datetime = BASE,
    duration: float = 8.0,
    path: str | None = None,
) -> Call:
    from app.ingest.service import upsert_talkgroup

    primary = None
    for tgid in tgids:
        primary = primary or upsert_talkgroup(
            session, system_id, tgid, alias=None, seen_at=started_at
        )
    call = Call(
        system_id=system_id,
        kind=kind,
        talkgroup_id=primary.id if primary is not None else None,
        talkgroup_ids=list(tgids),
        radio_id=radio_pk,
        target_radio_id=target_pk,
        ingest_source="file",
        recorded_at=started_at,
        started_at=started_at,
        started_local=started_at.replace(tzinfo=None),
        ended_at=started_at + timedelta(seconds=duration),
        time_source="file_write",
        duration_s=Decimal(str(round(duration, 3))),
        frequency_hz=852137500,
        audio_state="present",
        transcript_status="pending",
        file_path=path or f"synthetic/{kind}-{started_at.timestamp()}-{duration}.mp3",
        file_name="synthetic.mp3",
    )
    session.add(call)
    session.flush()
    return call


def _interval(kind: str, pair: tuple[int | None, int | None], *, tgids=(1701,)) -> Interval:  # noqa: ANN001
    return Interval(
        started_at=BASE,
        ended_at=BASE + timedelta(seconds=8),
        duration_s=8.0,
        talkgroup_ids=tuple(tgids) if kind == "group" else (),
        frequency_hz=852137500,
        kind=kind,
        unit_pair=pair,
    )


def test_two_copies_of_one_private_call_match_on_the_pair() -> None:
    cfg = ReconcileConfig.of(Settings(_env_file=None))
    result = matches(_interval("unit", (1, 2)), _interval("unit", (1, 2)), cfg)
    assert result.matched
    assert result.key == "unit:1>2"


def test_two_private_calls_between_different_radios_never_match() -> None:
    """Same second, same traffic frequency — and a different conversation entirely.  The
    frequency rung that saves a talkgroup-less *conventional* call must not fire here."""
    cfg = ReconcileConfig.of(Settings(_env_file=None))
    assert not matches(_interval("unit", (1, 2)), _interval("unit", (3, 4)), cfg).matched


def test_a_private_call_is_directional() -> None:
    cfg = ReconcileConfig.of(Settings(_env_file=None))
    assert not matches(_interval("unit", (1, 2)), _interval("unit", (2, 1)), cfg).matched


def test_a_private_call_with_an_unresolved_end_matches_nothing() -> None:
    cfg = ReconcileConfig.of(Settings(_env_file=None))
    assert not matches(_interval("unit", (1, None)), _interval("unit", (1, None)), cfg).matched


def test_a_unit_call_and_a_group_call_never_reconcile_together() -> None:
    """They overlap perfectly and share a frequency; they are still two transmissions."""
    cfg = ReconcileConfig.of(Settings(_env_file=None))
    result = matches(_interval("unit", (1, 2)), _interval("group", (None, None)), cfg)
    assert not result.matched
    assert result.rule == "different call kind"


def test_find_match_pairs_two_recorders_copies_of_one_private_call(session) -> None:  # noqa: ANN001
    system_id = _system(session)
    caller, callee = _radio(session, system_id, CALLER), _radio(session, system_id, CALLEE)
    first = _call(
        session, system_id=system_id, kind="unit", tgids=(), radio_pk=caller, target_pk=callee
    )
    second = _call(
        session,
        system_id=system_id,
        kind="unit",
        tgids=(),
        radio_pk=caller,
        target_pk=callee,
        started_at=BASE + timedelta(seconds=0.4),
        path="synthetic/second.mp3",
    )
    other, result = find_match(session, second, ReconcileConfig.of(Settings(_env_file=None)))
    assert other is not None and other.id == first.id
    assert result.key == f"unit:{caller}>{callee}"


def test_a_group_call_on_the_same_frequency_is_not_a_copy_of_a_private_one(session) -> None:  # noqa: ANN001
    system_id = _system(session)
    caller, callee = _radio(session, system_id, CALLER), _radio(session, system_id, CALLEE)
    _call(session, system_id=system_id, kind="group", radio_pk=caller)
    private = _call(
        session,
        system_id=system_id,
        kind="unit",
        tgids=(),
        radio_pk=caller,
        target_pk=callee,
        path="synthetic/private.mp3",
    )
    other, _ = find_match(session, private, ReconcileConfig.of(Settings(_env_file=None)))
    assert other is None


def _push_pair(client, session, mp3, fixture_audio):  # noqa: ANN001, ANN202
    """Both recorders push one private call, seconds apart, with different bytes and
    different ``sourceQuality.instance`` — the production shape."""
    second_audio = fixture_audio / (
        "20260829/EXAMPLE MED 2/20260829_135155_Example-System_Central"
        "_T-Control__TO_1634_FROM_9200114_V2.mp3"
    )
    if not second_audio.is_file():  # pragma: no cover - the fixture tree ships with the repo
        pytest.skip(f"fixture recording missing: {second_audio}")
    # Different bytes, so the exact-sha256 rung cannot answer this for us.
    assert second_audio.read_bytes() != mp3.read_bytes()

    first = post(
        client,
        mp3,
        startMs=str(int(BASE.timestamp() * 1000)),
        sourceQuality='{"instance": "recorder-a", "voice_frames": 400, "bad_frames": 2}',
    )
    second = post(
        client,
        second_audio,
        startMs=str(int(BASE.timestamp() * 1000) + 500),
        sourceQuality='{"instance": "laptop-01", "voice_frames": 400, "bad_frames": 9}',
    )
    calls = session.scalars(select(Call).order_by(Call.id)).all()
    return first, second, calls


def test_two_recorders_push_one_private_call_and_it_reconciles_at_ingest(  # noqa: ANN001
    client, session, token, mp3, fixture_audio
) -> None:
    """The simple case: neither radio has ever been seen, so each gets exactly one row."""
    first, second, calls = _push_pair(client, session, mp3, fixture_audio)
    assert first.text == OK
    # sdrtrunk string-matches this body; the copy is stored all the same.
    assert second.text == "duplicate call rejected"
    assert len(calls) == 2
    primaries = [c for c in calls if c.duplicate_of is None]
    assert len(primaries) == 1
    assert [c.duplicate_of for c in calls if c.duplicate_of is not None] == [primaries[0].id]


def test_a_private_call_reconciles_inline_even_when_the_radio_has_several_rows(  # noqa: ANN001
    client, session, token, mp3, fixture_audio
) -> None:
    """The production miss.  P25 LC opcode 9 rides some transmissions and not others, so one
    unit is filed bare *and* under every home-system spelling it was ever heard with —
    one radio can have five ``radios`` rows.  Reconciliation resolves a
    record's radios before the call is written, and while that lookup asked only for "a row
    with this ``radio_id``" it got whichever row came back last: the pair it built named
    rows no stored call carries, the candidate query returned nothing, and both recorders'
    copies sat on screen as separate calls until the half-hourly batch pass.
    """
    system_id = _system(session)
    for wacn, home in ((101010, 57), (0, 52), (101010, 52), (101010, 82)):
        session.add(
            Radio(system_id=system_id, radio_id=CALLER, wacn=wacn, home_system_id=home)
        )
    session.add(Radio(system_id=system_id, radio_id=CALLEE, wacn=101010, home_system_id=57))
    session.flush()

    first, second, calls = _push_pair(client, session, mp3, fixture_audio)
    assert first.text == OK
    assert second.text == "duplicate call rejected"
    assert len(calls) == 2
    primaries = [c for c in calls if c.duplicate_of is None]
    assert len(primaries) == 1, "the two copies of one private call are still two calls"
    loser = next(c for c in calls if c.duplicate_of is not None)
    assert loser.duplicate_of == primaries[0].id
    # Both copies name the same pair of rows, and it is the bare one the write path mints.
    assert primaries[0].radio_id == loser.radio_id
    assert primaries[0].target_radio_id == loser.target_radio_id


def test_the_pre_write_radio_lookup_agrees_with_the_write_path(session) -> None:  # noqa: ANN001
    """``radio_pks_for_record`` and ``resolve_radio`` must never name different rows: the
    first decides what a pushed call is compared against, the second what it is stored as."""
    from app.ingest.reconcile import radio_pks_for_record
    from app.ingest.service import resolve_radio

    system_id = _system(session)
    for wacn, home in ((101010, 57), (0, 52), (101010, 52)):
        session.add(
            Radio(system_id=system_id, radio_id=CALLER, wacn=wacn, home_system_id=home)
        )
    session.flush()
    bare = resolve_radio(
        session, system_id, radio_id=CALLER, wacn=0, home_system=0, seen_at=BASE
    )
    callee = resolve_radio(
        session, system_id, radio_id=CALLEE, wacn=0, home_system=0, seen_at=BASE
    )
    record = SimpleNamespace(
        radio=SimpleNamespace(radio_id=CALLER, wacn=None, home_system=None),
        target_radio_id=CALLEE,
        kind="unit",
    )
    assert radio_pks_for_record(session, record, system_id) == (bare, callee)


# --------------------------------------------------------------------------------------
# the export gate
# --------------------------------------------------------------------------------------


def _unit(kind: str = "unit", **over):  # noqa: ANN003, ANN202
    return SimpleNamespace(
        kind=kind,
        talkgroup_id=None,
        frequency_hz=852137500,
        encrypted=False,
        duration_s=8.0,
        audio_state="present",
        file_path="uploads/x.mp3",
        system_id=1,
        **over,
    )


@pytest.mark.parametrize("service", ["broadcastify", "openmhz"])
def test_the_archives_skip_a_unit_call_with_its_own_reason(service: str) -> None:
    settings = Settings(_env_file=None)
    assert excluded_reason(None, _unit(), settings, service) == UNIT_CALL
    assert UNIT_CALL == "unit_call"


@pytest.mark.parametrize("service", ["broadcastify", "openmhz"])
def test_the_setting_cannot_let_a_unit_call_onto_the_archives(service: str) -> None:
    """The Feed's switch is the Feed's; for Calls and OpenMHz this is a rule, not a choice."""
    settings = Settings(_env_file=None, broadcastify_feed_include_unit_calls=True)
    assert not unit_calls_ok(_unit(), settings, service)


def test_the_feed_takes_unit_calls_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.broadcastify_feed_include_unit_calls is True
    assert excluded_reason(None, _unit(), settings, "broadcastify_feed") is None


def test_the_feed_can_be_told_not_to() -> None:
    settings = Settings(_env_file=None, broadcastify_feed_include_unit_calls=False)
    assert excluded_reason(None, _unit(), settings, "broadcastify_feed") == UNIT_CALL


def test_a_unit_call_is_not_refused_for_having_no_talkgroup() -> None:
    """``skip_reason_for`` is about the recording.  "No talkgroup" is a fact about a
    *group* call that lost one; a private call never had one, and that is not a fault."""
    assert skip_reason_for(_unit(), Settings(_env_file=None)) is None


def test_a_group_call_with_no_talkgroup_is_still_refused() -> None:
    assert skip_reason_for(_unit(kind="group"), Settings(_env_file=None)) == "no talkgroup"


# --------------------------------------------------------------------------------------
# the control-channel event
# --------------------------------------------------------------------------------------


def test_private_call_is_a_radio_event_kind() -> None:
    assert normalize_kind("private_call") == "private_call"
    # A recorder that spells it differently still lands on the modeled kind rather than
    # falling through to ``other``.
    assert normalize_kind("unit-to-unit") == "private_call"


def test_a_private_call_event_keeps_both_radios(session) -> None:  # noqa: ANN001
    result = record_events(
        session,
        {
            "recorder": "trunkd",
            "system": {"label": "Example System", "wacn": 101010, "system_id": 52, "nac": 52},
            "site": {"label": "Central", "rfss": 10, "site": 1, "lra": 10},
            "events": [
                {
                    "at": BASE.isoformat(),
                    "kind": "private_call",
                    "opcode": 4,
                    "radio_id": CALLER,
                    "target_radio_id": CALLEE,
                    "frequency_hz": 852162500,
                    "result": "accept",
                }
            ],
        },
        settings=Settings(_env_file=None),
    )
    assert result.stored == 1
    event = session.scalars(select(RadioEvent)).one()
    assert event.kind == "private_call"
    assert event.radio_id == CALLER
    assert event.target_radio_id == CALLEE
    assert event.talkgroup_tgid is None
