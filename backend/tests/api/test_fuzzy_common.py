"""A word common enough to have no spelling problem is searched exactly under ``fuzzy``.

On production an all-time fuzzy search for *fire* (427 k calls) widened it to eight
spellings that added 31 calls between them, and paid for it by ranking every one of the
427 k against a nine-lexeme query.  :data:`app.services.fuzzy.COMMON` stops that: a term
in that many calls or more gets no variants, so the fuzzy search runs the exact query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import fuzzy
from tests.conftest import requires_db

pytestmark = requires_db

#: Four calls say *fire*, one says *firet* — Whisper's version of it.
TRANSCRIPTS = [
    "Engine 1 respond to a structure fire on Melrose Avenue.",
    "Command, the fire is knocked down.",
    "Fire alarm at the school, Engine 2 respond.",
    "Medic 3, stage for the fire.",
    "Ladder 1 on scene of the firet.",
    "Milrose Avenue, smoke showing.",
]


@pytest.fixture
def calls(session: Session) -> list[int]:
    from app.models import Call, System

    system = System(name="kd8")
    session.add(system)
    session.flush()
    ids = []
    for n, transcript in enumerate(TRANSCRIPTS):
        at = datetime(2026, 9, 20, 12, 0, tzinfo=UTC) + timedelta(minutes=n)
        call = Call(
            system_id=system.id,
            recorded_at=at,
            started_at=at,
            started_local=at.replace(tzinfo=None),
            ended_at=at + timedelta(seconds=5),
            duration_s=5,
            file_path=f"kd8/{n}.mp3",
            file_name=f"{n}.mp3",
            transcript_text=transcript,
            transcript_status="done",
        )
        session.add(call)
        session.flush()
        ids.append(call.id)
    fuzzy.refresh_vocabulary(session, full=True)
    session.commit()
    return ids


def test_a_common_term_gets_no_variants(session: Session) -> None:
    """At :data:`COMMON` calls a term is not widened; one call below it still is."""
    common = fuzzy.COMMON
    session.execute(
        text(
            "INSERT INTO search_vocab (word, ndoc, nentry) VALUES "
            "('fire', :common, :common), ('firet', 7, 7), "
            "('melros', :below, :below), ('milros', 60, 60)"
        ),
        {"common": common, "below": common - 1},
    )
    found = fuzzy.variants_for(session, ["fire", "melros"])
    assert found == {"fire": [], "melros": ["milros"]}
    session.rollback()


def test_expand_reports_the_common_term_but_does_not_widen_it(session: Session) -> None:
    session.execute(
        text(
            "INSERT INTO search_vocab (word, ndoc, nentry) VALUES "
            "('fire', :n, :n), ('firet', 7, 7), ('melros', 2466, 2466), ('milros', 60, 60)"
        ),
        {"n": fuzzy.COMMON * 40},
    )
    alone = fuzzy.expand(session, "fire")
    assert alone.tsquery is None
    assert alone.terms == (fuzzy.FuzzyTerm(term="fire", variants=()),)
    # Beside a rare word, only the rare one is widened.
    both = fuzzy.expand(session, "fire melrose")
    assert both.tsquery == "'fire' & ( 'melros' | 'milros' )"
    session.rollback()


def test_fuzzy_on_a_common_word_answers_as_the_exact_search(
    client, calls, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """With *fire* over the cap, ``fuzzy=true`` returns the exact search's calls, ranks and
    highlights, and says that nothing was widened."""
    monkeypatch.setattr(fuzzy, "COMMON", 4)  # 'fire' is in 4 calls here
    exact = client.get("/api/search", params={"q": "fire"}).json()
    widened = client.get("/api/search", params={"q": "fire", "fuzzy": True}).json()
    assert exact["total"] == 4
    assert widened["fuzzy"] == {
        "automatic": False,
        "widened": False,
        "terms": [{"term": "fire", "variants": []}],
    }
    assert exact["fuzzy"] is None
    assert {k: v for k, v in widened.items() if k != "fuzzy"} == {
        k: v for k, v in exact.items() if k != "fuzzy"
    }
    # Under the cap the same query does widen, to the call spelt *firet*.
    monkeypatch.setattr(fuzzy, "COMMON", 5)
    below = client.get("/api/search", params={"q": "fire", "fuzzy": True}).json()
    assert below["total"] == 5
    assert below["fuzzy"]["terms"] == [{"term": "fire", "variants": ["firet"]}]
    assert calls[4] in {hit["call"]["id"] for hit in below["items"]}


def test_the_exact_search_is_untouched_by_the_cap(client, calls, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """The cap only ever changes a ``fuzzy`` search: the exact answer is the same with any value."""
    before = client.get("/api/search", params={"q": "fire avenue"}).content
    monkeypatch.setattr(fuzzy, "COMMON", 1)
    assert client.get("/api/search", params={"q": "fire avenue"}).content == before
