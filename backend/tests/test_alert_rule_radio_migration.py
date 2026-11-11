"""The alert-rule radio migration: radio alert rules' stored row ids become on-air radio numbers.

``3216a949614d`` does for ``kind = 'radio'`` what ``0ebc81310604`` did for talkgroups,
now that :func:`app.alerts.matches` reads a radio rule's ids as on-air numbers only.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.conftest import BACKEND_DIR, requires_db

pytestmark = requires_db

REVISION = "3216a949614d"


def _migration() -> Any:
    """The migration module, loaded by path: ``alembic/versions`` is not a package."""
    path = BACKEND_DIR / "alembic" / "versions" / f"{REVISION}_alert_rule_radios_are_numbers.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", Path(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _maps(session: Session) -> tuple[dict[int, int], set[int]]:
    rows = list(session.execute(text("SELECT id, radio_id FROM radios")))
    return {int(r[0]): int(r[1]) for r in rows}, {int(r[1]) for r in rows}


def _rule(session: Session, rule_id: int) -> tuple[str | None, dict[str, Any] | None]:
    row = session.execute(
        text("SELECT pattern, filters FROM alert_rules WHERE id = :id"), {"id": rule_id}
    ).one()
    return row[0], row[1]


@pytest.fixture
def seeded(session: Session) -> dict[str, Any]:
    """Radio rules naming a row id, a number, a row id that is also a number, and a stranger."""
    from app.models import AlertRule, Radio, System

    system = System(name="Example System")
    session.add(system)
    session.flush()
    engine1 = Radio(system_id=system.id, radio_id=5200007, unit_id="E1")
    medic = Radio(system_id=system.id, radio_id=5200102, unit_id="M1")
    session.add_all([engine1, medic])
    session.flush()
    # A radio whose on-air number is engine1's row id: typed by a person, that number
    # means this radio, not engine1's row.
    ambiguous = Radio(system_id=system.id, radio_id=engine1.id, unit_id="X")
    session.add(ambiguous)
    session.commit()

    def add(kind: str = "radio", **kw: Any) -> int:
        row = AlertRule(name="rule", kind=kind, channels=["webhook"], channel_config={}, **kw)
        session.add(row)
        session.commit()
        return row.id

    rules = {
        "by_pk": add(filters={"radio_ids": [medic.id, 99_999_999]}),
        "by_number": add(filters={"radio_ids": [5200007]}),
        "ambiguous": add(filters={"radio_ids": [engine1.id]}),
        "pattern": add(pattern=f"{medic.id}, 5200007 x"),
        "radios_key": add(filters={"radios": medic.id}),
        "talkgroup_rule": add(kind="talkgroup", filters={"talkgroup_ids": [medic.id]}),
    }
    return {"rules": rules, "medic_pk": medic.id, "engine1_pk": engine1.id}


def test_upgrade_rewrites_stored_row_ids_to_radio_numbers(session: Session, seeded) -> None:  # noqa: ANN001
    changed = _migration().rewrite(session.connection(), *_maps(session))
    session.commit()
    rules = seeded["rules"]

    assert changed == 3  # by_pk, pattern, radios_key
    assert _rule(session, rules["by_pk"])[1] == {"radio_ids": [5200102, 99_999_999]}
    assert _rule(session, rules["by_number"])[1] == {"radio_ids": [5200007]}
    # A number that is some radio's number is read as one, even though it is also a row id.
    assert _rule(session, rules["ambiguous"])[1] == {"radio_ids": [seeded["engine1_pk"]]}
    assert _rule(session, rules["pattern"])[0] == "5200102, 5200007 x"
    assert _rule(session, rules["radios_key"])[1] == {"radios": 5200102}
    # Only radio rules: a talkgroup rule's ids are the talkgroup migration's business.
    assert _rule(session, rules["talkgroup_rule"])[1] == {"talkgroup_ids": [seeded["medic_pk"]]}


def test_the_rewrite_is_a_fixed_point(session: Session, seeded) -> None:  # noqa: ANN001
    module = _migration()
    module.rewrite(session.connection(), *_maps(session))
    session.commit()
    once = {rid: _rule(session, rid) for rid in seeded["rules"].values()}
    assert module.rewrite(session.connection(), *_maps(session)) == 0
    session.commit()
    assert {rid: _rule(session, rid) for rid in seeded["rules"].values()} == once
