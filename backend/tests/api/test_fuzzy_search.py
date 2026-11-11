"""Fuzzy search over transcripts (``search_vocab`` + ``pg_trgm``) and aliases.

The acceptance case is the production one: Whisper writes ``Milrose`` for Melrose Avenue
(60 calls on production against 2 476 spelt right) and ``Kelguck`` for Kelbuk Street.
A reader searching *melrose* must be able to reach the Milrose calls, and a reader who
misspells *kelbuck* must still find Kelbuk — while an exact search returns exactly what
it did before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.services import fuzzy
from tests.conftest import BACKEND_DIR, requires_db

MIGRATION = "ec34a3a39b74"
BEFORE = "1054b8161324"
ALIAS_INDEXES = {
    "ix_talkgroups_alias_trgm": "talkgroups",
    "ix_radios_alias_trgm": "radios",
    "ix_radios_unit_id_trgm": "radios",
    "ix_units_callsign_trgm": "units",
    "ix_units_name_trgm": "units",
    "ix_personnel_name_trgm": "personnel",
}

TRANSCRIPTS = {
    "melrose": "Medic 3 respond to 1200 Melrose Avenue for a fall.",
    "milrose": "Engine 1 en route to Milrose Avenue, smoke showing.",
    "kelbuk": "Car 12 traffic stop on Kelbuk Street near the bridge.",
    "kelguck": "Units clear, one in custody at Kelguck Street.",
    "drill": "Engine 1 at Melrose Avenue for the drill.",
}


# --------------------------------------------------------------------------------------
# the rewrite, no database
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "limit", "expected"),
    [
        ("melros", "milros", 2, 1),
        ("kelbuck", "kelguck", 2, 1),
        ("kelbuk", "kelguck", 2, 2),
        ("rochest", "chest", 2, 2),
        ("abcd", "abdc", 1, 1),  # a transposition is one edit
        ("park", "parkview", 1, 2),  # capped at limit + 1
        ("fire", "fire", 1, 0),
    ],
)
def test_edit_distance(a: str, b: str, limit: int, expected: int) -> None:
    assert fuzzy.edit_distance(a, b, limit) == expected


def test_only_positive_lexemes_are_widened() -> None:
    """A negated word, or any word inside a negated group, stays exact."""
    query = "'structur' <-> 'fire' & !'drill' & !( 'fals' <-> 'alarm' ) & 'melros'"
    assert fuzzy.positive_terms(query) == ["structur", "fire", "melros"]
    widened = fuzzy._rewrite(query, lambda term: {"melros": ["milros"], "drill": ["dril"]}.get(term, []))
    assert widened == (
        "'structur' <-> 'fire' & !'drill' & !( 'fals' <-> 'alarm' ) & ( 'melros' | 'milros' )"
    )


def test_quotes_and_backslashes_round_trip() -> None:
    assert fuzzy._quote("o'brien") == "'o''brien'"
    assert fuzzy._quote("a\\b") == "'a\\\\b'"
    assert fuzzy._unquote("o''brien") == "o'brien"


@pytest.mark.parametrize(
    ("term", "wanted"), [("melros", True), ("fire", True), ("fir", False), ("e12", False), ("1432", False)]
)
def test_short_words_and_numbers_are_not_widened(term: str, wanted: bool) -> None:
    assert fuzzy.wants_variants(term) is wanted


# --------------------------------------------------------------------------------------
# against the database
# --------------------------------------------------------------------------------------


def _call(session: Session, system_id: int, n: int, transcript: str | None, alias: str | None = None):  # noqa: ANN202
    from app.models import Call

    at = datetime(2026, 9, 20, 12, 0, tzinfo=UTC) + timedelta(minutes=n)
    call = Call(
        system_id=system_id,
        recorded_at=at,
        started_at=at,
        started_local=at.replace(tzinfo=None),
        ended_at=at + timedelta(seconds=5),
        duration_s=5,
        file_path=f"kd2/{n}.mp3",
        file_name=f"{n}.mp3",
        transcript_text=transcript,
        transcript_status="done" if transcript else "pending",
        talkgroup_alias_at_record=alias,
    )
    session.add(call)
    return call


@pytest.fixture
def streets(session: Session) -> dict[str, int]:
    """Five transcribed calls and a vocabulary built from them."""
    from app.models import System

    system = System(name="kd2")
    session.add(system)
    session.flush()
    ids = {}
    for n, (key, transcript) in enumerate(TRANSCRIPTS.items()):
        call = _call(session, system.id, n, transcript)
        session.flush()
        ids[key] = call.id
    fuzzy.refresh_vocabulary(session, full=True)
    session.commit()
    return ids


pytestmark = requires_db


def _ids(body: dict) -> set[int]:
    return {hit["call"]["id"] for hit in body["items"]}


def test_exact_search_is_unchanged(client, streets) -> None:  # noqa: ANN001
    """No ``fuzzy``, or ``fallback`` with exact hits: the same answer, and no ``fuzzy`` block."""
    plain = client.get("/api/search", params={"q": "melrose"}).json()
    assert _ids(plain) == {streets["melrose"], streets["drill"]}
    assert plain["fuzzy"] is None
    with_fallback = client.get("/api/search", params={"q": "melrose", "fallback": True}).json()
    assert with_fallback == plain


def test_fuzzy_finds_the_misspelt_street(client, streets) -> None:  # noqa: ANN001
    """The acceptance case: *melrose* reaches the call Whisper wrote as *Milrose*."""
    body = client.get("/api/search", params={"q": "melrose", "fuzzy": True}).json()
    assert _ids(body) == {streets["melrose"], streets["milrose"], streets["drill"]}
    assert body["fuzzy"]["automatic"] is False
    assert body["fuzzy"]["widened"] is True
    assert body["fuzzy"]["terms"] == [{"term": "melros", "variants": ["milros"]}]
    # The highlight marks the spelling that matched, because it is the same tsquery.
    milrose = next(h for h in body["items"] if h["call"]["id"] == streets["milrose"])
    assert "<mark>Milrose</mark>" in milrose["highlight"]


def test_a_misspelt_query_falls_back_by_itself(client, streets) -> None:  # noqa: ANN001
    """*kelbuck* matches nothing exactly; with ``fallback`` it finds both Kelbuk calls."""
    assert client.get("/api/search", params={"q": "kelbuck"}).json()["total"] == 0
    response = client.get("/api/search", params={"q": "kelbuck street", "fallback": True})
    body = response.json()
    assert _ids(body) == {streets["kelbuk"], streets["kelguck"]}
    assert response.headers["X-Total-Count"] == "2"
    assert body["fuzzy"]["automatic"] is True
    terms = {t["term"]: t["variants"] for t in body["fuzzy"]["terms"]}
    assert set(terms["kelbuck"]) == {"kelbuk", "kelguck"}


def test_a_fallback_that_finds_nothing_close_says_so(client, streets) -> None:  # noqa: ANN001
    body = client.get("/api/search", params={"q": "zzyzx", "fallback": True}).json()
    assert body["total"] == 0
    assert body["fuzzy"] == {
        "automatic": True,
        "widened": False,
        "terms": [{"term": "zzyzx", "variants": []}],
    }


def test_a_negated_word_stays_exact(client, streets) -> None:  # noqa: ANN001
    body = client.get("/api/search", params={"q": "avenue -drill -milrose", "fuzzy": True}).json()
    # -milrose removes the Milrose call only; the Melrose call is not dragged out with it.
    assert _ids(body) == {streets["melrose"]}
    assert [t["term"] for t in body["fuzzy"]["terms"]] == ["avenu"]


def test_fuzzy_is_a_filter_everywhere(client, streets) -> None:  # noqa: ANN001
    """The list, the facets and the search agree on what ``fuzzy`` matches."""
    params = {"q": "melrose", "fuzzy": True}
    search = client.get("/api/search", params=params).json()
    listed = client.get("/api/calls", params=params).json()
    facets = client.get("/api/calls/facets", params=params).json()
    assert search["total"] == listed["total"] == facets["total"] == 3
    assert client.get("/api/calls/facets", params={"q": "melrose"}).json()["total"] == 2


def test_an_established_word_does_not_widen_to_a_commoner_one(session: Session) -> None:
    """``rochester`` must not reach ``chest``; a rare typo still reaches the common spelling."""
    session.execute(
        text(
            "INSERT INTO search_vocab (word, ndoc, nentry) VALUES "
            "('rochest', 1000, 1000), ('chest', 4700, 4700), ('lochest', 22, 22), "
            "('kelbuck', 1, 1), ('kelbuk', 1437, 1437)"
        )
    )
    found = fuzzy.variants_for(session, ["rochest", "kelbuck"])
    assert found["rochest"] == ["lochest"]
    assert found["kelbuck"] == ["kelbuk"]
    session.rollback()


def test_the_vocabulary_follows_new_transcripts(session: Session, streets) -> None:  # noqa: ANN001
    """Incremental passes add new transcripts' words; the daily rebuild drops the gone ones."""
    from app.models import Call, Setting, Transcript

    state = session.get(Setting, fuzzy.STATE_KEY).value
    call = session.get(Call, streets["drill"])
    session.add(Transcript(call_id=call.id, engine="openai_compatible", text_="x", is_primary=True))
    call.transcript_text = "Ladder 4 to Tamarack Avenue"
    session.flush()
    summary = fuzzy.refresh_vocabulary(session)
    assert summary["mode"] == "incremental"
    assert summary["since"] == state["transcript_id"]
    words = set(session.scalars(text("SELECT word FROM search_vocab")))
    assert "tamarack" in words
    assert "drill" in words  # still there until a rebuild notices it is gone
    assert fuzzy.refresh_vocabulary(session)["written"] == 0  # nothing new since

    summary = fuzzy.refresh_vocabulary(session, full=True)
    assert summary["mode"] == "full"
    words = set(session.scalars(text("SELECT word FROM search_vocab")))
    assert "drill" not in words
    assert session.scalar(text("SELECT ndoc FROM search_vocab WHERE word = 'avenu'")) == 3
    # The daily rule: a rebuild a day after the last one, an incremental pass before.
    later = datetime.now(UTC) + fuzzy.FULL_REFRESH_EVERY + timedelta(minutes=1)
    assert fuzzy.refresh_vocabulary(session, now=later)["mode"] == "full"
    session.rollback()


def test_suggest_offers_near_spellings_only_when_asked(client, session: Session) -> None:  # noqa: ANN001
    from app.models import System, Talkgroup

    system = System(name="kd2-tg")
    session.add(system)
    session.flush()
    session.add(Talkgroup(system_id=system.id, tgid=4101, alias="RIVERTON PD", call_count=50))
    session.commit()

    assert client.get("/api/search/suggest", params={"q": "Rivertn"}).json() == []
    near = client.get("/api/search/suggest", params={"q": "Rivertn", "fuzzy": True}).json()
    assert near == [
        {"value": "RIVERTON PD", "kind": "talkgroup", "count": 50, "label": None, "fuzzy": True}
    ]
    # A substring match is still a substring match, and comes first.
    exact = client.get("/api/search/suggest", params={"q": "RIVER", "fuzzy": True}).json()
    assert exact[0]["value"] == "RIVERTON PD"
    assert exact[0]["fuzzy"] is False


def test_the_alias_indexes_exist_and_serve_the_suggestions(session: Session) -> None:
    """All six trigram indexes are valid, and match the near-spelling and ``ILIKE`` statements.

    Only the two columns without a btree of their own are plan-checked: on an empty table
    the planner prefers a full bitmap scan of any btree to a GIN index, which says nothing
    about 13 k radios (there, ``unit_id <% …`` is a Bitmap Index Scan on the trigram index,
    0.05 ms).  ``search_vocab``'s index is likewise not plan-checked on an empty table; on
    the 181 k-word production vocabulary the lookup uses it, 0.8 ms for one word.
    """
    rows = session.execute(
        text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = ANY(:names)"
        ),
        {"names": [*ALIAS_INDEXES, "ix_search_vocab_word_trgm"]},
    ).all()
    assert dict(rows) == dict.fromkeys([*ALIAS_INDEXES, "ix_search_vocab_word_trgm"], True)

    session.execute(text("SET LOCAL enable_seqscan = off"))
    plans = {
        index: "\n".join(r[0] for r in session.execute(text(f"EXPLAIN {sql}")))
        for index, sql in (
            ("ix_talkgroups_alias_trgm", "SELECT alias FROM talkgroups WHERE 'rivertn' <% alias"),
            ("ix_radios_alias_trgm", "SELECT alias FROM radios WHERE alias ILIKE '%eng%'"),
        )
    }
    session.rollback()
    for index, plan in plans.items():
        assert index in plan, plan


def test_the_migration_goes_down_and_up(session: Session) -> None:
    """Down removes the table, indexes and watermark; up rebuilds the vocabulary from calls."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command
    from app.db import engine
    from app.models import System

    system = System(name="kd2-migration")
    session.add(system)
    session.flush()
    _call(session, system.id, 1, "Respond to Milrose Avenue")
    session.commit()
    session.close()

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    def state() -> tuple[set[str], bool]:
        engine.dispose()
        inspector = inspect(engine)
        found = {ix["name"] for table in set(ALIAS_INDEXES.values()) for ix in inspector.get_indexes(table)}
        return found & set(ALIAS_INDEXES), inspector.has_table("search_vocab")

    assert state() == (set(ALIAS_INDEXES), True)
    command.downgrade(config, BEFORE)
    try:
        assert state() == (set(), False)
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM settings WHERE key = :k"), {"k": fuzzy.STATE_KEY}
            ).scalar_one() == 0
    finally:
        command.upgrade(config, "head")
    assert state() == (set(ALIAS_INDEXES), True)
    with engine.connect() as conn:
        words = set(conn.execute(text("SELECT word FROM search_vocab")).scalars())
        watermark = conn.execute(
            text("SELECT value FROM settings WHERE key = :k"), {"k": fuzzy.STATE_KEY}
        ).scalar_one()
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert {"milros", "avenu", "respond"} <= words
    assert set(watermark) == {"transcript_id", "full_at"}
    datetime.fromisoformat(watermark["full_at"])  # the worker must be able to read it
    script = ScriptDirectory.from_config(config)
    # back at head — which a later migration may have moved past this one
    assert current == script.get_current_head()
    assert MIGRATION in {rev.revision for rev in script.walk_revisions()}


def test_a_widened_search_pages_the_same_way_it_started(client, streets) -> None:  # noqa: ANN001
    """The Search page's scroll: page 1 fell back by itself, and asks for page 2 with the
    ``fuzzy=true`` page 1 reported, so the pages are slices of one widened ranking."""
    params = {"q": "kelbuck street", "fallback": True, "page_size": 1, "facets": False}
    first = client.get("/api/search", params=params).json()
    assert first["fuzzy"]["automatic"] is True
    assert first["total"] == 2
    second = client.get("/api/search", params={**params, "page": 2, "fuzzy": True}).json()
    assert second["total"] == 2
    assert second["fuzzy"]["automatic"] is False
    assert second["fuzzy"]["widened"] is True
    assert _ids(first) | _ids(second) == {streets["kelbuk"], streets["kelguck"]}
    assert not _ids(first) & _ids(second)
