"""The data migration that rewrites stored ``query->'talkgroup'`` from pks to tgids.

``?talkgroup=`` used to accept a ``talkgroups.id`` as well as the on-air ``tgid``, and the
SPA sent the primary key, so every filter object saved under the old reading — a feed, a
saved search — holds pks. The parameter now means the tgid and nothing else, which makes
those stored filters point at whatever unrelated channel happens to carry that number.
``a7f3c2d91b64`` translates them in place.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tests.conftest import requires_db

pytestmark = requires_db

REVISION = "a7f3c2d91b64"


def _migration() -> Any:
    """The migration module, loaded by path: ``alembic/versions`` is not a package."""
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / f"{REVISION}_stored_talkgroup_filters_are_tgids.py"
    )
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeded(session: Session, archive) -> dict[str, Any]:  # noqa: ANN001
    """A feed and a saved search filtering on a pk, plus one on a number we own no row for."""
    from app.models import Feed, SavedSearch, Talkgroup

    row = session.scalar(select(Talkgroup).where(Talkgroup.tgid == 1701))
    pk, tgid = row.id, row.tgid
    # 99_999_999 is nobody's id and nobody's tgid: the migration must leave it alone.
    feed = Feed(name="By pk", token="t-" + "x" * 40, query={"talkgroup": [pk, 99_999_999]})
    saved = SavedSearch(name="By pk", query={"talkgroup": [pk], "q": "fire"})
    session.add_all([feed, saved])
    session.commit()
    return {"feed_id": feed.id, "saved_id": saved.id, "pk": pk, "tgid": tgid}


def _query(session: Session, table: str, row_id: int) -> dict[str, Any]:
    return session.execute(
        text(f"SELECT query FROM {table} WHERE id = :id"),  # noqa: S608
        {"id": row_id},
    ).scalar_one()


def test_upgrade_rewrites_a_stored_pk_to_the_on_air_number(session: Session, seeded) -> None:  # noqa: ANN001
    _migration()._remap(
        session.connection(),
        {r[0]: r[1] for r in session.execute(text("SELECT id, tgid FROM talkgroups"))},
    )
    session.commit()

    feed = _query(session, "feeds", seeded["feed_id"])
    saved = _query(session, "saved_searches", seeded["saved_id"])
    assert feed["talkgroup"] == [seeded["tgid"], 99_999_999]
    assert saved["talkgroup"] == [seeded["tgid"]]
    # Nothing else in the object is disturbed.
    assert saved["q"] == "fire"


def test_a_value_that_is_no_talkgroups_id_survives_a_second_run(session: Session, seeded) -> None:  # noqa: ANN001
    """Idempotence, as far as it can be had: a value matching no pk is never rewritten."""
    module = _migration()
    mapping = {r[0]: r[1] for r in session.execute(text("SELECT id, tgid FROM talkgroups"))}
    module._remap(session.connection(), mapping)
    session.commit()
    once = _query(session, "feeds", seeded["feed_id"])

    module._remap(session.connection(), mapping)
    session.commit()
    assert _query(session, "feeds", seeded["feed_id"]) == once


def test_downgrade_puts_the_primary_key_back(session: Session, seeded) -> None:  # noqa: ANN001
    module = _migration()
    rows = list(session.execute(text("SELECT id, tgid FROM talkgroups")))
    module._remap(session.connection(), {r[0]: r[1] for r in rows})
    session.commit()

    counts: dict[int, list[int]] = {}
    for row_id, tgid in rows:
        counts.setdefault(int(tgid), []).append(int(row_id))
    module._remap(session.connection(), {tgid: ids[0] for tgid, ids in counts.items() if len(ids) == 1})
    session.commit()
    assert _query(session, "feeds", seeded["feed_id"])["talkgroup"] == [
        seeded["pk"],
        99_999_999,
    ]
