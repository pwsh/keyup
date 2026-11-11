"""``keyup sites name-message-kinds`` — giving the stored message kinds their names.

Central shipped with 267 message kinds and 0 of them named, which makes "unknown" say
nothing: most of them are standard TIA-102.AABC opcodes that trunkd prints by name in its
own logs.  These tests are about the three things the naming has to get right — it names
what the tables name, it leaves a *noise* entry explained but unknown, and running it
twice changes nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from app.cli import app
from app.models import Site, SiteMessageKind, System
from app.p25.message_names import BUILTIN_NAMES, is_noise, label_for, load_curated
from tests.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: Enough of trunkd's ``docs/identifiers.toml`` to exercise the curated half.
CURATED = """
[tsbk]
"0:22" = { name = "SNDCP_DCH_ANN_EX", meaning = "…", confidence = "probable" }
"228:58" = { name = "Mangled NET_STATUS_BCAST (noise)", confidence = "probable" }
"164:9" = { name = "L3Harris something", confidence = "probable" }
"nonsense" = { name = "not a key" }
"0:7" = { meaning = "no name at all" }
"""


@pytest.fixture
def site(session):  # noqa: ANN001, ANN201
    system = System(name="Example System", system_id=52, nac=52, wacn=101_010)
    session.add(system)
    session.flush()
    row = Site(system_id=system.id, name="Central", rfss=10, site_number=1)
    session.add(row)
    session.commit()
    return row


def kind(session, site, mfid: int, opcode: int, **overrides) -> SiteMessageKind:  # noqa: ANN001, ANN003
    row = SiteMessageKind(
        site_id=site.id,
        mfid=mfid,
        opcode=opcode,
        first_seen_at=NOW,
        last_seen_at=NOW,
        count=overrides.pop("count", 100),
        known=overrides.pop("known", False),
        label=overrides.pop("label", None),
    )
    session.add(row)
    session.commit()
    return row


def rows(session, site) -> dict[tuple[int, int], SiteMessageKind]:  # noqa: ANN001
    session.expire_all()
    from sqlalchemy import select

    return {
        (row.mfid, row.opcode): row
        for row in session.scalars(select(SiteMessageKind).where(SiteMessageKind.site_id == site.id))
    }


# --------------------------------------------------------------------------------------
# the table itself
# --------------------------------------------------------------------------------------


def test_the_builtin_table_is_the_64_standard_opcodes_plus_the_harris_ones() -> None:
    assert len([key for key in BUILTIN_NAMES if key[0] == 0]) == 64
    # Spelled the way trunkd spells them, so a name in the Keyup UI matches the recorder's log.
    assert BUILTIN_NAMES[(0, 0)] == "GRP_VCH_GRANT"
    assert BUILTIN_NAMES[(0, 61)] == "IDEN_UPDATE"
    assert BUILTIN_NAMES[(0, 56)] == "SYS_SVC_BCAST"
    assert BUILTIN_NAMES[(164, 48)] == "HARRIS REGROUP"
    assert (164, 34) not in BUILTIN_NAMES  # the beacon nobody has decoded stays unnamed
    assert label_for(0, 2) == ("GRP_VCH_GRNT_UPD", True)
    assert label_for(164, 34) == (None, False)
    assert is_noise("CRC false pass (noise)") and not is_noise("IDEN_UPDATE")


def test_a_curated_toml_is_read_and_a_broken_entry_is_skipped(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "identifiers.toml"
    path.write_text(CURATED)

    curated = load_curated(path)

    assert curated[(0, 22)] == "SNDCP_DCH_ANN_EX"
    assert curated[(164, 9)] == "L3Harris something"
    assert (0, 7) not in curated  # an entry with no name names nothing
    assert len(curated) == 3  # "nonsense" is not a mfid:opcode


# --------------------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------------------


def test_the_cli_names_the_standard_opcodes_and_counts_them(session, site) -> None:  # noqa: ANN001
    kind(session, site, 0, 2)
    kind(session, site, 0, 61)
    kind(session, site, 164, 34)  # nothing names this one

    result = CliRunner().invoke(app, ["sites", "name-message-kinds"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["rows"] == 3
    assert body["named"] == 2
    assert body["known"] == 2
    assert body["changed"] == 2
    assert body["per_site"]["Central"] == {"rows": 3, "named": 2, "known": 2, "changed": 2}
    stored = rows(session, site)
    assert (stored[(0, 2)].label, stored[(0, 2)].known) == ("GRP_VCH_GRNT_UPD", True)
    assert (stored[(0, 61)].label, stored[(0, 61)].known) == ("IDEN_UPDATE", True)
    assert (stored[(164, 34)].label, stored[(164, 34)].known) == (None, False)


def test_a_noise_entry_is_labelled_and_stays_unknown(session, site, tmp_path) -> None:  # noqa: ANN001
    """A CRC false pass is explained, not known: it is not a message the site transmits."""
    kind(session, site, 228, 58, count=1)
    kind(session, site, 0, 22)
    path = tmp_path / "identifiers.toml"
    path.write_text(CURATED)

    result = CliRunner().invoke(app, ["sites", "name-message-kinds", "--toml", str(path)])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["named"] == 2
    assert body["known"] == 1
    stored = rows(session, site)
    assert stored[(228, 58)].label == "Mangled NET_STATUS_BCAST (noise)"
    assert stored[(228, 58)].known is False
    assert (stored[(0, 22)].label, stored[(0, 22)].known) == ("SNDCP_DCH_ANN_EX", True)


def test_the_run_is_idempotent_and_dry_run_writes_nothing(session, site) -> None:  # noqa: ANN001
    kind(session, site, 0, 2)

    dry = json.loads(CliRunner().invoke(app, ["sites", "name-message-kinds", "--dry-run"]).stdout)
    assert dry == {
        "rows": 1,
        "named": 1,
        "known": 1,
        "changed": 1,
        "dry_run": True,
        "per_site": {"Central": {"rows": 1, "named": 1, "known": 1, "changed": 1}},
    }
    assert rows(session, site)[(0, 2)].label is None  # …and wrote nothing

    first = json.loads(CliRunner().invoke(app, ["sites", "name-message-kinds"]).stdout)
    second = json.loads(CliRunner().invoke(app, ["sites", "name-message-kinds"]).stdout)

    assert first["changed"] == 1
    assert second["changed"] == 0  # the second run has nothing left to do
    assert second["named"] == second["known"] == 1


def test_a_hand_written_label_is_left_alone(session, site) -> None:  # noqa: ANN001
    """An operator's label is a finding about this site; the table only knows the protocol."""
    kind(session, site, 164, 48, known=True, label="Harris patch, ECC dispatch")

    CliRunner().invoke(app, ["sites", "name-message-kinds"])

    assert rows(session, site)[(164, 48)].label == "Harris patch, ECC dispatch"


def test_reserved_and_alternate_mfid_names() -> None:
    """A reserved slot is named like the registry but never counted as known; mfid 1 mirrors 0."""
    from app.p25.message_names import builtin_name, is_noise

    assert builtin_name(0, 1) == "RESERVED_01"
    assert is_noise(builtin_name(0, 1))
    assert not is_noise(builtin_name(0, 0))
    assert builtin_name(1, 0) == builtin_name(0, 0) == "GRP_VCH_GRANT"
