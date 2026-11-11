"""Adding, editing and deleting agencies, and keeping the agency seed off them (§4.7).

The point of every test here is the same one: an operator's own agency rows and the seeded list (here
examples/agencies.example.csv) live in one table and must not be able to destroy each other.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Agency
from app.seed import seed_agencies
from tests.api.conftest import bearer
from tests.conftest import EXAMPLE_AGENCIES, requires_db

pytestmark = requires_db

NEW = {"code": "WEST_SO", "name": "West County Sheriff", "kind": "sheriff"}


def _seeded(session: Session) -> None:
    seed_agencies(session, path=EXAMPLE_AGENCIES)
    session.commit()


# ------------------------------------------------------------------------------- listing ---


def test_listing_needs_admin(client, session) -> None:
    _seeded(session)
    assert client.get("/api/admin/agencies").status_code in (401, 403)


def test_listing_shows_the_seed_with_its_counts(client, archive, admin_token) -> None:
    rows = client.get("/api/admin/agencies", headers=bearer(admin_token)).json()
    assert len(rows) == 3
    assert {r["source"] for r in rows} == {"seed"}
    fire = next(r for r in rows if r["code"] == "EXAMPLEFD")
    # The archive fixture maps one talkgroup and one radio to this agency.
    assert fire["talkgroup_count"] == 1
    assert fire["radio_count"] == 1
    assert fire["edited_at"] is None  # nobody has touched the seeded name


# -------------------------------------------------------------------------------- create ---


def test_create_makes_a_local_agency(client, session, admin_token) -> None:
    _seeded(session)
    body = client.post("/api/admin/agencies", json=NEW, headers=bearer(admin_token)).json()
    assert body["code"] == "WEST_SO"
    assert body["source"] == "local"
    assert body["active"] is True
    assert body["short_name"] == "WEST_SO"  # defaulted from the code rather than asked twice
    assert body["talkgroup_count"] == 0

    # …and it turns up on the open directory endpoint the pickers read.
    public = client.get("/api/agencies").json()
    assert any(a["code"] == "WEST_SO" and a["source"] == "local" for a in public)


def test_create_upper_cases_and_rejects_a_bad_code(client, session, admin_token) -> None:
    _seeded(session)
    lower = client.post("/api/admin/agencies", json={**NEW, "code": "west_so"}, headers=bearer(admin_token))
    assert lower.status_code == 201
    assert lower.json()["code"] == "WEST_SO"

    for bad in ("x", "way-too-long-a-code-here", "has space", "punc!"):
        response = client.post("/api/admin/agencies", json={**NEW, "code": bad}, headers=bearer(admin_token))
        assert response.status_code == 422, bad


def test_create_refuses_a_duplicate_code_and_an_unknown_kind(client, session, admin_token) -> None:
    _seeded(session)
    client.post("/api/admin/agencies", json=NEW, headers=bearer(admin_token))
    again = client.post("/api/admin/agencies", json=NEW, headers=bearer(admin_token))
    assert again.status_code == 409

    # A ECC code is just as much a duplicate as one of ours.
    collide = client.post("/api/admin/agencies", json={**NEW, "code": "ECC"}, headers=bearer(admin_token))
    assert collide.status_code == 409

    bad_kind = client.post(
        "/api/admin/agencies",
        json={**NEW, "code": "EXAMPLE_RR", "kind": "railroad"},
        headers=bearer(admin_token),
    )
    assert bad_kind.status_code == 422
    assert "agency kind" in bad_kind.json()["detail"]


def test_create_needs_admin_not_merely_write(client, session, write_token) -> None:
    _seeded(session)
    response = client.post("/api/admin/agencies", json=NEW, headers=bearer(write_token))
    assert response.status_code == 403


# --------------------------------------------------------------------------------- patch ---


def test_patch_renames_and_retires(client, session, admin_token) -> None:
    _seeded(session)
    client.post("/api/admin/agencies", json=NEW, headers=bearer(admin_token))
    body = client.patch(
        "/api/admin/agencies/WEST_SO",
        json={"name": "West County Sheriff's Office", "short_name": "WCSO", "active": False},
        headers=bearer(admin_token),
    ).json()
    assert body["name"] == "West County Sheriff's Office"
    assert body["short_name"] == "WCSO"
    assert body["active"] is False
    assert body["source"] == "local"  # editing does not change where a row came from

    assert (
        client.patch("/api/admin/agencies/NOPE", json={"name": "x"}, headers=bearer(admin_token)).status_code
        == 404
    )


def test_patch_of_a_seeded_row_stamps_it(client, session, admin_token) -> None:
    _seeded(session)
    body = client.patch(
        "/api/admin/agencies/EXAMPLEFD",
        json={"name": "Central City Fire"},
        headers=bearer(admin_token),
    ).json()
    assert body["source"] == "seed"  # still the seeded row, just readable now
    assert body["edited_at"] is not None


def test_patch_refuses_an_unknown_kind(client, session, admin_token) -> None:
    _seeded(session)
    response = client.patch(
        "/api/admin/agencies/ECC", json={"kind": "coastguard"}, headers=bearer(admin_token)
    )
    assert response.status_code == 422


# -------------------------------------------------------------------------------- delete ---


def test_delete_removes_an_unreferenced_local_agency(client, session, admin_token) -> None:
    _seeded(session)
    client.post("/api/admin/agencies", json=NEW, headers=bearer(admin_token))
    assert client.delete("/api/admin/agencies/WEST_SO", headers=bearer(admin_token)).status_code == 204
    codes = [a["code"] for a in client.get("/api/agencies").json()]
    assert "WEST_SO" not in codes


def test_delete_refuses_a_seeded_row(client, session, admin_token) -> None:
    _seeded(session)
    response = client.delete("/api/admin/agencies/ECC", headers=bearer(admin_token))
    assert response.status_code == 409
    assert "cannot be deleted" in response.json()["detail"]
    assert session.get(Agency, "ECC") is not None


def test_delete_refuses_a_local_row_something_points_at(client, archive, session, admin_token) -> None:
    from app.models import Talkgroup

    client.post("/api/admin/agencies", json=NEW, headers=bearer(admin_token))
    talkgroup = session.scalars(select(Talkgroup)).first()
    talkgroup.agency_code = "WEST_SO"
    session.commit()

    response = client.delete("/api/admin/agencies/WEST_SO", headers=bearer(admin_token))
    assert response.status_code == 409
    body = response.json()
    assert body["counts"]["talkgroup_count"] == 1
    assert body["counts"]["radio_count"] == 0
    assert "reassign" in body["detail"]
    assert session.get(Agency, "WEST_SO") is not None

    # The counts the list endpoint reports are the same ones the guard checked.
    rows = client.get("/api/admin/agencies", headers=bearer(admin_token)).json()
    assert next(r for r in rows if r["code"] == "WEST_SO")["talkgroup_count"] == 1


# ---------------------------------------------------------------------------- seed safety ---


def test_reseeding_never_touches_a_local_row(client, session, admin_token) -> None:
    _seeded(session)
    client.post("/api/admin/agencies", json=NEW, headers=bearer(admin_token))

    _seeded(session)  # the cron case: run the seed again
    row = session.get(Agency, "WEST_SO")
    assert row is not None
    assert row.source == "local"
    assert row.name == "West County Sheriff"


def test_reseeding_never_overwrites_a_local_row_that_collides(session) -> None:
    """A code an operator typed that the seeded list later issues stays the operator's."""
    session.add(
        Agency(code="ECC", name="Somebody else's ECC", short_name="X", kind="police", source="local")
    )
    session.commit()

    seed_agencies(session, path=EXAMPLE_AGENCIES, force=True)  # even forced: force is about *our* rows, not theirs
    session.commit()
    row = session.get(Agency, "ECC")
    assert row.name == "Somebody else's ECC"
    assert row.source == "local"


def test_reseeding_keeps_an_edited_name_until_forced(client, session, admin_token) -> None:
    _seeded(session)
    client.patch(
        "/api/admin/agencies/EXAMPLEFD",
        json={"name": "Central City Fire"},
        headers=bearer(admin_token),
    )

    _seeded(session)
    session.expire_all()
    assert session.get(Agency, "EXAMPLEFD").name == "Central City Fire"

    seed_agencies(session, path=EXAMPLE_AGENCIES, force=True)
    session.commit()
    session.expire_all()
    row = session.get(Agency, "EXAMPLEFD")
    assert row.name == "CENTRAL CITY FIRE DEPARTMENT"  # the shared spelling is back
    assert row.edited_at is None  # …and the row is under ordinary refresh again


def test_reseeding_still_refreshes_an_untouched_row(session) -> None:
    _seeded(session)
    row = session.get(Agency, "EXAMPLEFD")
    row.name = "drifted"
    row.edited_at = None  # as if the CSV, not an operator, had changed
    session.commit()

    _seeded(session)
    session.expire_all()
    assert session.get(Agency, "EXAMPLEFD").name == "CENTRAL CITY FIRE DEPARTMENT"


def test_the_seed_deletes_nothing(session) -> None:
    _seeded(session)
    session.add(Agency(code="EXAMPLE_RR", name="EXAMPLE_RR", short_name="XRR", kind="police", source="local"))
    session.commit()

    assert seed_agencies(session, path=EXAMPLE_AGENCIES) == 3
    session.commit()
    assert len(list(session.scalars(select(Agency)))) == 4


# ------------------------------------------------------------------------- the CLI seed ---


def test_seed_agencies_needs_a_csv_unless_told_it_is_optional(session, monkeypatch) -> None:  # noqa: ANN001
    """No packaged list: without ``--csv`` or ``KEYUP_SEED_AGENCIES`` it is an error — and
    ``--if-configured``, the compose ``migrate`` step's form, is a quiet no-op."""
    import json

    from typer.testing import CliRunner

    from app.cli import app as cli
    from app.config import get_settings

    monkeypatch.delenv("KEYUP_SEED_AGENCIES", raising=False)
    get_settings.cache_clear()
    try:
        assert CliRunner().invoke(cli, ["seed-agencies"]).exit_code == 2
        quiet = CliRunner().invoke(cli, ["seed-agencies", "--if-configured"])
        assert quiet.exit_code == 0, quiet.output
        assert json.loads(quiet.stdout)["agencies"] == 0
        assert session.scalar(select(func.count()).select_from(Agency)) == 0
    finally:
        get_settings.cache_clear()


def test_seed_agencies_reads_the_configured_csv(session, monkeypatch) -> None:  # noqa: ANN001
    import json

    from typer.testing import CliRunner

    from app.cli import app as cli
    from app.config import get_settings

    monkeypatch.setenv("KEYUP_SEED_AGENCIES", str(EXAMPLE_AGENCIES))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["seed-agencies", "--if-configured"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["agencies"] == 3
    session.expire_all()
    assert session.get(Agency, "ECC").source == "seed"

    missing = CliRunner().invoke(cli, ["seed-agencies", "--csv", "/nonexistent/agencies.csv"])
    assert missing.exit_code == 2
