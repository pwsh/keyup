"""``3506d783396e``: the seeded agencies' provenance is relabelled ``seed``.

An existing ``agencies`` table holds the seeded rows under an older, installation-specific
label and a few ``local`` ones.  The upgrade must relabel exactly the rows whose source is not
a current value, leave every other column alone (an operator's ``edited_at`` stamp is what
keeps the seed off a renamed row), and leave the next ``keyup seed-agencies`` with the same
rows to refresh as before.  The downgrade cannot know the old label and changes nothing.  The seed CSV is the deployment's own when this checkout has one
(``deploy/site/agencies.csv``), else the example list.
"""

from __future__ import annotations

import csv
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.seed import seed_agencies
from tests.conftest import BACKEND_DIR, EXAMPLE_AGENCIES, requires_db

pytestmark = requires_db

REVISION = "3506d783396e"
#: Stands in for the old label: the upgrade keys on "not a current value", not on a name.
LEGACY = "old-install"
SITE_AGENCIES = BACKEND_DIR.parent / "deploy" / "site" / "agencies.csv"
CSV = SITE_AGENCIES if SITE_AGENCIES.is_file() else EXAMPLE_AGENCIES


def _migration() -> Any:
    path = BACKEND_DIR / "alembic" / "versions" / f"{REVISION}_agency_source_seed.py"
    spec = importlib.util.spec_from_file_location(f"migration_{REVISION}", Path(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(session: Session, step: str) -> None:
    context = MigrationContext.configure(session.connection())
    with Operations.context(context):
        getattr(_migration(), step)()
    session.commit()


def _rows(session: Session) -> dict[str, tuple[str, str, bool, Any]]:
    return {
        r[0]: (r[1], r[2], r[3], r[4])
        for r in session.execute(text("SELECT code, source, name, active, edited_at FROM agencies"))
    }


def _as_production(session: Session) -> None:
    """The seeded rows under the legacy label, one renamed and stamped, plus two local rows."""
    with CSV.open(newline="") as handle:
        for row in csv.DictReader(handle):
            session.execute(
                text(
                    "INSERT INTO agencies (code, name, short_name, kind, source, active) "
                    "VALUES (:code, :name, :short_name, :kind, :source, true)"
                ),
                {**{k: row[k].strip() for k in ("code", "name", "short_name", "kind")}, "source": LEGACY},
            )
    first = next(iter(_rows(session)))
    session.execute(
        text("UPDATE agencies SET name = 'Renamed here', edited_at = :at WHERE code = :code"),
        {"at": datetime(2026, 10, 1, tzinfo=UTC), "code": first},
    )
    session.execute(
        text(
            "INSERT INTO agencies (code, name, short_name, kind, source, active) VALUES "
            "('NORTH_SO', 'North County Sheriff', 'NCSO', 'sheriff', 'local', true), "
            "('OLD_PD', 'Old Town PD', 'OTPD', 'police', 'local', false)"
        )
    )
    session.commit()


def test_upgrade_relabels_only_the_seeded_rows_and_downgrade_keeps_them(session: Session) -> None:
    _as_production(session)
    before = _rows(session)

    _run(session, "upgrade")
    after = _rows(session)
    assert {source for source, *_ in after.values()} == {"seed", "local"}
    for code, (source, *rest) in before.items():
        assert after[code][1:] == tuple(rest), code  # name, active, edited_at untouched
        assert after[code][0] == ("seed" if source == LEGACY else "local"), code

    # The seed now owns exactly the rows it owned before: it refreshes, adds nothing, and
    # still leaves the renamed row's name alone.
    seeded = sum(1 for source, *_ in before.values() if source == LEGACY)
    assert seed_agencies(session, path=CSV) == seeded
    session.commit()
    refreshed = _rows(session)
    assert set(refreshed) == set(before)
    renamed = next(code for code, (_, name, *_) in before.items() if name == "Renamed here")
    assert refreshed[renamed][1] == "Renamed here"

    # Downgrade is a documented no-op: the legacy label is gone, every row stays as it is.
    upgraded = _rows(session)
    _run(session, "downgrade")
    assert _rows(session) == upgraded


def test_upgrade_on_an_empty_table_changes_nothing(session: Session) -> None:
    _run(session, "upgrade")
    assert _rows(session) == {}
