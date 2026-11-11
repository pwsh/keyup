"""Fixtures for the API suite.

Everything runs against the real test database with the fixture recordings: six of the
synthetic MP3s are ingested through ``ingest_file`` — the same code path the watcher uses —
so the rows under test are the rows production would have, generated column and all.
Nothing here fabricates a call row.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import EXAMPLE_AGENCIES, requires_db

pytestmark = requires_db

#: Six fixture recordings covering every shape the corpus contains: a plain group call, a
#: call with no FROM, a fully-qualified radio id, two patches and a ``_V2`` collision.
FIXTURE_FILES = [
    "20260828/EXAMPLE EMS OPS/20260828_055219_Example-System_Central_T-Control__TO_1421_FROM_101010_57_9831120.mp3",
    "20260828/EXAMPLE EVENT 1/20260828_082515_Example-System_Central_T-Control__TO_1701_FROM_9200007_V2.mp3",
    "20260829/EXAMPLE EVENT 1/20260829_030253_Example-System_Central_T-Control__TO_1701.mp3",
    "20260829/EXAMPLE EVENT 1/20260829_141703_Example-System_Central_T-Control__TO_1701_FROM_9200006.mp3",
    "20260829/PATCH 1157/20260829_165914_Example-System_Central_T-Control__TO_P1157-[1812--1561--1811]_FROM_101010_57_9700005.mp3",
    "20260829/EXAMPLE LAW 1/20260829_200945_Example-System_Central_T-Control__TO_1601_FROM_9200102.mp3",
]

#: Headers a browser-style mutation must carry: the custom header plus a same-site Origin.
CSRF_HEADERS = {"X-Requested-With": "keyup", "Origin": "http://testserver"}


@dataclass
class Archive:
    """What the ingest produced, so a test can name a call without re-querying."""

    call_ids: list[int]
    by_tgid: dict[int, list[int]]
    patch_call_id: int
    transcribed_id: int


@pytest.fixture
def archive(session: Session, settings, fixture_audio: Path) -> Archive:  # noqa: ANN001
    """Six ingested recordings, one of them carrying a transcript."""
    from app.ingest.service import ingest_file
    from app.models import Call, Talkgroup, Transcript
    from app.seed import seed_agencies

    seed_agencies(session, path=EXAMPLE_AGENCIES)
    session.commit()

    ids: list[int] = []
    for relative in FIXTURE_FILES:
        result = ingest_file(
            session, fixture_audio / relative, settings=settings, audio_root=fixture_audio
        )
        assert result.status in ("ingested", "duplicate"), result
        assert result.call_id is not None
        ids.append(result.call_id)

    by_tgid: dict[int, list[int]] = {}
    for call in session.scalars(select(Call).where(Call.id.in_(ids))):
        for tgid in call.talkgroup_ids or ():
            by_tgid.setdefault(tgid, []).append(call.id)

    patch_call_id = next(
        c.id for c in session.scalars(select(Call).where(Call.id.in_(ids))) if c.patch_id is not None
    )

    # One call gets a transcript, so search, quality filters, coverage and the correlation
    # text score have something to work on.
    target = session.get(Call, ids[3])
    transcript = Transcript(
        call_id=target.id,
        engine="openai_compatible",
        backend_name="whisper",
        model="Systran/faster-whisper-large-v3",
        language="en",
        text_="Engine 1 responding to the structure fire on Cherry Court, heavy smoke showing.",
        segments=[
            {"start": 0.0, "end": 2.4, "text": "Engine 1 responding to the structure fire", "avg_logprob": -0.2},
            {"start": 2.4, "end": 4.1, "text": "on Cherry Court, heavy smoke showing.", "avg_logprob": -0.3},
        ],
        avg_logprob=-0.25,
        no_speech_prob=0.02,
        compression_ratio=1.4,
        quality="good",
        quality_reasons=[],
        vad_used=True,
        processing_ms=900,
        is_primary=True,
        created_at=datetime.now(UTC),
    )
    session.add(transcript)
    target.transcript_text = transcript.text_
    target.transcript_status = "done"
    target.transcript_quality = "good"
    session.commit()

    # A little directory metadata, so the agency and unit filters have something to match.
    talkgroup = session.get(Talkgroup, target.talkgroup_id)
    talkgroup.agency_code = "EXAMPLEFD"
    talkgroup.category = "fire"
    talkgroup.service_tag = "Fire Dispatch"
    from app.models import Radio  # noqa: PLC0415

    radio = session.get(Radio, target.radio_id)
    radio.unit_id = "E1"
    radio.agency_code = "EXAMPLEFD"
    session.commit()

    return Archive(
        call_ids=ids, by_tgid=by_tgid, patch_call_id=patch_call_id, transcribed_id=target.id
    )


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:  # noqa: ARG001 - ordering: truncate first
    """A TestClient with the application's lifespan running (so the live bus is up)."""
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_token(session: Session) -> str:
    """A raw ``kup_…`` token with every scope."""
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-admin", ["admin"], created_by="tests")
    return raw


@pytest.fixture
def write_token(session: Session) -> str:
    """A token that may write but is not an admin."""
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-write", ["read", "write"], created_by="tests")
    return raw


@pytest.fixture
def read_token(session: Session) -> str:
    from app.auth import create_api_token

    _, raw = create_api_token(session, "tests-read", ["read"], created_by="tests")
    return raw


@pytest.fixture
def user_password() -> str:
    return "correct-horse-battery-staple"


@pytest.fixture
def api_user(session: Session, user_password: str) -> Any:
    from app.auth import hash_password
    from app.models import User

    user = User(
        username="tester",
        display_name="Tester",
        password_hash=hash_password(user_password),
        scopes=["read", "write"],
    )
    session.add(user)
    session.commit()
    return user


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
