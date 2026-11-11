"""Resolved talkgroup sets, and the bounds on the facet queries (§4.1).

The incident behind all of this: ``/calls?preset=today&category=law&…`` (seven categories)
put Postgres at 430 % for minutes and starved the pool until ``POST /api/recorder/spectrum``
timed out with a 500.  The cause was a plan, not a volume — ``talkgroup_ids && (SELECT
array_agg(tgid) …)`` hides the array's size from the planner, which guessed 283 rows and
read 2.9 million GIN entries per facet query.  Three defences are tested here: the sets are
resolved to literal arrays before the statement is built, a facet query that runs long is
cancelled rather than allowed to hold its connection, and the answer is cached for a minute.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api.filters import (
    CallFilters,
    aggregate_select,
    base_call_select,
    filters_from_mapping,
    resolve_filter_sets,
)
from app.models import Call, Talkgroup
from app.services import calls as call_service
from app.services import counts as counts_service
from tests.conftest import requires_db

pytestmark = requires_db


def _sql(f: CallFilters) -> str:
    stmt = aggregate_select(f, func.count())
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _ids(session: Session, f: CallFilters) -> set[int]:
    return {row[0] for row in session.execute(base_call_select(f, Call.id))}


@pytest.mark.parametrize(
    "params",
    [
        {"category": ["fire"]},
        {"category": ["fire", "law"]},
        {"category": ["nothing-is-in-this-category"]},
        {"agency": ["EXAMPLEFD"]},
        {"agency": ["EXAMPLEFD"], "include_shared": True},
        {"talkgroup": [1701]},
        {"talkgroup": [1701, 1601]},
    ],
)
def test_resolving_a_filter_set_does_not_change_which_calls_match(
    session: Session, archive, params: dict
) -> None:
    """The whole fix is a plan change and nothing else: same filter, same rows."""
    f = filters_from_mapping(params)
    assert _ids(session, f) == _ids(session, resolve_filter_sets(session, f))


def test_a_resolved_set_is_a_literal_array_and_not_a_subquery(session: Session, archive) -> None:
    """What the planner can see is the point — an InitPlan it cannot cost is what cost 13.8 s."""
    f = filters_from_mapping({"category": ["fire"]})
    before = _sql(f)
    assert "SELECT coalesce(array_agg" in before

    after = _sql(resolve_filter_sets(session, f))
    assert "array_agg" not in after
    tgid = session.scalar(select(Talkgroup.tgid).where(func.lower(Talkgroup.category) == "fire"))
    assert f"{tgid}" in after
    assert "talkgroup_ids && CAST('{" in after


def test_a_category_nothing_is_filed_under_resolves_to_nothing_at_all(session: Session, archive) -> None:
    """An empty set used to be ``&& '{}'``, a scan for an answer that cannot exist."""
    f = resolve_filter_sets(session, filters_from_mapping({"category": ["no-such-category"]}))
    assert f._resolved["category"] == ()
    assert "false" in _sql(f).lower()
    assert _ids(session, f) == set()


def test_the_talkgroup_filter_is_the_on_air_number_and_stays_patch_aware(
    session: Session, archive
) -> None:
    """``talkgroup=`` is the on-air number, and it matches the whole ``talkgroup_ids``
    array, so a patch carrying the channel is found (§1.4).

    It used to accept this archive's ``talkgroups.id`` as a second reading, resolved
    through the directory and OR'd in.  The two number spaces overlap, so that reading
    quietly widened every filter the SPA built to whatever channel carried the pk as its
    tgid; the resolved set is now the caller's own numbers and nothing else.
    """
    patch = session.get(Call, archive.patch_call_id)
    member = next(t for t in patch.talkgroup_ids if t != patch.talkgroup.tgid)
    by_number = resolve_filter_sets(session, filters_from_mapping({"talkgroup": [member]}))
    assert archive.patch_call_id in _ids(session, by_number)

    row = session.scalar(select(Talkgroup).where(Talkgroup.tgid == 1701))
    by_key = resolve_filter_sets(session, filters_from_mapping({"talkgroup": [row.id]}))
    assert by_key._resolved["talkgroup"] == (row.id,)
    assert 1701 not in by_key._resolved["talkgroup"]
    # Resolving is a plan change and nothing else, here as everywhere.
    assert _ids(session, by_key) == _ids(session, filters_from_mapping({"talkgroup": [row.id]}))


def test_replacing_a_filter_drops_what_was_resolved_for_the_old_one(session: Session, archive) -> None:
    """``replace`` changes the question, so the cached answer must not survive it."""
    fire = resolve_filter_sets(session, filters_from_mapping({"category": ["fire"]}))
    assert fire._resolved["category"]
    assert fire.replace(category=("law",))._resolved == {}


def test_echo_never_leaks_the_resolved_sets(session: Session, archive) -> None:
    """``echo()`` is stored on export jobs and returned by ``/links``: it is the filter the
    caller asked for, not a thousand tgids the server worked out."""
    f = resolve_filter_sets(session, filters_from_mapping({"category": ["fire"]}))
    assert f.echo() == {
        "category": ["fire"],
        "sort": "-started_at",
        "include_duplicates": False,
        "include_shared": False,
    }


# --------------------------------------------------------------------------------------
# the facet bounds
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_facet_cache():  # noqa: ANN202
    counts_service.invalidate()
    yield
    counts_service.invalidate()


def test_a_facet_that_times_out_is_dropped_rather_than_failing_the_request(
    client, archive, monkeypatch
) -> None:
    """Eight counts and a caveat beat a 500 — and the rollback in between is what makes the
    dimensions *after* the cancelled one possible at all.

    The stand-in for a bad plan is ``pg_sleep`` in the predicate of every *dimension*
    query, against a 20 ms timeout.  The total is counted through ``count_call_select`` and
    is left alone, so this also pins the shape of a partial answer: the dimension that
    finished is still there and still exact.
    """
    real = call_service.base_call_select
    monkeypatch.setattr(
        call_service,
        "base_call_select",
        lambda f, *columns: real(f, *columns).where(literal_column("pg_sleep(0.05)::text IS NOT NULL")),
    )
    monkeypatch.setattr(call_service, "FACET_TIMEOUT", "20ms")
    response = client.get("/api/calls/facets")
    assert response.status_code == 200
    body = response.json()
    assert body["incomplete"] is True
    assert body["total"] == len(archive.call_ids)
    assert body["talkgroup"] == []

    # And the request after it is unaffected: the timeout was SET LOCAL, so it died with
    # the transaction rather than riding the pooled connection to the next caller.
    counts_service.invalidate()
    monkeypatch.undo()
    again = client.get("/api/calls/facets").json()
    assert again["incomplete"] is False
    assert again["total"] == len(archive.call_ids)


def test_facets_are_computed_once_a_minute_for_the_same_filter(
    session: Session, archive, monkeypatch
) -> None:
    """Seven clicks on the category picker are seven requests; they are not seven rounds of
    nine aggregate queries."""
    runs = []
    real = call_service._facets
    monkeypatch.setattr(
        call_service,
        "_facets",
        lambda *a, **kw: (runs.append(1), real(*a, **kw))[1],
    )
    first = call_service.facets(session, CallFilters(), limit=5)
    second = call_service.facets(session, CallFilters(), limit=5)
    assert len(runs) == 1
    assert second is first

    # A different filter is a different question and is computed.
    call_service.facets(session, filters_from_mapping({"category": ["fire"]}), limit=5)
    assert len(runs) == 2

    # …and the sort is not part of it: it reorders rows, it does not change the counts.
    call_service.facets(session, CallFilters(sort="duration"), limit=5)
    assert len(runs) == 2


def test_a_burst_of_identical_requests_computes_once(session: Session, archive, monkeypatch) -> None:
    """Single-flight: the picker's second click waits for the first one's answer instead of
    launching its own five heavy queries beside it."""
    runs = []

    def slow(*_args, **_kwargs):  # noqa: ANN202
        runs.append(1)
        time.sleep(0.2)
        return call_service.FacetsOut(total=len(archive.call_ids))

    monkeypatch.setattr(call_service, "_facets", slow)
    start = threading.Barrier(6)

    def ask() -> None:
        start.wait(timeout=5)
        call_service.facets(session, CallFilters(), limit=5)

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(runs) == 1
