"""The feed-owner and alert-rule tgid migrations: ``feeds.token_id`` and the alert-rule pk → tgid rewrite.

``09bfeda20009`` adds the column a token-made feed is owned by; ``0ebc81310604`` rewrites
the talkgroup ids stored in ``alert_rules`` from ``talkgroups.id`` to the on-air tgid, now
that the matcher reads tgids only.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import any_, inspect, select, text
from sqlalchemy.orm import Session

from tests.conftest import BACKEND_DIR, requires_db

pytestmark = requires_db

ALERTS_REVISION = "0ebc81310604"
#: The revision both of them sit on: downgrading to it undoes 0ebc81310604 then 09bfeda20009.
BEFORE = "b8e4d2a61f37"


def _migration(revision: str, slug: str) -> Any:
    """A migration module, loaded by path: ``alembic/versions`` is not a package."""
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{revision}_{slug}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alerts_migration() -> Any:
    return _migration(ALERTS_REVISION, "alert_rule_talkgroups_are_tgids")


def _maps(session: Session) -> tuple[dict[int, int], set[int]]:
    rows = list(session.execute(text("SELECT id, tgid FROM talkgroups WHERE tgid IS NOT NULL")))
    return {int(r[0]): int(r[1]) for r in rows}, {int(r[1]) for r in rows}


@pytest.fixture
def seeded(session: Session, archive) -> dict[str, Any]:  # noqa: ANN001
    """Talkgroup rules naming a pk, a tgid, a pk that is also some tgid, and a stranger."""
    from app.models import AlertRule, Talkgroup

    by_tgid = {tg.tgid: tg for tg in session.scalars(select(Talkgroup))}
    event6 = by_tgid[1701]
    # A pk that is *also* some talkgroup's tgid: make one by giving a talkgroup the tgid
    # equal to event6's pk.  Typed by a person, that number means the channel on the air.
    ambiguous_pk = event6.id
    by_tgid[1601].tgid = ambiguous_pk
    session.commit()

    def add(**kw: Any) -> int:
        row = AlertRule(name="rule", kind="talkgroup", channels=["webhook"], channel_config={}, **kw)
        session.add(row)
        session.commit()
        return row.id

    fire = session.get(Talkgroup, by_tgid[1421].id)
    rules = {
        "by_pk": add(filters={"talkgroup_ids": [fire.id, 99_999_999]}),
        "by_tgid": add(filters={"talkgroup_ids": [1701]}),
        "ambiguous": add(filters={"talkgroup_ids": [ambiguous_pk]}),
        "pattern": add(pattern=f"{fire.id}, 1701 x"),
        "talkgroups_key": add(filters={"talkgroups": fire.id}),
        "tgids_key": add(filters={"tgids": [fire.id]}),
    }
    keyword = AlertRule(name="kw", kind="keyword", pattern=str(fire.id), channels=[], channel_config={})
    session.add(keyword)
    session.commit()
    return {"rules": rules, "keyword": keyword.id, "fire_pk": fire.id, "fire_tgid": fire.tgid,
            "ambiguous": ambiguous_pk}


def _rule(session: Session, rule_id: int) -> tuple[str | None, dict[str, Any] | None]:
    row = session.execute(
        text("SELECT pattern, filters FROM alert_rules WHERE id = :id"), {"id": rule_id}
    ).one()
    return row[0], row[1]


def test_upgrade_rewrites_stored_pks_to_on_air_numbers(session: Session, seeded) -> None:  # noqa: ANN001
    pk_to_tgid, tgids = _maps(session)
    changed = _alerts_migration().rewrite(session.connection(), pk_to_tgid, tgids)
    session.commit()
    rules, fire_tgid = seeded["rules"], seeded["fire_tgid"]

    assert changed == 3  # by_pk, pattern, talkgroups_key
    assert _rule(session, rules["by_pk"])[1] == {"talkgroup_ids": [fire_tgid, 99_999_999]}
    assert _rule(session, rules["by_tgid"])[1] == {"talkgroup_ids": [1701]}
    # A number that is a tgid is read as one, even though it is also a pk.
    assert _rule(session, rules["ambiguous"])[1] == {"talkgroup_ids": [seeded["ambiguous"]]}
    # Separators and words in a typed pattern survive; only the pk token changes.
    assert _rule(session, rules["pattern"])[0] == f"{fire_tgid}, 1701 x"
    assert _rule(session, rules["talkgroups_key"])[1] == {"talkgroups": fire_tgid}
    # ``tgids`` already says what it holds, and a keyword rule's pattern is a word.
    assert _rule(session, rules["tgids_key"])[1] == {"tgids": [seeded["fire_pk"]]}
    assert _rule(session, seeded["keyword"])[0] == str(seeded["fire_pk"])


def test_the_rewrite_is_a_fixed_point(session: Session, seeded) -> None:  # noqa: ANN001
    module = _alerts_migration()
    pk_to_tgid, tgids = _maps(session)
    module.rewrite(session.connection(), pk_to_tgid, tgids)
    session.commit()
    once = {rid: _rule(session, rid) for rid in seeded["rules"].values()}
    assert module.rewrite(session.connection(), pk_to_tgid, tgids) == 0
    session.commit()
    assert {rid: _rule(session, rid) for rid in seeded["rules"].values()} == once


def test_a_rewritten_rule_fires_on_the_channel_it_used_to(session: Session, seeded, settings) -> None:  # noqa: ANN001
    """End to end: the pk rule stopped matching under the tgid-only matcher, and the
    migration brings it back to the channel its author picked."""
    from app.alerts import matches, summarize
    from app.models import AlertRule, Call

    call = session.scalar(select(Call).where(any_(Call.talkgroup_ids) == seeded["fire_tgid"]))
    summary = summarize(session, call.id, settings)
    by_pk = session.get(AlertRule, seeded["rules"]["by_pk"])
    assert not matches(by_pk, call, summary)

    pk_to_tgid, tgids = _maps(session)
    _alerts_migration().rewrite(session.connection(), pk_to_tgid, tgids)
    session.commit()
    session.refresh(by_pk)
    assert matches(by_pk, call, summary)


def test_both_migrations_go_down_and_up(migrated_database) -> None:  # noqa: ANN001, ARG001
    """``alembic downgrade`` past both and back to head, on the real schema."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command
    from app.db import engine

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    def feed_columns() -> set[str]:
        engine.dispose()
        return {c["name"] for c in inspect(engine).get_columns("feeds")}

    assert "token_id" in feed_columns()
    command.downgrade(config, BEFORE)
    try:
        assert "token_id" not in feed_columns()
    finally:
        command.upgrade(config, "head")
    assert "token_id" in feed_columns()
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert current == ScriptDirectory.from_config(config).get_current_head()
