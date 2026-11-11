"""Best-source negotiation on the push path (docs/RECORDERS.md, "Best source").

Two receivers hear one transmission and both push it.  The fuzzy dedupe recognizes the
second push as the same call and — before this — threw it away, whichever of the two had
actually heard it.  With both recorders reporting how well they heard it (``sourceQuality``:
the instance, the voice frames it decoded, the frames it could not, the level), the archive
can keep the better recording instead of the first one to arrive.

Everything here is about the *refusals*, because that is where the risk is.  A feature that
swaps a call's audio has to be provably inert whenever it is not certain: no report, a
report from the same recorder re-sending, a margin inside the noise, junk in the field — all
of them must land on exactly the answer the recorder saw before this existed.

The path under test is the one where the old dedupe would **discard** the second copy, i.e.
``reconcile_enabled=false``.  With reconciliation on, the second copy is stored as its own
row and its own scoring decides which is primary (docs/RECONCILIATION.md) — nothing is
thrown away there, so there is nothing for this to rescue.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import call_upload as upload_module
from app.api.call_upload import DUPLICATE_BODY, OK_BODY, router
from app.api.uploads import body_size_guard
from app.config import Settings
from app.db import get_session
from app.ingest import flow
from app.models import ActivityEvent, ApiToken, Call, TranscriptionJob
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

TAGGED = "20260829/EXAMPLE TRANSIT/20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3"
KEY = "test-best-source-key"
START = datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_gauge():  # noqa: ANN201
    """The admission gauge is process-global; no test may inherit another's reading."""
    flow.invalidate()
    flow.reset_semaphore()
    flow.set_router(None)
    yield
    flow.invalidate()
    flow.reset_semaphore()
    flow.set_router(None)


@pytest.fixture
def upload_settings(tmp_path):  # noqa: ANN001, ANN201
    """The old dedupe, which is the path that discards a second copy — see the module note."""
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        transcribe_min_seconds=0.75,
        conversation_gap_seconds=20,
        reconcile_enabled=False,
        best_source_enabled=True,
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
        label="recorders",
        token_hash=hashlib.sha256(KEY.encode()).hexdigest(),
        token_prefix=KEY[:8],
        scopes=["upload"],
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def tagged_mp3(fixture_audio) -> Path:  # noqa: ANN001
    path = fixture_audio / TAGGED
    if not path.is_file():  # pragma: no cover - the fixture tree ships with the repo
        pytest.skip(f"fixture recording missing: {path}")
    return path


def quality(instance: str, voice: int | None = None, bad: int | None = None) -> str:
    payload: dict[str, object] = {"instance": instance, "rssi_dbfs": -38.4}
    if voice is not None:
        payload["voice_frames"] = voice
    if bad is not None:
        payload["bad_frames"] = bad
    return json.dumps(payload)


def post(client, mp3: Path, *, source_quality: str | None = None, name: str = "call.mp3"):  # noqa: ANN001, ANN201
    """One call-upload, identical every time but for the quality report and the file name."""
    form = {
        "key": KEY,
        "system": "1",
        "dateTime": str(int(START.timestamp())),
        "talkgroup": "1701",
        "source": "9200006",
        "systemLabel": "Example System",
        "talkgroupLabel": "EXAMPLE EVENT 1",
        "recorder": "trunkd",
        "priority": "live",
    }
    if source_quality is not None:
        form["sourceQuality"] = source_quality
    return client.post(
        "/api/call-upload",
        data=form,
        files={"audio": (name, mp3.read_bytes(), "audio/mpeg")},
    )


def the_call(session) -> Call:  # noqa: ANN001
    session.expire_all()
    return session.scalars(select(Call)).one()


def kinds(session) -> list[str]:  # noqa: ANN001
    return list(session.scalars(select(ActivityEvent.kind).order_by(ActivityEvent.id)))


# --------------------------------------------------------------------------------------
# the field itself
# --------------------------------------------------------------------------------------


def test_the_quality_report_is_stored_on_the_call(client, token, tagged_mp3, session) -> None:  # noqa: ANN001, ARG001
    """Declared on the endpoint at last: FastAPI dropped this field until it was named."""
    assert post(client, tagged_mp3, source_quality=quality("rec-01", 1000, 10)).text == OK_BODY

    call = the_call(session)
    assert call.source_instance == "rec-01"
    assert call.source_quality == {
        "instance": "rec-01",
        "rssi_dbfs": -38.4,
        "voice_frames": 1000,
        "bad_frames": 10,
    }


@pytest.mark.parametrize("junk", ["{{{", "not json at all", "[1,2,3]", "17", ""])
def test_junk_quality_is_ignored_rather_than_refused(client, token, tagged_mp3, session, junk) -> None:  # noqa: ANN001, ARG001, PLR0913
    """A garbled optional field must never cost the recorder its call."""
    assert post(client, tagged_mp3, source_quality=junk).text == OK_BODY

    call = the_call(session)
    assert call.source_instance is None
    assert call.source_quality is None


#: ``json.dumps`` refuses to write these, so the literals go on the wire by hand — which is
#: exactly how a recorder whose arithmetic divided by zero sends them.  Python's own
#: ``json.loads`` accepts all three and hands back a float ``jsonb`` cannot store.
NON_FINITE = [
    '{"instance": "rec-01", "voice_frames": NaN}',
    '{"instance": "rec-01", "voice_frames": 1000, "bad_frames": -Infinity}',
    '{"instance": "rec-01", "voice_frames": 1000, "levels": {"rssi_dbfs": Infinity}}',
    '{"instance": "rec-01", "voice_frames": 1000, "history": [1, 2, NaN]}',
]


@pytest.mark.parametrize("report", NON_FINITE)
def test_a_non_finite_number_costs_the_report_and_not_the_call(client, token, tagged_mp3, session, report) -> None:  # noqa: ANN001, ARG001, PLR0913
    """NaN and the infinities have no ``jsonb`` spelling: storing one is a 500, and a 500 is

    a recorder that spools the call and retries it for ever.  The report is dropped whole —
    however deep in the object the bad number sits — and the call is ingested as one that
    carried no report at all.
    """
    assert post(client, tagged_mp3, source_quality=report).text == OK_BODY

    call = the_call(session)
    assert call.source_instance is None
    assert call.source_quality is None


def test_a_nan_score_never_wins_a_replacement(client, token, tagged_mp3, session, upload_settings) -> None:  # noqa: ANN001, ARG001, PLR0913
    """The margin test is a comparison, and every comparison against NaN is False.

    ``incoming - stored <= abs(stored) * 0.05`` would therefore read "not worse enough to
    refuse" as "the incoming copy won" and swap a good recording for whatever this is.
    """
    post(client, tagged_mp3, source_quality=quality("rec-01", 1000, 0), name="first.mp3")
    kept = the_call(session).file_path

    nan_report = '{"instance": "rec-02", "voice_frames": NaN, "bad_frames": 0}'
    assert post(client, tagged_mp3, source_quality=nan_report).text == DUPLICATE_BODY

    call = the_call(session)
    assert call.file_path == kept
    assert call.source_instance == "rec-01"
    assert call.source_quality["voice_frames"] == 1000
    assert (Path(upload_settings.audio_root) / kept).is_file()
    assert "call.better_source" not in kinds(session)


def test_an_upload_without_the_field_behaves_exactly_as_before(client, token, tagged_mp3, session) -> None:  # noqa: ANN001, ARG001
    """The regression that matters: sdrtrunk sends no such field and must see no change."""
    assert post(client, tagged_mp3).text == OK_BODY
    assert post(client, tagged_mp3).text == DUPLICATE_BODY

    call = the_call(session)
    assert call.source_instance is None and call.source_quality is None
    assert kinds(session) == ["call.accepted", "call.duplicate"]


# --------------------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------------------


def test_a_better_copy_takes_the_row_and_the_old_audio_goes(  # noqa: PLR0913
    client, token, tagged_mp3, session, upload_settings  # noqa: ANN001, ARG001
) -> None:
    post(client, tagged_mp3, source_quality=quality("rec-01", 1000, 10), name="first.mp3")
    first = the_call(session)
    old_path = Path(upload_settings.audio_root) / first.file_path
    assert old_path.is_file()
    # Pretend the first copy has been transcribed, so the re-enqueue below is visible as a
    # *new* job rather than as the one the first upload already left queued.
    session.query(TranscriptionJob).delete()
    first.transcript_status = "done"
    session.commit()

    # 2000 against 1000 − 5×10 = 950: a different receiver, and a real margin.
    response = post(client, tagged_mp3, source_quality=quality("rec-02", 2000, 0), name="second.mp3")
    assert response.status_code == 200
    assert response.text == OK_BODY  # stored, not "duplicate call rejected"

    call = the_call(session)
    assert session.scalars(select(Call)).all() == [call]  # one row, not two
    assert call.source_instance == "rec-02"
    assert call.source_quality["voice_frames"] == 2000
    assert call.file_path != str(old_path.relative_to(upload_settings.audio_root))
    new_path = Path(upload_settings.audio_root) / call.file_path
    assert new_path.is_file() and new_path.read_bytes() == tagged_mp3.read_bytes()
    assert not old_path.exists()  # the bytes the row no longer points at are gone

    # Re-transcribed, because the audio the old transcript was made from has left.
    assert call.transcript_status == "queued"
    assert session.scalars(select(TranscriptionJob.call_id)).all() == [call.id]

    event = session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "call.better_source")
    ).one()
    assert event.call_id == call.id
    assert event.meta["from_instance"] == "rec-01"
    assert event.meta["to_instance"] == "rec-02"
    assert (event.meta["from_score"], event.meta["to_score"]) == (950.0, 2000.0)


def test_a_worse_copy_is_still_rejected(client, token, tagged_mp3, session, upload_settings) -> None:  # noqa: ANN001, ARG001, PLR0913
    post(client, tagged_mp3, source_quality=quality("rec-01", 2000, 0), name="first.mp3")
    kept = the_call(session).file_path

    assert post(client, tagged_mp3, source_quality=quality("rec-02", 100, 0)).text == DUPLICATE_BODY

    call = the_call(session)
    assert call.file_path == kept
    assert call.source_instance == "rec-01"
    assert (Path(upload_settings.audio_root) / kept).is_file()
    # The refused copy left nothing behind — one file in the archive, not two.
    assert len(list(Path(upload_settings.audio_root).rglob("*.mp3"))) == 1
    assert "call.better_source" not in kinds(session)


def test_a_margin_below_five_percent_does_not_replace(client, token, tagged_mp3, session) -> None:  # noqa: ANN001, ARG001
    """1040 against 1000 is two receivers hearing the same call, not a better one."""
    post(client, tagged_mp3, source_quality=quality("rec-01", 1000, 0), name="first.mp3")

    assert post(client, tagged_mp3, source_quality=quality("rec-02", 1040, 0)).text == DUPLICATE_BODY
    assert the_call(session).source_instance == "rec-01"

    # …and 1060 is.  Same pair of numbers, the other side of the line.
    assert post(client, tagged_mp3, source_quality=quality("rec-02", 1060, 0)).text == OK_BODY
    assert the_call(session).source_instance == "rec-02"


def test_the_same_recorder_never_replaces_its_own_copy(client, token, tagged_mp3, session) -> None:  # noqa: ANN001, ARG001
    """A retransmit is one receiver saying it again, not a second opinion."""
    post(client, tagged_mp3, source_quality=quality("rec-01", 1000, 0), name="first.mp3")
    kept = the_call(session).file_path

    assert post(client, tagged_mp3, source_quality=quality("rec-01", 9999, 0)).text == DUPLICATE_BODY
    call = the_call(session)
    assert call.file_path == kept
    assert call.source_quality["voice_frames"] == 1000


def test_quality_on_only_one_side_keeps_todays_behavior(client, token, tagged_mp3, session) -> None:  # noqa: ANN001, ARG001
    post(client, tagged_mp3, name="first.mp3")  # the stored copy says nothing
    kept = the_call(session).file_path

    assert post(client, tagged_mp3, source_quality=quality("rec-02", 9999, 0)).text == DUPLICATE_BODY
    assert the_call(session).file_path == kept


def test_a_report_without_voice_frames_is_not_scoreable(client, token, tagged_mp3, session) -> None:  # noqa: ANN001, ARG001
    """A report that does not say how much voice it decoded says nothing comparable."""
    post(client, tagged_mp3, source_quality=quality("rec-01", 1000, 0), name="first.mp3")

    assert post(client, tagged_mp3, source_quality=quality("rec-02")).text == DUPLICATE_BODY
    assert the_call(session).source_instance == "rec-01"


def test_the_switch_turns_the_whole_comparison_off(  # noqa: PLR0913
    client, token, tagged_mp3, session, upload_settings, monkeypatch  # noqa: ANN001, ARG001
) -> None:
    post(client, tagged_mp3, source_quality=quality("rec-01", 1000, 0), name="first.mp3")
    kept = the_call(session).file_path

    off = upload_settings.model_copy(update={"best_source_enabled": False})
    monkeypatch.setattr(upload_module, "get_settings", lambda: off)

    assert post(client, tagged_mp3, source_quality=quality("rec-02", 9999, 0)).text == DUPLICATE_BODY
    assert the_call(session).file_path == kept


# --------------------------------------------------------------------------------------
# the scoring rule on its own
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ({"voice_frames": 100, "bad_frames": 4}, 80.0),
        ({"voice_frames": 100}, 100.0),  # a missing bad-frame count is zero
        ({"voice_frames": 0, "bad_frames": 3}, -15.0),
        ({"bad_frames": 3}, None),  # nothing to compare
        ({"voice_frames": True}, None),  # a bool is not a frame count
        ({}, None),
        (None, None),
        # Non-finite is unscoreable, not "infinitely good": a score that is not a real
        # number makes every comparison downstream of it False.
        ({"voice_frames": float("nan")}, None),
        ({"voice_frames": float("inf")}, None),
        ({"voice_frames": float("-inf")}, None),
        # …and a non-finite penalty is read as absent, exactly as a non-numeric one is.
        ({"voice_frames": 100, "bad_frames": float("nan")}, 100.0),
    ],
)
def test_quality_score(report, expected) -> None:  # noqa: ANN001
    assert upload_module.quality_score(report) == expected
