"""Priority classes and admission control on the push path (``app/ingest/flow.py``).

Two contracts meet here and both are load-bearing:

* the **legacy** one, which sdrtrunk and trunk-recorder string-match — a recorder that
  sends no ``priority`` must see byte-identical bodies to the ones it saw before flow
  control existed, and must never be refused for a *backlog* reason;
* the **opt-in** one, which ``trunkd`` and ``tools/migrate_sdrtrunk.py`` use — send a
  ``priority`` and refusals arrive as JSON with a ``Retry-After`` worth obeying.

The gauge is monkeypatched rather than filled with real jobs: the query it runs is one
``GROUP BY`` and testing the *policy* against a fabricated reading is both faster and far
more precise about the boundaries than seeding five thousand rows would be.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import call_upload as upload_module
from app.api.call_upload import router
from app.api.uploads import body_size_guard
from app.config import Settings
from app.db import get_session
from app.ingest import flow
from app.models import ApiToken, Call, Talkgroup, TranscriptionJob
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db

TAGGED = "20260829/EXAMPLE TRANSIT/20260829_015213_Example-System_Central_T-Control__TO_1753_FROM_9250041.mp3"
KEY = "test-flow-key"


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_gauge():  # noqa: ANN201
    """The gauge and the in-flight semaphore are process-global; no test may inherit one."""
    flow.invalidate()
    flow.reset_semaphore()
    flow.set_router(None)
    yield
    flow.invalidate()
    flow.reset_semaphore()
    flow.set_router(None)


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
        label="migration",
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


def gauge_reading(monkeypatch, *, live: int = 0, backlog: int = 0, audio: float = 0.0) -> None:
    """Pin what the queue looks like, without seeding thousands of jobs to get there."""
    counts = flow.QueueCounts(live=live, backlog=backlog, audio_seconds=audio)
    monkeypatch.setattr(flow, "counts", lambda session, force=False: counts)  # noqa: ARG005


def post(client, mp3: Path, **fields):  # noqa: ANN001, ANN003
    form = {
        "key": KEY,
        "system": "1",
        "dateTime": str(int(datetime(2026, 8, 29, 19, 17, 3, tzinfo=UTC).timestamp())),
        "talkgroup": "1701",
        "source": "9200006",
        "systemLabel": "Example System",
        "talkgroupLabel": "EXAMPLE EVENT 1",
    }
    headers = fields.pop("headers", None)
    form.update({k: v for k, v in fields.items() if v is not None})
    for key in [k for k, v in fields.items() if v is None]:
        form.pop(key, None)
    return client.post(
        "/api/call-upload",
        data=form,
        files={"audio": (fields.get("audioName") or "call.mp3", mp3.read_bytes(), "audio/mpeg")},
        headers=headers,
    )


def job_priority(session) -> int:  # noqa: ANN001
    return session.scalars(select(TranscriptionJob.priority)).one()


# --------------------------------------------------------------------------------------
# parse_priority — the class vocabulary
# --------------------------------------------------------------------------------------


def test_no_priority_is_live_and_not_explicit() -> None:
    choice = flow.parse_priority(None, None)
    assert (choice.name, choice.explicit, choice.is_backlog) == ("live", False, False)
    assert choice.resolve(140) == 140  # the talkgroup's own priority: today's behavior


@pytest.mark.parametrize(
    ("name", "talkgroup_priority", "expected", "backlog_band"),
    [
        ("urgent", 140, 10, False),
        ("live", 140, 140, False),
        ("backlog", 140, 640, True),
        ("bulk", 140, 900, True),
        ("backlog", 100, 600, True),
    ],
)
def test_each_class_maps_to_a_priority(name, talkgroup_priority, expected, backlog_band) -> None:  # noqa: ANN001
    choice = flow.parse_priority(name)
    assert choice.resolve(talkgroup_priority) == expected
    assert choice.is_backlog is backlog_band


def test_an_integer_priority_is_used_as_is() -> None:
    choice = flow.parse_priority("42")
    assert choice.resolve(140) == 42
    assert choice.is_backlog is False
    assert flow.parse_priority("777").is_backlog is True  # its own value puts it in the band


def test_a_resolved_priority_is_clamped_to_the_column_range() -> None:
    # 999 + 500 would be 1499, which is not a priority; the band still holds.
    assert flow.parse_priority("backlog").resolve(999) == 999


@pytest.mark.parametrize("bad", ["", "  "])
def test_a_blank_priority_is_the_default(bad) -> None:  # noqa: ANN001
    assert flow.parse_priority(bad).explicit is False


@pytest.mark.parametrize("bad", ["soon", "0", "1000", "-3", "backlogg"])
def test_an_unusable_priority_is_rejected(bad) -> None:  # noqa: ANN001
    with pytest.raises(flow.BadPriority):
        flow.parse_priority(bad)


def test_the_form_wins_over_the_header() -> None:
    assert flow.parse_priority("bulk", "urgent").name == "bulk"
    assert flow.parse_priority(None, "urgent").name == "urgent"
    assert flow.parse_priority("", "urgent").name == "urgent"


# --------------------------------------------------------------------------------------
# the class on the wire
# --------------------------------------------------------------------------------------


def test_a_backlog_upload_queues_behind_live_and_records_its_class(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    assert post(client, tagged_mp3, priority="backlog").status_code == 200

    call = session.scalars(select(Call)).one()
    assert call.ingest_priority == "backlog"
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    assert job_priority(session) == talkgroup.priority + 500


def test_an_urgent_upload_queues_ahead_of_everything(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    assert post(client, tagged_mp3, priority="urgent").status_code == 200
    assert job_priority(session) == 10
    assert session.scalars(select(Call)).one().ingest_priority == "urgent"


def test_a_bulk_upload_queues_at_the_back(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    assert post(client, tagged_mp3, priority="bulk").status_code == 200
    assert job_priority(session) == 900


def test_an_integer_priority_on_the_wire_is_used_as_is(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    assert post(client, tagged_mp3, priority="42").status_code == 200
    assert job_priority(session) == 42
    assert session.scalars(select(Call)).one().ingest_priority == "42"


def test_the_priority_header_is_honoured_and_the_form_beats_it(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    assert post(client, tagged_mp3, headers={"X-Keyup-Priority": "bulk"}).status_code == 200
    assert job_priority(session) == 900


def test_the_form_priority_beats_the_header(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    response = post(client, tagged_mp3, priority="urgent", headers={"X-Keyup-Priority": "bulk"})
    assert response.status_code == 200
    assert job_priority(session) == 10


def test_an_upload_without_a_priority_keeps_todays_behavior(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    assert post(client, tagged_mp3).status_code == 200
    call = session.scalars(select(Call)).one()
    # NULL, not 'live': the column answers "what did the migration bring in", and a
    # recorder that never heard of priorities did not bring anything in under a class.
    assert call.ingest_priority is None
    assert job_priority(session) == session.get(Talkgroup, call.talkgroup_id).priority


def test_an_unusable_priority_is_a_400_with_the_json_shape(client, token, tagged_mp3) -> None:  # noqa: ANN001
    response = post(client, tagged_mp3, priority="whenever")
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_priority"
    assert "queue" in body


# --------------------------------------------------------------------------------------
# headers
# --------------------------------------------------------------------------------------


def test_every_accepted_upload_carries_the_queue_headers(client, token, tagged_mp3, monkeypatch) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=7, backlog=3, audio=120.0)
    response = post(client, tagged_mp3, priority="backlog")

    assert response.status_code == 200
    assert response.headers["X-Keyup-Queue-Live"] == "7"
    assert response.headers["X-Keyup-Queue-Backlog"] == "3"
    assert response.headers["X-Keyup-Queue-Audio-Seconds"] == "120"
    assert response.headers["X-Keyup-Accepting-Backlog"] == "yes"
    assert int(response.headers["X-Keyup-Queue-Eta-Seconds"]) >= 0


def test_the_probe_carries_the_headers_and_is_never_refused(client, token, monkeypatch) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=99999, backlog=99999, audio=1e6)
    response = client.post("/api/call-upload", data={"key": KEY, "system": "1", "test": "1"})

    assert response.status_code == 200
    assert response.text == "incomplete call data: no talkgroup"
    assert response.headers["X-Keyup-Accepting-Backlog"] == "no"


def test_an_invalid_key_learns_nothing_about_the_queue(client, token, tagged_mp3) -> None:  # noqa: ANN001
    response = post(client, tagged_mp3, key="nope")
    assert response.status_code == 401
    assert "X-Keyup-Queue-Live" not in response.headers


# --------------------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------------------


def test_backlog_is_refused_when_the_backlog_band_is_full(client, token, tagged_mp3, monkeypatch, upload_settings) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=2, backlog=upload_settings.upload_max_queue_backlog, audio=600.0)

    response = post(client, tagged_mp3, priority="backlog")

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "queue_full"
    assert body["class"] == "backlog"
    assert body["queue"]["backlog"] == upload_settings.upload_max_queue_backlog
    assert body["retry_after"] == int(response.headers["Retry-After"])
    assert response.headers["X-Keyup-Accepting-Backlog"] == "no"


def test_backlog_is_refused_when_the_queue_takes_too_long_to_drain(client, token, tagged_mp3, monkeypatch) -> None:  # noqa: ANN001
    # One backend at the fallback RTF of 1.0 and concurrency 2 drains 2 s of audio per
    # second; 4000 s of audio is a 2000 s wait, well over the 900 s ceiling.
    gauge_reading(monkeypatch, live=50, backlog=5, audio=4000.0)

    response = post(client, tagged_mp3, priority="backlog")

    assert response.status_code == 429
    assert "drain" in response.json()["detail"]


def test_bulk_is_refused_for_the_same_reasons_as_backlog(client, token, tagged_mp3, monkeypatch, upload_settings) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, backlog=upload_settings.upload_max_queue_backlog + 10)
    response = post(client, tagged_mp3, priority="bulk")
    assert response.status_code == 429
    assert response.json()["class"] == "bulk"


def test_live_is_not_refused_while_only_the_backlog_band_is_full(client, token, tagged_mp3, monkeypatch, upload_settings) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=3, backlog=upload_settings.upload_max_queue_backlog * 10, audio=90000.0)

    # The point of the whole design: a live call is stored while the migration is refused.
    assert post(client, tagged_mp3, priority="live").status_code == 200
    assert post(client, tagged_mp3, priority="backlog", audioName="other.mp3").status_code == 429


def test_live_is_refused_only_over_its_own_cap(client, token, tagged_mp3, monkeypatch, upload_settings) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=upload_settings.upload_max_queue_live, audio=50.0)

    response = post(client, tagged_mp3, priority="live")

    assert response.status_code == 429
    assert response.json()["class"] == "live"
    assert 5 <= int(response.headers["Retry-After"]) <= 600


def test_a_recorder_that_sent_no_priority_gets_the_plain_text_body(client, token, tagged_mp3, monkeypatch, upload_settings) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=upload_settings.upload_max_queue_live, audio=50.0)

    response = post(client, tagged_mp3)

    assert response.status_code == 429
    assert response.text == "queue full, retry later"  # sdrtrunk string-matches; no JSON
    assert response.headers["Retry-After"]


def test_a_recorder_that_sent_no_priority_is_never_refused_for_backlog_reasons(client, token, tagged_mp3, monkeypatch) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=4, backlog=100000, audio=1e6)
    response = post(client, tagged_mp3)
    assert response.status_code == 200
    assert response.text == "Call imported successfully."


def test_no_disk_refuses_every_class(client, token, tagged_mp3, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(flow, "disk_free", lambda path: 1024)  # noqa: ARG005

    live = post(client, tagged_mp3, priority="live")
    assert live.status_code == 507
    assert live.json()["error"] == "insufficient_storage"
    assert int(live.headers["Retry-After"]) == flow.DISK_RETRY_AFTER

    legacy = post(client, tagged_mp3)
    assert legacy.status_code == 507
    assert legacy.text == "insufficient storage"


def test_an_unmeasurable_disk_never_refuses(client, token, tagged_mp3, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(flow, "disk_free", lambda path: None)  # noqa: ARG005
    assert post(client, tagged_mp3, priority="backlog").status_code == 200


def test_the_in_flight_semaphore_bounds_concurrent_backlog_uploads(monkeypatch, upload_settings) -> None:  # noqa: ANN001
    view = flow.QueueGauge(
        live=0, backlog=0, audio_seconds=0.0, eta_seconds=0.0,
        accepting_live=True, accepting_backlog=True, paused_backlog=False,
    )
    settings = upload_settings.model_copy(update={"upload_backlog_max_concurrency": 2})
    choice = flow.parse_priority("backlog")

    held = [flow.admit(choice, view, settings, audio_root=settings.audio_root) for _ in range(2)]
    assert all(a.ok for a in held)

    refused = flow.admit(choice, view, settings, audio_root=settings.audio_root)
    assert not refused.ok
    assert refused.status == 429
    assert refused.retry_after == flow.MIN_RETRY_AFTER
    assert "in flight" in refused.detail

    held[0].release()
    assert flow.admit(choice, view, settings, audio_root=settings.audio_root).ok


def test_the_slot_is_given_back_by_a_finished_upload(client, token, tagged_mp3, monkeypatch, upload_settings) -> None:  # noqa: ANN001
    """A one-slot semaphore that leaked would refuse the *second* upload, not the first."""
    monkeypatch.setattr(upload_module, "get_settings", lambda: upload_settings.model_copy(
        update={"upload_backlog_max_concurrency": 1}
    ))
    assert post(client, tagged_mp3, priority="backlog").status_code == 200
    assert post(client, tagged_mp3, priority="backlog", audioName="second.mp3").status_code == 200


@pytest.mark.parametrize(
    ("live", "backlog", "audio"),
    [(0, 400, 10.0), (0, 350, 100000.0), (5000, 400, 500.0)],
)
def test_retry_after_stays_inside_its_bounds(live, backlog, audio, upload_settings) -> None:  # noqa: ANN001
    view = flow.QueueGauge(
        live=live, backlog=backlog, audio_seconds=audio,
        eta_seconds=audio / 2.0, accepting_live=False, accepting_backlog=False,
        paused_backlog=False,
    )
    decision = flow.admit(
        flow.parse_priority("backlog"), view, upload_settings, audio_root=upload_settings.audio_root
    )
    assert not decision.ok
    assert flow.MIN_RETRY_AFTER <= decision.retry_after <= flow.MAX_RETRY_AFTER


# --------------------------------------------------------------------------------------
# the pause switch
# --------------------------------------------------------------------------------------


def test_pausing_refuses_backlog_and_leaves_live_alone(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    from app import runtime_settings
    from app.models import Setting

    session.add(Setting(key="upload_pause_backlog", value=True))
    session.commit()
    runtime_settings.invalidate()

    refused = post(client, tagged_mp3, priority="backlog")
    assert refused.status_code == 429
    assert "paused" in refused.json()["detail"]
    assert refused.headers["X-Keyup-Accepting-Backlog"] == "no"

    assert post(client, tagged_mp3, priority="live").status_code == 200
    assert post(client, tagged_mp3, audioName="legacy.mp3").status_code in (200,)


def test_resuming_accepts_backlog_again(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    from app import runtime_settings
    from app.models import Setting

    session.add(Setting(key="upload_pause_backlog", value=True))
    session.commit()
    runtime_settings.invalidate()
    assert post(client, tagged_mp3, priority="backlog").status_code == 429

    session.query(Setting).filter(Setting.key == "upload_pause_backlog").delete()
    session.commit()
    runtime_settings.invalidate()
    assert post(client, tagged_mp3, priority="backlog").status_code == 200


# --------------------------------------------------------------------------------------
# GET /api/upload/flow
# --------------------------------------------------------------------------------------


def test_the_flow_endpoint_answers_the_gauge(client, token, monkeypatch) -> None:  # noqa: ANN001
    gauge_reading(monkeypatch, live=11, backlog=2, audio=44.0)

    response = client.get("/api/upload/flow", params={"key": KEY})

    assert response.status_code == 200
    body = response.json()
    assert body["live"] == 11
    assert body["backlog"] == 2
    assert body["audio_seconds"] == 44.0
    assert body["accepting"] == {"live": True, "backlog": True}
    assert body["paused_backlog"] is False
    assert response.headers["X-Keyup-Queue-Live"] == "11"


def test_the_flow_endpoint_accepts_a_bearer_token(client, token) -> None:  # noqa: ANN001
    response = client.get("/api/upload/flow", headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 200


def test_the_flow_endpoint_needs_the_upload_scope(client, session) -> None:  # noqa: ANN001
    session.add(
        ApiToken(
            label="reader",
            token_hash=hashlib.sha256(b"read-only-key").hexdigest(),
            scopes=["read"],
        )
    )
    session.commit()

    assert client.get("/api/upload/flow").status_code == 401
    assert client.get("/api/upload/flow", params={"key": "read-only-key"}).status_code == 401


# --------------------------------------------------------------------------------------
# POST /api/upload/exists
# --------------------------------------------------------------------------------------


def stored_digest(client, token, tagged_mp3) -> str:  # noqa: ANN001
    """Upload the fixture and hand back the sha256 the server stored it under."""
    assert post(client, tagged_mp3, priority="backlog").status_code == 200
    return hashlib.sha256(tagged_mp3.read_bytes()).hexdigest()


def test_the_precheck_names_the_hashes_already_stored(client, token, session, tagged_mp3) -> None:  # noqa: ANN001
    digest = stored_digest(client, token, tagged_mp3)
    assert session.scalars(select(Call.sha256)).one() == digest
    absent = "b" * 64

    response = client.post(
        "/api/upload/exists", params={"key": KEY}, json={"sha256": [digest, absent]}
    )

    assert response.status_code == 200
    assert response.json() == {"present": [digest]}


def test_the_precheck_answers_nothing_for_an_empty_archive(client, token) -> None:  # noqa: ANN001
    response = client.post("/api/upload/exists", params={"key": KEY}, json={"sha256": ["a" * 64]})
    assert response.status_code == 200
    assert response.json() == {"present": []}


def test_the_precheck_takes_an_empty_list(client, token) -> None:  # noqa: ANN001
    response = client.post("/api/upload/exists", params={"key": KEY}, json={"sha256": []})
    assert response.status_code == 200
    assert response.json() == {"present": []}


def test_the_precheck_accepts_a_bearer_token(client, token) -> None:  # noqa: ANN001
    response = client.post(
        "/api/upload/exists",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"sha256": ["a" * 64]},
    )
    assert response.status_code == 200


def test_the_precheck_does_not_count_as_a_backlog_upload(client, token, monkeypatch) -> None:  # noqa: ANN001
    # The gauge is over the transcription queue; asking a question queues nothing, so a
    # paused-backlog server still answers.  Nothing here may be refused for flow reasons.
    gauge_reading(monkeypatch, live=99999, backlog=99999, audio=1e9)
    response = client.post("/api/upload/exists", params={"key": KEY}, json={"sha256": ["a" * 64]})
    assert response.status_code == 200


@pytest.mark.parametrize("bad", ["not-hex", "", "a" * 63, "A" * 65, "  "])
def test_the_precheck_refuses_something_that_is_not_a_digest(client, token, bad) -> None:  # noqa: ANN001
    response = client.post("/api/upload/exists", params={"key": KEY}, json={"sha256": [bad]})
    assert response.status_code == 422


def test_the_precheck_refuses_more_than_the_batch_cap(client, token) -> None:  # noqa: ANN001
    from app.api.call_upload import MAX_EXISTS_HASHES

    batch = [f"{i:064x}" for i in range(MAX_EXISTS_HASHES + 1)]
    assert client.post("/api/upload/exists", params={"key": KEY}, json={"sha256": batch}).status_code == 422
    ok = client.post("/api/upload/exists", params={"key": KEY}, json={"sha256": batch[:-1]})
    assert ok.status_code == 200


def test_the_precheck_needs_the_upload_scope(client, session) -> None:  # noqa: ANN001
    session.add(
        ApiToken(
            label="reader",
            token_hash=hashlib.sha256(b"read-only-key").hexdigest(),
            scopes=["read"],
        )
    )
    session.commit()

    assert client.post("/api/upload/exists", json={"sha256": []}).status_code == 401
    refused = client.post(
        "/api/upload/exists", params={"key": "read-only-key"}, json={"sha256": []}
    )
    assert refused.status_code == 401


# --------------------------------------------------------------------------------------
# the gauge query itself
# --------------------------------------------------------------------------------------


def test_the_gauge_counts_both_bands_and_the_audio_behind_them(session, upload_settings, tagged_mp3) -> None:  # noqa: ANN001
    from tests.test_stt_worker import seed_call

    seed_call(session, tgid=1701, duration=4.0, priority=100, file_path="a.mp3")
    seed_call(session, tgid=1102, duration=6.0, priority=600, file_path="b.mp3")
    seed_call(session, tgid=1103, duration=10.0, priority=900, file_path="c.mp3")

    view = flow.gauge(session, upload_settings, force=True)

    assert (view.live, view.backlog) == (1, 2)
    assert view.audio_seconds == pytest.approx(20.0)
    assert view.eta_seconds > 0


def test_the_gauge_is_cached_for_a_couple_of_seconds(session, upload_settings) -> None:  # noqa: ANN001
    from tests.test_stt_worker import seed_call

    first = flow.gauge(session, upload_settings, force=True)
    seed_call(session, tgid=1701, duration=4.0, priority=100, file_path="a.mp3")
    assert flow.gauge(session, upload_settings).live == first.live  # still the cached reading
    assert flow.gauge(session, upload_settings, force=True).live == first.live + 1


def test_upload_exists_is_a_daemon_route_not_a_browser_one() -> None:
    """The migration client sends only ``?key=`` — no cookie, no CSRF header, no Origin.

    Like ``/api/call-upload`` and the recorder routes, the path must be exempt from the
    cookie CSRF proof and from the app-wide ``write``-scope rule; the endpoint checks the
    ``upload``-scope key itself. Regression: the first deployment answered every precheck
    with 403 "Missing x-requested-with", and the client quietly uploaded everything.
    """
    from app.auth import NO_CSRF_PATHS, OPEN_MUTATION_PATHS

    assert "/api/upload/exists" in NO_CSRF_PATHS
    assert "/api/upload/exists" in OPEN_MUTATION_PATHS
