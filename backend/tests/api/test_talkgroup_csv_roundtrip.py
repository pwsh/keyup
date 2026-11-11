"""The "five-minute path": `/talkgroups/export` → edit in a spreadsheet → `/talkgroups/import`.

This is how an operator classifies forty channels without a rules file and without forty
`PATCH` requests, and it is only useful if it is *lossless* for the columns being edited.
The three that matter to a CAD consumer are `category`, `agency_code` and `service_tag`; `group_name`
is here too because a spreadsheet round trip that silently blanks a column is worse than one
that refuses.
"""

from __future__ import annotations

import csv
import io

from sqlalchemy import select

from app.models import Talkgroup
from tests.api.conftest import CSRF_HEADERS, bearer
from tests.conftest import requires_db

pytestmark = requires_db


def _export(client) -> list[dict[str, str]]:  # noqa: ANN001
    response = client.get("/api/talkgroups/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(response.text)))


def _import(client, token: str, rows: list[dict[str, str]], header: list[str]):  # noqa: ANN001, ANN202
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header)
    writer.writeheader()
    writer.writerows(rows)
    return client.post(
        "/api/talkgroups/import",
        json={"content": buffer.getvalue(), "format": "csv"},
        headers={**bearer(token), **CSRF_HEADERS},
    )


def test_export_carries_the_columns_the_importer_reads(client, archive) -> None:  # noqa: ANN001
    rows = _export(client)
    assert rows
    for column in ("tgid", "alias", "group_name", "category", "service_tag", "agency_code"):
        assert column in rows[0]


def test_a_round_trip_keeps_category_agency_and_service_tag(client, session, archive, write_token) -> None:  # noqa: ANN001
    """Export, classify a channel in the spreadsheet, import: the edits land and nothing else moves."""
    rows = _export(client)
    header = list(rows[0])
    target = next(row for row in rows if row["tgid"] == "1601")
    target.update(
        {
            "category": "law",
            "service_tag": "Law Dispatch",
            "agency_code": "EXAMPLEPD",
            "group_name": "Law",
        }
    )

    response = _import(client, write_token, rows, header)
    assert response.status_code == 200, response.text
    assert response.json()["skipped_service_tags"] == 0

    session.expire_all()
    tg = session.scalars(select(Talkgroup).where(Talkgroup.tgid == 1601)).one()
    assert (tg.category, tg.service_tag, tg.agency_code) == ("law", "Law Dispatch", "EXAMPLEPD")
    assert tg.group_name == "Law"

    # And the channel that was already classified by the fixture is untouched.
    fire = session.scalars(select(Talkgroup).where(Talkgroup.tgid == 1701)).one()
    assert (fire.category, fire.service_tag, fire.agency_code) == ("fire", "Fire Dispatch", "EXAMPLEFD")


def test_re_importing_an_unedited_export_changes_nothing(client, session, archive, write_token) -> None:  # noqa: ANN001
    """The property that makes the path safe to repeat: export → import is the identity."""
    before = {
        tg.tgid: (tg.alias, tg.group_name, tg.category, tg.service_tag, tg.agency_code)
        for tg in session.scalars(select(Talkgroup))
    }
    rows = _export(client)
    assert _import(client, write_token, rows, list(rows[0])).status_code == 200

    session.expire_all()
    after = {
        tg.tgid: (tg.alias, tg.group_name, tg.category, tg.service_tag, tg.agency_code)
        for tg in session.scalars(select(Talkgroup))
    }
    assert after == before


def test_a_typo_in_a_cell_costs_that_cell_and_not_the_file(client, session, archive, write_token) -> None:  # noqa: ANN001
    """One bad cell in a thousand-row directory must not fail the other 999."""
    rows = _export(client)
    header = list(rows[0])
    target = next(row for row in rows if row["tgid"] == "1601")
    target.update({"category": "law", "service_tag": "Fire Brigade", "agency_code": "NOSUCHCODE"})

    response = _import(client, write_token, rows, header)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped_service_tags"] == 1
    assert body["skipped_agency_codes"] == 1

    session.expire_all()
    tg = session.scalars(select(Talkgroup).where(Talkgroup.tgid == 1601)).one()
    # The good cell landed; the two bad ones were dropped rather than written or raised.
    assert tg.category == "law"
    assert tg.service_tag is None
    assert tg.agency_code is None


def test_the_recorders_own_spelling_of_the_tag_column_still_works(client, session, archive, write_token) -> None:  # noqa: ANN001
    """trunk-recorder calls it `tag`, so a directory exported from one imports unedited."""
    response = _import(
        client,
        write_token,
        [{"tgid": "1601", "alias": "EXAMPLE LAW 1", "tag": "Law Tac", "category": "law"}],
        ["tgid", "alias", "tag", "category"],
    )
    assert response.status_code == 200, response.text
    session.expire_all()
    tg = session.scalars(select(Talkgroup).where(Talkgroup.tgid == 1601)).one()
    assert tg.service_tag == "Law Tac"


def test_the_export_flags_round_trip(client, session, archive, write_token) -> None:  # noqa: ANN001, ARG001
    """Clearing a checkbox in the spreadsheet lands; a file without the columns leaves it alone."""
    rows = _export(client)
    header = list(rows[0])
    assert "export_broadcastify" in header
    assert "export_openmhz" in header

    target = next(row for row in rows if row["tgid"] == "1601")
    target["export_broadcastify"] = "False"
    assert _import(client, write_token, rows, header).status_code == 200

    tg = session.scalar(select(Talkgroup).where(Talkgroup.tgid == 1601))
    session.refresh(tg)
    assert tg.export_broadcastify is False
    assert tg.export_openmhz is True

    # A directory exported before these columns existed must not retick the box.
    old_header = [column for column in header if not column.startswith("export_")]
    old_rows = [{key: row[key] for key in old_header} for row in rows]
    assert _import(client, write_token, old_rows, old_header).status_code == 200
    session.refresh(tg)
    assert tg.export_broadcastify is False
