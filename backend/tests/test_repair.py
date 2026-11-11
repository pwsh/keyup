"""``keyup fix-upload-system`` — folding a phantom system back into the real one.

The fixture is the shape production actually had: a real ``Example System`` holding the
configured Central site, and beside it a ``System`` literally named ``upload`` (WACN NULL)
that the push path's old fallback label created, holding its own Central — a duplicate —
and Eastside, which is a real neighbor site that only ever landed under the wrong parent.

The ``busy`` fixture adds the other half of the production shape: the phantom in the real
archive holds a million calls, and the repair's whole design is about not stalling the live
feed while it moves them.  Three hundred calls and ``batch_size=50`` buys the same shape —
many committed batches, an interruption in the middle of one — at test speed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import case, func, select, update

from app.models import (
    Base,
    Call,
    Radio,
    RadioAffiliation,
    RadioAssignment,
    RecorderGap,
    RecorderObservation,
    Site,
    SiteFrequency,
    System,
    Talkgroup,
    Unit,
)
from app.repair import (
    RepairIncomplete,
    finish_phantom_merge,
    map_phantom_children,
    merge_phantom_system,
    plan_phantom_merge,
    repoint_phantom_rows,
)
from tests.conftest import requires_db

pytestmark = requires_db

STARTED = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)
#: enough calls that ``--batch-size 50`` has to commit half a dozen times per column
BULK_CALLS = 300


def _call_row(*, system_id, site_id, talkgroup_id, tgid, name, radio_id=None) -> dict:  # noqa: ANN001
    return {
        "system_id": system_id,
        "site_id": site_id,
        "talkgroup_id": talkgroup_id,
        "radio_id": radio_id,
        "talkgroup_ids": [tgid],
        "recorded_at": STARTED + timedelta(seconds=3),
        "started_at": STARTED,
        "started_local": STARTED.replace(tzinfo=None),
        "ended_at": STARTED + timedelta(seconds=3),
        "duration_s": Decimal("3.000"),
        "file_path": f"20260830/{name}.mp3",
        "file_name": f"{name}.mp3",
    }


def _call(session, **kwargs) -> Call:  # noqa: ANN001
    call = Call(**_call_row(**kwargs))
    session.add(call)
    session.flush()
    return call


@pytest.fixture
def phantom(session):  # noqa: ANN001, ANN201
    """The production shape, in miniature.  Returns the ids the assertions need."""
    real = System(name="Example System", wacn=101010, protocol="P25 Phase 1")
    ghost = System(name="upload")
    session.add_all([real, ghost])
    session.flush()

    real_central = Site(system_id=real.id, name="Central", site_number=1, rfss=1)
    ghost_central = Site(system_id=ghost.id, name="Central", nac=0x34)
    eastside = Site(system_id=ghost.id, name="Eastside", site_number=4)
    session.add_all([real_central, ghost_central, eastside])
    session.flush()

    # The same control channel, heard under both parents: one of these has to go.
    session.add_all(
        [
            SiteFrequency(
                site_id=real_central.id, frequency_hz=771_056_250, role="primary_control", source="observed"
            ),
            SiteFrequency(
                site_id=ghost_central.id, frequency_hz=771_056_250, role="primary_control", source="observed"
            ),
            # …and one only the phantom ever heard, which must survive the fold.
            SiteFrequency(
                site_id=ghost_central.id, frequency_hz=772_306_250, role="voice", source="observed"
            ),
        ]
    )
    session.add_all(
        [
            RecorderObservation(
                recorder="trunkd",
                observed_at=STARTED,
                system_id=ghost.id,
                site_id=ghost_central.id,
                payload={"site": {"label": "Central"}},
            ),
            RecorderObservation(
                recorder="trunkd",
                observed_at=STARTED,
                system_id=ghost.id,
                site_id=eastside.id,
                payload={"site": {"label": "Eastside"}},
            ),
        ]
    )
    session.add(
        RecorderGap(
            system_id=ghost.id,
            started_at=STARTED,
            ended_at=STARTED + timedelta(minutes=30),
            duration_s=Decimal("1800.000"),
        )
    )

    real_tg = Talkgroup(system_id=real.id, tgid=1101, alias="EC LAW 1", call_count=7)
    ghost_tg = Talkgroup(system_id=ghost.id, tgid=1101, call_count=2)
    orphan_tg = Talkgroup(system_id=ghost.id, tgid=1999, alias="EC EVENT 9", call_count=1)
    session.add_all([real_tg, ghost_tg, orphan_tg])
    session.flush()

    real_call = _call(
        session,
        system_id=real.id,
        site_id=real_central.id,
        talkgroup_id=real_tg.id,
        tgid=1101,
        name="real",
    )
    ghost_call = _call(
        session,
        system_id=ghost.id,
        site_id=ghost_central.id,
        talkgroup_id=ghost_tg.id,
        tgid=1101,
        name="ghost",
    )
    eastside_call = _call(
        session,
        system_id=ghost.id,
        site_id=eastside.id,
        talkgroup_id=orphan_tg.id,
        tgid=1999,
        name="eastside",
    )
    session.commit()
    return {
        "real": real.id,
        "ghost": ghost.id,
        "real_central": real_central.id,
        "ghost_central": ghost_central.id,
        "eastside": eastside.id,
        "real_tg": real_tg.id,
        "ghost_tg": ghost_tg.id,
        "orphan_tg": orphan_tg.id,
        "calls": (real_call.id, ghost_call.id, eastside_call.id),
    }


def test_the_merge_folds_duplicates_and_keeps_the_real_neighbor(session, phantom) -> None:  # noqa: ANN001
    report = merge_phantom_system(session, dry_run=False)
    session.commit()
    session.expire_all()

    assert report["changed"] is True
    assert report["target"] == {"id": phantom["real"], "name": "Example System"}

    # The phantom is gone, and it took nothing real with it.
    assert session.scalars(select(System).where(System.name == "upload")).first() is None
    assert session.get(System, phantom["real"]) is not None

    # One Central, the configured one, with its id and its site number intact — and the
    # NAC the duplicate had heard folded into the blank the real row left.
    centrals = session.scalars(
        select(Site).where(Site.system_id == phantom["real"], Site.name == "Central")
    ).all()
    assert [s.id for s in centrals] == [phantom["real_central"]]
    assert centrals[0].site_number == 1
    assert centrals[0].nac == 0x34
    assert session.get(Site, phantom["ghost_central"]) is None

    # Eastside is a real site.  It is re-parented, not deleted.
    eastside = session.get(Site, phantom["eastside"])
    assert eastside is not None and eastside.system_id == phantom["real"]

    # Its frequencies: the duplicate control channel dropped, the unheard voice kept.
    freqs = session.scalars(
        select(SiteFrequency).where(SiteFrequency.site_id == phantom["real_central"])
    ).all()
    assert sorted(f.frequency_hz for f in freqs) == [771_056_250, 772_306_250]

    # Every call now hangs off the real system, and the duplicated one off the real site.
    calls = session.scalars(select(Call).order_by(Call.id)).all()
    assert {c.system_id for c in calls} == {phantom["real"]}
    assert [c.site_id for c in calls] == [
        phantom["real_central"],
        phantom["real_central"],
        phantom["eastside"],
    ]
    assert [c.talkgroup_id for c in calls[:2]] == [phantom["real_tg"], phantom["real_tg"]]

    # The duplicate talkgroup is folded, counters summed; the one with no counterpart moves.
    assert session.get(Talkgroup, phantom["ghost_tg"]) is None
    assert session.get(Talkgroup, phantom["real_tg"]).call_count == 9
    assert session.get(Talkgroup, phantom["orphan_tg"]).system_id == phantom["real"]

    # Observations and gaps come across whole.
    assert {
        o.system_id for o in session.scalars(select(RecorderObservation)).all()
    } == {phantom["real"]}
    assert {g.system_id for g in session.scalars(select(RecorderGap)).all()} == {phantom["real"]}


def test_a_second_run_finds_nothing_to_do(session, phantom) -> None:  # noqa: ANN001
    merge_phantom_system(session, dry_run=False)
    session.commit()

    again = merge_phantom_system(session, dry_run=False)
    assert again["changed"] is False
    assert "nothing to do" in again["detail"]


def test_a_dry_run_reports_the_same_merge_and_keeps_nothing(session, phantom) -> None:  # noqa: ANN001
    preview = merge_phantom_system(session, dry_run=True)
    session.rollback()
    session.expire_all()

    assert preview["changed"] is True
    assert [row["name"] for row in preview["sites_merged"]] == ["Central"]
    assert [row["name"] for row in preview["sites_reparented"]] == ["Eastside"]
    # …and the archive is exactly as it was.
    assert session.scalars(select(System).where(System.name == "upload")).first() is not None
    assert session.get(Site, phantom["ghost_central"]) is not None

    applied = merge_phantom_system(session, dry_run=False)
    session.commit()
    assert applied["sites_merged"] == preview["sites_merged"]
    assert applied["sites_reparented"] == preview["sites_reparented"]


def test_it_refuses_to_guess_between_several_systems(session, phantom) -> None:  # noqa: ANN001
    session.add(System(name="West County"))
    session.commit()

    report = merge_phantom_system(session, dry_run=False)

    assert report["changed"] is False
    assert "exactly one obvious target" in report["detail"]
    assert session.scalars(select(System).where(System.name == "upload")).first() is not None


def test_the_cli_command_is_dry_by_default(session, phantom) -> None:  # noqa: ANN001
    import json

    from typer.testing import CliRunner

    from app.cli import app

    result = CliRunner().invoke(app, ["fix-upload-system"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["dry_run"] is True
    session.expire_all()
    assert session.scalars(select(System).where(System.name == "upload")).first() is not None

    result = CliRunner().invoke(app, ["fix-upload-system", "--apply"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["dry_run"] is False and body["changed"] is True
    session.expire_all()
    assert session.scalars(select(System).where(System.name == "upload")).first() is None


# --------------------------------------------------------------------------------------
# the production shape: a phantom holding a million calls, in miniature
# --------------------------------------------------------------------------------------


@pytest.fixture
def busy(session, phantom):  # noqa: ANN001, ANN201
    """``phantom``, plus a twinned radio, a displaced one, and a pile of phantom calls."""
    real_radio = Radio(
        system_id=phantom["real"], radio_id=4831120, wacn=101010, home_system_id=57, call_count=5
    )
    ghost_radio = Radio(
        system_id=phantom["ghost"], radio_id=4831120, wacn=101010, home_system_id=57, call_count=3
    )
    lone_radio = Radio(system_id=phantom["ghost"], radio_id=5250057, alias="M12", call_count=11)
    session.add_all([real_radio, ghost_radio, lone_radio])
    session.flush()

    session.execute(
        Call.__table__.insert(),
        [
            _call_row(
                system_id=phantom["ghost"],
                site_id=phantom["ghost_central"],
                talkgroup_id=phantom["ghost_tg"],
                radio_id=ghost_radio.id,
                tgid=1101,
                name=f"bulk-{n:04d}",
            )
            for n in range(BULK_CALLS)
        ],
    )
    session.commit()
    return {
        **phantom,
        "real_radio": real_radio.id,
        "ghost_radio": ghost_radio.id,
        "lone_radio": lone_radio.id,
    }


def _row_counts(session) -> dict[str, int]:  # noqa: ANN001
    """Every table's row count — the cheapest possible proof that nothing was written."""
    return {
        name: session.scalar(select(func.count()).select_from(table))
        for name, table in Base.metadata.tables.items()
    }


class Interrupted(RuntimeError):
    """Stands in for the Ctrl-C an operator presses in the middle of phase B."""


def _stop_after(batches):  # noqa: ANN001, ANN202
    seen = {"n": 0}

    def hook(event) -> None:  # noqa: ANN001, ARG001
        seen["n"] += 1
        if seen["n"] >= batches:
            raise Interrupted

    return hook


def test_the_dry_run_reads_the_archive_and_writes_absolutely_nothing(session, busy) -> None:  # noqa: ANN001
    """No rehearsal transaction: the plan is COUNTs, and the phantom is untouched after."""
    before = _row_counts(session)

    report = plan_phantom_merge(session)

    session.expire_all()
    assert _row_counts(session) == before
    assert session.get(System, busy["ghost"]) is not None
    assert session.get(Site, busy["ghost_central"]) is not None
    assert session.get(Radio, busy["ghost_radio"]) is not None
    assert session.get(Site, busy["eastside"]).system_id == busy["ghost"]

    # …and it still says exactly what the real run will do.
    assert report["changed"] is True and report["dry_run"] is True
    assert [row["name"] for row in report["sites_merged"]] == ["Central"]
    assert [row["name"] for row in report["sites_reparented"]] == ["Eastside"]
    assert [row["radio_id"] for row in report["radios_merged"]] == [4831120]
    assert [row["radio_id"] for row in report["radios_reparented"]] == [5250057]
    counts = report["rows_to_repoint"]
    assert counts["calls.system_id"] == BULK_CALLS + 2
    assert counts["calls.site_id"] == BULK_CALLS + 1
    assert counts["calls.talkgroup_id"] == BULK_CALLS + 1
    assert counts["calls.radio_id"] == BULK_CALLS
    assert counts["site_frequencies.site_id"] == 2
    assert report["rows_to_repoint_total"] == sum(counts.values())
    assert report["would_delete"] == {"sites": 1, "talkgroups": 1, "radios": 1, "systems": 1}


def test_applying_it_in_many_small_batches_lands_where_one_transaction_did(session, busy) -> None:  # noqa: ANN001
    """The end state is the old single-transaction merge's, reached fifty rows at a time."""
    seen: list[dict] = []
    report = merge_phantom_system(session, dry_run=False, batch_size=50, on_batch=seen.append)
    session.expire_all()

    # It really did commit many times rather than once.
    assert len(seen) > 6
    assert max(e["batch"] for e in seen if e["table"] == "calls" and e["column"] == "system_id") >= 6

    assert report["changed"] is True
    assert session.scalars(select(System).where(System.name == "upload")).first() is None

    # Not one call was lost, and every one of them hangs off the real system now.
    calls = session.scalars(select(Call)).all()
    assert len(calls) == BULK_CALLS + 3
    assert {c.system_id for c in calls} == {busy["real"]}
    assert {c.site_id for c in calls} == {busy["real_central"], busy["eastside"]}
    assert {c.talkgroup_id for c in calls} == {busy["real_tg"], busy["orphan_tg"]}
    assert {c.radio_id for c in calls} == {None, busy["real_radio"]}

    # The duplicates folded, counters summed, blanks filled; the originals re-parented.
    assert session.get(Site, busy["ghost_central"]) is None
    assert session.get(Site, busy["real_central"]).nac == 0x34
    assert session.get(Talkgroup, busy["real_tg"]).call_count == 9
    assert session.get(Radio, busy["ghost_radio"]) is None
    assert session.get(Radio, busy["real_radio"]).call_count == 8
    assert session.get(Radio, busy["lone_radio"]).system_id == busy["real"]
    assert session.get(Site, busy["eastside"]).system_id == busy["real"]

    # The twinned control channel dropped, the one only the phantom ever heard kept.
    freqs = session.scalars(
        select(SiteFrequency).where(SiteFrequency.site_id == busy["real_central"])
    ).all()
    assert sorted(f.frequency_hz for f in freqs) == [771_056_250, 772_306_250]
    assert {o.system_id for o in session.scalars(select(RecorderObservation)).all()} == {
        busy["real"]
    }
    assert {g.system_id for g in session.scalars(select(RecorderGap)).all()} == {busy["real"]}


def test_an_interrupted_repoint_keeps_its_committed_batches_and_resumes(session, busy) -> None:  # noqa: ANN001
    """Ctrl-C in the middle of phase B, then run it again: it finishes, losing nothing."""
    fold = map_phantom_children(session)
    with pytest.raises(Interrupted):
        repoint_phantom_rows(session, fold, batch_size=50, on_batch=_stop_after(3))
    session.rollback()
    session.expire_all()

    # Some rows moved and stayed moved; some did not.  Nothing was destroyed either way.
    assert session.scalar(select(func.count()).select_from(Call)) == BULK_CALLS + 3
    left = session.scalar(
        select(func.count()).select_from(Call).where(Call.site_id == busy["ghost_central"])
    )
    assert 0 < left < BULK_CALLS + 1
    assert session.get(System, busy["ghost"]) is not None

    # Phase C will not paper over it.
    with pytest.raises(RepairIncomplete) as raised:
        finish_phantom_merge(session, fold)
    assert "run the repair again to resume" in str(raised.value)
    assert session.get(System, busy["ghost"]) is not None

    # Running the whole repair again picks up where it stopped and finishes the job.
    session.rollback()
    report = merge_phantom_system(session, dry_run=False, batch_size=50)
    session.expire_all()
    assert report["changed"] is True
    assert session.get(System, busy["ghost"]) is None
    calls = session.scalars(select(Call)).all()
    assert len(calls) == BULK_CALLS + 3
    assert {c.system_id for c in calls} == {busy["real"]}
    assert session.get(Talkgroup, busy["real_tg"]).call_count == 9
    assert session.get(Radio, busy["real_radio"]).call_count == 8


def test_phase_c_refuses_a_merge_whose_repointing_never_ran(session, busy) -> None:  # noqa: ANN001
    """Map, then jump straight to finish: it names what is still pointing at the phantom."""
    fold = map_phantom_children(session)

    with pytest.raises(RepairIncomplete) as raised:
        finish_phantom_merge(session, fold)

    message = str(raised.value)
    assert "repointing is not finished" in message
    assert f"calls.system_id: {BULK_CALLS + 2} rows" in message
    session.expire_all()
    assert session.get(System, busy["ghost"]) is not None
    assert session.scalar(select(func.count()).select_from(Call)) == BULK_CALLS + 3


def test_running_it_again_after_a_clean_run_is_a_no_op(session, busy) -> None:  # noqa: ANN001
    """The second pass finds no phantom, says so, and touches nothing."""
    merge_phantom_system(session, dry_run=False, batch_size=50)
    session.expire_all()
    after = _row_counts(session)

    again = merge_phantom_system(session, dry_run=False, batch_size=50)

    assert again["changed"] is False
    assert again["detail"] == "no system named 'upload' — nothing to do"
    session.expire_all()
    assert _row_counts(session) == after
    # …and the dry run agrees, rather than reporting a merge it cannot do.
    assert plan_phantom_merge(session)["changed"] is False


def test_the_cli_takes_a_batch_size_and_reports_what_it_moved(session, busy) -> None:  # noqa: ANN001, ARG001
    import json

    from typer.testing import CliRunner

    from app.cli import app

    result = CliRunner().invoke(app, ["fix-upload-system", "--apply", "--batch-size", "50"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["dry_run"] is False and body["changed"] is True
    assert body["rows_repointed"]["calls.system_id"] == BULK_CALLS + 2
    assert body["duplicates_dropped"]["site_frequencies.site_id"] == 1
    assert body["deleted"] == {"sites": 1, "talkgroups": 1, "radios": 1, "systems": 1}
    session.expire_all()
    assert session.scalars(select(System).where(System.name == "upload")).first() is None


# --------------------------------------------------------------------------------------
# a unique key that spans two folded columns
# --------------------------------------------------------------------------------------


@pytest.fixture
def affiliated(session, busy):  # noqa: ANN001, ANN201
    """The production crash, in miniature: three affiliations that fold into one row.

    ``uq_radio_affiliations_radio_talkgroup`` spans both foreign keys, and the fold moves
    both of them.  ``(A', X')`` is the pure phantom pair, ``(A', X)`` the phantom radio
    already heard on the *real* talkgroup, ``(A, X')`` the real radio on the phantom
    talkgroup — three different rows today, one and the same row once the fold has run.
    """
    rows = [
        (busy["ghost_radio"], busy["ghost_tg"], 1),
        (busy["ghost_radio"], busy["real_tg"], 2),
        (busy["real_radio"], busy["ghost_tg"], 3),
    ]
    for radio_pk, talkgroup_pk, count in rows:
        session.add(
            RadioAffiliation(
                radio_id_fk=radio_pk,
                talkgroup_id_fk=talkgroup_pk,
                first_heard_at=STARTED,
                last_heard_at=STARTED + timedelta(seconds=count),
                call_count=count,
            )
        )
        session.flush()  # one at a time, so the primary keys ascend in the order above
    session.commit()
    return busy


def _affiliations(session):  # noqa: ANN001, ANN202
    return session.scalars(select(RadioAffiliation).order_by(RadioAffiliation.id)).all()


def test_a_key_spanning_two_folded_columns_collapses_in_one_go(session, affiliated) -> None:  # noqa: ANN001
    """The crash this repair used to die on: both halves of the key move, so both count.

    Reading one column at a time against the row's *current* other column finds no
    collision here at all — and then the radios pass inserts the duplicate the talkgroups
    pass left behind.  Fully mapped keys see one group of three from the first batch on.
    """
    report = merge_phantom_system(session, dry_run=False, batch_size=50)
    session.expire_all()

    # It ran to the end — phase C dropped the phantom — rather than aborting mid-phase.
    assert report["changed"] is True
    assert session.get(System, affiliated["ghost"]) is None
    assert report["deleted"] == {"sites": 1, "talkgroups": 1, "radios": 1, "systems": 1}

    # Exactly one affiliation is left, and it is the real radio on the real talkgroup.
    left = _affiliations(session)
    assert [(row.radio_id_fk, row.talkgroup_id_fk) for row in left] == [
        (affiliated["real_radio"], affiliated["real_tg"])
    ]
    # …the lowest primary key of the three, kept deterministically so a re-run agrees.
    assert left[0].call_count == 1
    assert report["duplicates_dropped"]["radio_affiliations.talkgroup_id_fk"] == 2


def test_it_resumes_from_the_crash_that_stopped_it_half_way(session, affiliated) -> None:  # noqa: ANN001
    """Phase B committed through ``calls.talkgroup_id`` and then died; run it again."""
    fold = map_phantom_children(session)
    for table_name, column_name in (
        ("calls", "site_id"),
        ("conversations", "talkgroup_id"),
        ("calls", "talkgroup_id"),
    ):
        table = Base.metadata.tables[table_name]
        column = table.c[column_name]
        parent = next(fk.column.table.name for fk in column.foreign_keys)
        id_map = fold.id_maps[parent]
        session.execute(
            update(table)
            .where(column.in_(list(id_map)))
            .values(
                {column_name: case(*[(column == o, n) for o, n in id_map.items()], else_=column)}
            )
        )
    session.commit()
    session.expire_all()

    # Half-moved, exactly as production was found: calls across, affiliations untouched.
    assert (
        session.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.talkgroup_id == affiliated["ghost_tg"])
        )
        == 0
    )
    assert len(_affiliations(session)) == 3

    report = merge_phantom_system(session, dry_run=False, batch_size=50)
    session.expire_all()

    assert report["changed"] is True
    assert session.get(System, affiliated["ghost"]) is None
    assert [(row.radio_id_fk, row.talkgroup_id_fk) for row in _affiliations(session)] == [
        (affiliated["real_radio"], affiliated["real_tg"])
    ]
    assert session.scalar(select(func.count()).select_from(Call)) == BULK_CALLS + 3


def test_a_partial_unique_index_is_caught_by_the_retry_rather_than_aborting(session, busy) -> None:  # noqa: ANN001
    """The keys ``_drop_losers`` cannot read by name still must not take the run down.

    ``uq_radio_assignments_current_unit`` is partial — at most one *current* unit
    assignment per radio — so the fold cannot compare it by column name and the batch
    update walks into it.  The batch is retried a row at a time and the row that will not
    go is dropped, loudly, instead of aborting phase B.
    """
    unit = Unit(callsign="E1")
    session.add(unit)
    session.flush()
    session.add_all(
        [
            RadioAssignment(radio_pk=busy["real_radio"], unit_id=unit.id),
            RadioAssignment(radio_pk=busy["ghost_radio"], unit_id=unit.id),
        ]
    )
    session.commit()

    report = merge_phantom_system(session, dry_run=False, batch_size=50)
    session.expire_all()

    assert report["changed"] is True
    assert session.get(System, busy["ghost"]) is None
    assignments = session.scalars(select(RadioAssignment)).all()
    assert [a.radio_pk for a in assignments] == [busy["real_radio"]]
    assert report["duplicates_dropped"]["radio_assignments.radio_pk"] == 1
