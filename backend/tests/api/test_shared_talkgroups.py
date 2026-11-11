"""``talkgroups.shared``: the shared channels no single agency owns.

The problem this solves is one an ``agency_code`` cannot: on the Example System the dispatch
and interop channels — EC LAW 1-3, FIRE DISPATCH, the EVENT channels — belong to every
department and therefore to none, so they carry no ``agency_code`` at all.  ``?agency=``
then matches an agency's *own* channels and misses the one its calls actually go out on.
A CAD consumer would otherwise hardcode the tgid list in its own config; these tests pin
the API that replaces that list — the flag, the ``include_shared`` widening, and the
``shared_talkgroup_ids`` every agency carries so the set is discoverable at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.api.conftest import bearer
from tests.conftest import requires_db

pytestmark = requires_db

#: The fixture archive maps EXAMPLE EVENT 1 to CCFD and leaves EXAMPLE LAW 1 owned by nobody —
#: which is exactly the shape the real problem has: an agency has a channel of its own, and
#: the interop channel its calls also land on belongs to no department at all.
OWNED_AGENCY = "EXAMPLEFD"
OWNED_TGID = 1701
SHARED_TGID = 1601


@pytest.fixture
def shared_channel(session, archive):  # noqa: ANN001, ANN201
    """EXAMPLE LAW 1 flagged shared and mapped to ECC, the way ``mark-shared`` leaves it."""
    from app.models import Talkgroup

    row = session.scalar(select(Talkgroup).where(Talkgroup.tgid == SHARED_TGID))
    row.shared = True
    row.agency_code = "ECC"
    session.commit()
    return row.id


# --------------------------------------------------------------------------------------
# the flag on the API
# --------------------------------------------------------------------------------------


def test_shared_is_readable_and_writable_through_the_talkgroup_endpoints(
    client, archive, write_token
) -> None:
    listed = client.get("/api/talkgroups").json()["items"]
    assert all(item["shared"] is False for item in listed)
    target = next(item for item in listed if item["tgid"] == SHARED_TGID)

    patched = client.patch(
        f"/api/talkgroups/{target['id']}", json={"shared": True}, headers=bearer(write_token)
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["shared"] is True

    assert client.get(f"/api/talkgroups/{target['id']}").json()["shared"] is True
    # …and an omitted key still means "leave it alone", not "set it false".
    client.patch(
        f"/api/talkgroups/{target['id']}", json={"notes": "county wide"}, headers=bearer(write_token)
    )
    assert client.get(f"/api/talkgroups/{target['id']}").json()["shared"] is True


def test_shared_can_be_set_on_a_selection_at_once(client, archive, write_token) -> None:
    ids = [item["id"] for item in client.get("/api/talkgroups").json()["items"][:2]]
    response = client.post(
        "/api/talkgroups/bulk",
        json={"ids": ids, "changes": {"shared": True}},
        headers=bearer(write_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == len(ids)
    assert all(item["shared"] is True for item in body["items"])


# --------------------------------------------------------------------------------------
# include_shared
# --------------------------------------------------------------------------------------


def test_include_shared_widens_agency_on_the_directory(client, shared_channel) -> None:
    owned = client.get("/api/talkgroups", params={"agency": OWNED_AGENCY}).json()
    assert SHARED_TGID not in {t["tgid"] for t in owned["items"]}

    widened = client.get(
        "/api/talkgroups", params={"agency": OWNED_AGENCY, "include_shared": True}
    ).json()
    tgids = {t["tgid"] for t in widened["items"]}
    assert SHARED_TGID in tgids
    assert tgids > {t["tgid"] for t in owned["items"]}
    # The widening is opt-in, so the total the header reports moves with it.
    assert widened["total"] > owned["total"]


def test_include_shared_widens_agency_on_calls(client, archive, shared_channel) -> None:
    owned = client.get("/api/calls", params={"agency": OWNED_AGENCY}).json()
    widened = client.get(
        "/api/calls", params={"agency": OWNED_AGENCY, "include_shared": True}
    ).json()
    shared_only = client.get("/api/calls", params={"talkgroup": SHARED_TGID}).json()

    assert shared_only["total"] > 0
    assert widened["total"] == owned["total"] + shared_only["total"]

    owned_ids = {c["id"] for c in owned["items"]}
    assert owned_ids < {c["id"] for c in widened["items"]}
    # Without an agency it changes nothing: "shared" is not a set anyone asked to see alone.
    assert (
        client.get("/api/calls", params={"include_shared": True}).json()["total"]
        == client.get("/api/calls").json()["total"]
    )


# --------------------------------------------------------------------------------------
# discovery: /api/agencies
# --------------------------------------------------------------------------------------


def test_every_agency_carries_the_shared_set_so_a_consumer_can_discover_it(
    client, shared_channel
) -> None:
    agencies = client.get("/api/agencies").json()
    assert agencies

    # The same list on every agency: it is the set, not a possession.
    assert {tuple(a["shared_talkgroup_ids"]) for a in agencies} == {(SHARED_TGID,)}

    dispatch = next(a for a in agencies if a["code"] == "ECC")
    # ECC owns them in the ordinary sense too, so they are its own talkgroups as well —
    # and `talkgroup_ids` is tgids while `talkgroups` is row PKs, which is the pairing
    # a consumer reads defensively until it is pinned.
    assert dispatch["talkgroup_ids"] == [SHARED_TGID]
    assert dispatch["talkgroups"] == [shared_channel]
    assert dispatch["talkgroups"] != dispatch["talkgroup_ids"]


# --------------------------------------------------------------------------------------
# the one-off CLI
# --------------------------------------------------------------------------------------


def test_the_cli_marks_a_named_list_and_says_what_it_did(session, archive) -> None:
    """``mark-shared`` flags a named list and fills the blank owners with ``--agency``."""
    import json

    from typer.testing import CliRunner

    from app.cli import app as cli
    from app.models import Talkgroup

    result = CliRunner().invoke(
        cli,
        [
            "mark-shared",
            "--system",
            "Example System",
            "--agency",
            "ECC",
            str(SHARED_TGID),
            str(OWNED_TGID),
            "59999",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["matched"] == [SHARED_TGID, OWNED_TGID]
    assert body["missing"] == [59999]  # not created: the archive names channels, not the CLI
    assert body["newly_shared"] == [SHARED_TGID, OWNED_TGID]
    assert body["agency_filled"] == [SHARED_TGID]
    assert body["agency_kept"] == [{"tgid": OWNED_TGID, "agency_code": OWNED_AGENCY}]

    session.expire_all()
    assert session.scalar(select(Talkgroup).where(Talkgroup.tgid == SHARED_TGID)).agency_code == "ECC"
    assert (
        session.scalar(select(Talkgroup).where(Talkgroup.tgid == OWNED_TGID)).agency_code
        == OWNED_AGENCY
    )

    # Idempotent: the second run finds nothing left to change.
    again = json.loads(
        CliRunner()
        .invoke(cli, ["mark-shared", "--agency", "ECC", str(SHARED_TGID), str(OWNED_TGID)])
        .stdout
    )
    assert again["newly_shared"] == []
    assert again["agency_filled"] == []


def test_the_cli_scopes_to_the_system_it_was_given(session, archive) -> None:
    """Two systems can both have a 1701; ``--system`` is how you mean one of them."""
    import json

    from typer.testing import CliRunner

    from app.cli import app as cli
    from app.models import System, Talkgroup

    other = System(name="Some Other Network")
    session.add(other)
    session.flush()
    session.add(Talkgroup(system_id=other.id, tgid=SHARED_TGID))
    session.commit()

    body = json.loads(
        CliRunner()
        .invoke(cli, ["mark-shared", "--system", "Example System", str(SHARED_TGID)])
        .stdout
    )
    assert len(body["newly_shared"]) == 1

    session.expire_all()
    rows = {
        t.system_id: t.shared
        for t in session.scalars(select(Talkgroup).where(Talkgroup.tgid == SHARED_TGID))
    }
    assert rows[other.id] is False
    assert True in rows.values()


def test_without_an_agency_the_cli_only_flags(session, archive) -> None:  # noqa: ANN001, ARG001
    """``shared_agency_code`` is blank by default: nothing names an owner, so none is set."""
    import json

    from typer.testing import CliRunner

    from app import runtime_settings
    from app.cli import app as cli
    from app.models import Talkgroup

    runtime_settings.invalidate()
    body = json.loads(CliRunner().invoke(cli, ["mark-shared", str(SHARED_TGID)]).stdout)
    assert body["newly_shared"] == [SHARED_TGID]
    assert body["agency_filled"] == []
    assert body["agency_code"] is None
    assert body["agency_missing"] is None
    session.expire_all()
    row = session.scalar(select(Talkgroup).where(Talkgroup.tgid == SHARED_TGID))
    assert (row.shared, row.agency_code) == (True, None)


def test_the_cli_reads_the_tgids_and_the_agency_setting(session, archive, tmp_path) -> None:  # noqa: ANN001, ARG001
    """``--file`` is the deployment's list; ``shared_agency_code`` is its dispatch center."""
    import json

    from typer.testing import CliRunner

    from app import runtime_settings
    from app.cli import app as cli
    from app.models import Setting, Talkgroup

    session.add(Setting(key="shared_agency_code", value="ECC"))
    session.commit()
    runtime_settings.invalidate()
    tgids = tmp_path / "shared-talkgroups.txt"
    tgids.write_text(f"# the shared channels\n{SHARED_TGID}  # EXAMPLE LAW 1\n\n59999, 59998\n")
    try:
        result = CliRunner().invoke(cli, ["mark-shared", "--file", str(tgids)])
    finally:
        runtime_settings.invalidate()
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["requested"] == [SHARED_TGID, 59998, 59999]
    assert body["agency_filled"] == [SHARED_TGID]
    assert body["agency_code"] == "ECC"
    session.expire_all()
    assert session.scalar(select(Talkgroup).where(Talkgroup.tgid == SHARED_TGID)).agency_code == "ECC"


def test_an_unknown_agency_is_reported_not_applied(session, archive) -> None:  # noqa: ANN001, ARG001
    import json

    from typer.testing import CliRunner

    from app.cli import app as cli

    body = json.loads(CliRunner().invoke(cli, ["mark-shared", "--agency", "NOPE", str(SHARED_TGID)]).stdout)
    assert body["newly_shared"] == [SHARED_TGID]
    assert body["agency_filled"] == []
    assert body["agency_missing"] == "NOPE"


def test_the_cli_needs_some_tgids(session, tmp_path) -> None:  # noqa: ANN001, ARG001
    from typer.testing import CliRunner

    from app.cli import app as cli

    assert CliRunner().invoke(cli, ["mark-shared"]).exit_code == 2
    bad = tmp_path / "bad.txt"
    bad.write_text("1601 twelve\n")
    assert CliRunner().invoke(cli, ["mark-shared", "--file", str(bad)]).exit_code == 2
