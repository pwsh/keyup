"""Unit-to-unit (private) calls through the API — the row, the filter, the radio, the stats.

A private call has no talkgroup, so every place the API answers "which channel was this"
has to have something else to say.  These tests are that: the row carries `kind` and both
radios, `?kind=` narrows to one or the other, a radio's calls include the ones it was
*called on* and not only the ones it started, and the dashboard counts them separately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.conftest import requires_db

pytestmark = requires_db

#: An hour ago, so the dashboard's 24-hour window sees it — the one thing about this
#: fixture that has to be relative rather than a fixed instant.
BASE = datetime.now(UTC) - timedelta(hours=1)
CALLER, CALLEE = 5238001, 5238109


@pytest.fixture
def private_call(session, archive):  # noqa: ANN001, ANN201
    """One private call between two radios, beside the archive's ordinary traffic."""
    from app.ingest.service import upsert_radio
    from app.models import Call

    system_id = session.scalars(select(Call.system_id)).first()
    caller = upsert_radio(
        session, system_id, radio_id=CALLER, wacn=0, home_system=0, seen_at=BASE
    )
    callee = upsert_radio(
        session, system_id, radio_id=CALLEE, wacn=0, home_system=0, seen_at=BASE
    )
    call = Call(
        system_id=system_id,
        kind="unit",
        call_type="individual",
        talkgroup_id=None,
        talkgroup_ids=[],
        radio_id=caller,
        radio_raw=str(CALLER),
        target_radio_id=callee,
        ingest_source="trunkd",
        recorded_at=BASE,
        started_at=BASE,
        started_local=BASE.replace(tzinfo=None),
        ended_at=BASE + timedelta(seconds=6),
        time_source="upload",
        duration_s=Decimal("6.000"),
        frequency_hz=852162500,
        audio_state="present",
        transcript_status="pending",
        file_path="uploads/2026/08/29/aa/private.mp3",
        file_name="private.mp3",
    )
    session.add(call)
    session.commit()
    return call


def test_the_row_carries_the_kind_and_both_radios(client, private_call) -> None:  # noqa: ANN001
    row = client.get(f"/api/calls/{private_call.id}").json()
    assert row["kind"] == "unit"
    assert row["talkgroup_id"] is None
    assert row["tgid"] is None
    assert row["target_radio_id"] == private_call.target_radio_id
    # What the table renders in place of a talkgroup name.
    assert row["radio_label"] == str(CALLER)
    assert row["target_radio_label"] == str(CALLEE)
    assert row["target_radio"]["radio_id"] == CALLEE


def test_an_ordinary_call_is_a_group_call(client, archive) -> None:  # noqa: ANN001
    row = client.get(f"/api/calls/{archive.call_ids[0]}").json()
    assert row["kind"] == "group"
    assert row["target_radio_id"] is None
    assert row["target_radio_label"] is None


def test_the_kind_filter_narrows_to_one_or_the_other(client, archive, private_call) -> None:  # noqa: ANN001
    everything = client.get("/api/calls").json()
    assert private_call.id in [c["id"] for c in everything["items"]]

    units = client.get("/api/calls", params={"kind": "unit"}).json()
    assert [c["id"] for c in units["items"]] == [private_call.id]

    groups = client.get("/api/calls", params={"kind": "group"}).json()
    assert private_call.id not in [c["id"] for c in groups["items"]]
    assert groups["total"] == everything["total"] - 1


def test_the_radio_filter_reads_both_ends_of_a_private_call(client, private_call) -> None:  # noqa: ANN001
    """The far end took part in the call; a filter that showed only the caller's half would
    leave the callee's own page claiming it was never called."""
    for radio_pk in (private_call.radio_id, private_call.target_radio_id):
        found = client.get("/api/calls", params={"radio": radio_pk}).json()
        assert private_call.id in [c["id"] for c in found["items"]]


def test_stats_counts_unit_calls_on_their_own(client, archive, private_call) -> None:  # noqa: ANN001
    overview = client.get("/api/stats/overview").json()
    assert overview["unit_calls_24h"] == 1
    # A subset of the total, never an addition to it.
    assert overview["unit_calls_24h"] <= overview["calls_24h"]
