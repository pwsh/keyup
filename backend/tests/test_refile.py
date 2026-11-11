"""``keyup refile-calls`` — putting a mislabeled slice of calls under the right system.

The fixture is the shape production actually had.  ``Example System`` holds a ``Central``
site with its own traffic and a ``Eastside`` site holding nothing but calls whose filenames
say ``EXAMPLE_SCHOOL_DISTRICT`` — the school district's own P25 system,
recorded under the county network's label.  Because a talkgroup is keyed on
``(system_id, tgid)``, the school's tgid 101 was merged into the county's tgid 101 (a different
channel with the same number) and its radios merged into the county's radios the same way: the
county's own calls sit on the very rows the school's calls sit on.

The target system and its empty site already exist, exactly as production's do.  The
``busy`` fixture adds the other half of the shape — enough calls that ``batch_size`` has to
commit several times — because the whole design is about not stalling the live feed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    Call,
    CallSource,
    Conversation,
    Radio,
    RadioAffiliation,
    RadioEvent,
    RecorderObservation,
    Site,
    System,
    Talkgroup,
    Tuner,
)
from app.refile import (
    RefileIncomplete,
    RefileTargetMissing,
    finish_refile,
    map_refile,
    plan_refile,
    refile_calls,
    repoint_refiled_calls,
    site_blockers,
)
from tests.conftest import requires_db

pytestmark = requires_db

STARTED = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)

#: The filename shape production matches on, and the pattern the operator types.
DISTRICT = "EXAMPLE_SCHOOL_DISTRICT_Eastside_District_Schools"
MATCH = f"%{DISTRICT}%"

#: Enough that ``--batch-size 25`` has to commit several times.
BULK_CALLS = 120


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
def mislabeled(session):  # noqa: ANN001, ANN201
    """The production shape, in miniature.  Returns the ids the assertions need."""
    example = System(name="Example System", wacn=101010, protocol="P25 Phase 1")
    district = System(name="EXAMPLE SCHOOL DISTRICT", wacn=101010)
    session.add_all([example, district])
    session.flush()

    central = Site(system_id=example.id, name="Central", site_number=1, rfss=1)
    # Two sites called Eastside, one under each system: the whole reason a site name is never
    # rendered on its own.  the county's holds the mislabeled slice; the district's is empty and waiting.
    example_eastside = Site(system_id=example.id, name="Eastside", site_number=4)
    district_eastside = Site(system_id=district.id, name="Eastside")
    session.add_all([central, example_eastside, district_eastside])
    session.flush()

    # tgid 101 exists on both systems in the *real* archive — but the archive we are
    # repairing has only the county's, because the ingest merged the school's into it.
    shared_tg = Talkgroup(system_id=example.id, tgid=101, alias="EC LAW 1", category="law")
    school_only_tg = Talkgroup(
        system_id=example.id, tgid=1201, alias="DISTRICT BUS 1", category="schools", service_tag="Schools"
    )
    session.add_all([shared_tg, school_only_tg])
    session.flush()

    shared_radio = Radio(system_id=example.id, radio_id=5200007, alias="E1", kind="mobile")
    school_radio = Radio(system_id=example.id, radio_id=9900001, alias="BUS 12", kind="portable")
    session.add_all([shared_radio, school_radio])
    session.flush()

    # the county's own traffic: on the shared talkgroup, with the shared radio, at Central.
    example_call = _call(
        session,
        system_id=example.id,
        site_id=central.id,
        talkgroup_id=shared_tg.id,
        tgid=101,
        name="20260830_140000_Example-System_Central_T-Control__TO_101_FROM_5200007",
        radio_id=shared_radio.id,
    )
    # The school's traffic, mislabeled: same talkgroup row, same radio row, wrong site.
    school_shared = _call(
        session,
        system_id=example.id,
        site_id=example_eastside.id,
        talkgroup_id=shared_tg.id,
        tgid=101,
        name=f"20260830_140500_{DISTRICT}__TO_101_FROM_5200007",
        radio_id=shared_radio.id,
    )
    school_only = _call(
        session,
        system_id=example.id,
        site_id=example_eastside.id,
        talkgroup_id=school_only_tg.id,
        tgid=1201,
        name=f"20260830_141000_{DISTRICT}__TO_1201_FROM_9900001",
        radio_id=school_radio.id,
    )
    session.add_all(
        [
            CallSource(call_id=school_only.id, radio_id=school_radio.id, src=9900001, seq=0),
            CallSource(call_id=school_only.id, radio_id=shared_radio.id, src=5200007, seq=1),
        ]
    )

    # Two affiliations.  (shared_radio, shared_tg) is claimed by the county's own call as well, so
    # it must stay; (school_radio, school_only_tg) is exclusively the school's and must move.
    session.add_all(
        [
            RadioAffiliation(
                radio_id_fk=shared_radio.id,
                talkgroup_id_fk=shared_tg.id,
                last_heard_at=STARTED,
                call_count=2,
            ),
            RadioAffiliation(
                radio_id_fk=school_radio.id,
                talkgroup_id_fk=school_only_tg.id,
                last_heard_at=STARTED,
                call_count=1,
            ),
        ]
    )
    session.add(
        RadioEvent(
            at=STARTED,
            system_id=example.id,
            site_id=example_eastside.id,
            kind="group_affiliation",
            radio_pk=school_radio.id,
            radio_id=9900001,
            talkgroup_tgid=1201,
            talkgroup_pk=school_only_tg.id,
        )
    )

    # A thread of the school's calls only (must be re-parented) and one of the county's own
    # (untouched, because the refile never names it).
    school_thread = Conversation(
        talkgroup_id=school_only_tg.id, started_at=STARTED, ended_at=STARTED, call_count=1
    )
    example_thread = Conversation(
        talkgroup_id=shared_tg.id, started_at=STARTED, ended_at=STARTED, call_count=2
    )
    session.add_all([school_thread, example_thread])
    session.flush()
    school_only.conversation_id = school_thread.id
    example_call.conversation_id = example_thread.id
    school_shared.conversation_id = example_thread.id

    # The counters as ingest left them, so the recount has something to correct.
    shared_tg.call_count = 2
    school_only_tg.call_count = 1
    shared_radio.call_count = 2
    school_radio.call_count = 1
    session.commit()

    return {
        "example": example.id,
        "district": district.id,
        "central": central.id,
        "example_eastside": example_eastside.id,
        "district_eastside": district_eastside.id,
        "shared_tg": shared_tg.id,
        "school_only_tg": school_only_tg.id,
        "shared_radio": shared_radio.id,
        "school_radio": school_radio.id,
        "example_call": example_call.id,
        "school_shared": school_shared.id,
        "school_only": school_only.id,
        "school_thread": school_thread.id,
        "example_thread": example_thread.id,
    }


@pytest.fixture
def busy(session, mislabeled):  # noqa: ANN001, ANN201
    """The same shape with enough mislabeled calls to need many committed batches."""
    session.add_all(
        [
            Call(
                **_call_row(
                    system_id=mislabeled["example"],
                    site_id=mislabeled["example_eastside"],
                    talkgroup_id=mislabeled["school_only_tg"],
                    tgid=1201,
                    name=f"20260830_15{index:04d}_{DISTRICT}__TO_1201_FROM_9900001",
                    radio_id=mislabeled["school_radio"],
                )
            )
            for index in range(BULK_CALLS)
        ]
    )
    session.commit()
    return mislabeled


def _apply(session, **kwargs) -> dict:  # noqa: ANN001
    return refile_calls(
        session,
        match=MATCH,
        to_system="EXAMPLE SCHOOL DISTRICT",
        to_site="Eastside",
        dry_run=False,
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# the dry run
# --------------------------------------------------------------------------------------


def test_the_dry_run_reports_the_plan_and_writes_nothing(session, mislabeled) -> None:  # noqa: ANN001
    before = session.scalar(select(func.count()).select_from(Talkgroup))
    report = plan_refile(
        session,
        match=MATCH,
        to_system="EXAMPLE SCHOOL DISTRICT",
        to_site="Eastside",
    )

    assert report["dry_run"] is True
    assert report["calls_to_move"] == 2
    assert report["from"] == [
        {
            "system_id": mislabeled["example"],
            "system": "Example System",
            "site_id": mislabeled["example_eastside"],
            "site": "Eastside",
            "calls": 2,
        }
    ]
    # Both talkgroups and both radios the slice names need twins: the target system is
    # empty of everything but its site.
    assert {row["tgid"] for row in report["talkgroups_created"]} == {101, 1201}
    assert {row["radio_id"] for row in report["radios_created"]} == {5200007, 9900001}
    assert all(row["id"] is None for row in report["talkgroups_created"])

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Talkgroup)) == before
    assert session.get(Call, mislabeled["school_only"]).system_id == mislabeled["example"]


def test_a_missing_target_is_refused_rather_than_created(session, mislabeled) -> None:  # noqa: ANN001, ARG001
    with pytest.raises(RefileTargetMissing, match="no system named"):
        plan_refile(session, match=MATCH, to_system="Typo District", to_site="Eastside")
    with pytest.raises(RefileTargetMissing, match="no site named"):
        plan_refile(
            session,
            match=MATCH,
            to_system="EXAMPLE SCHOOL DISTRICT",
            to_site="Nowhere",
        )
    assert session.scalars(select(System).where(System.name == "Typo District")).first() is None


# --------------------------------------------------------------------------------------
# the move
# --------------------------------------------------------------------------------------


def test_the_slice_moves_and_the_rest_of_the_archive_does_not(session, mislabeled) -> None:  # noqa: ANN001
    report = _apply(session)
    session.expire_all()

    assert report["changed"] is True
    assert report["calls_moved"] == 2

    district, district_eastside = mislabeled["district"], mislabeled["district_eastside"]
    for call_id in (mislabeled["school_shared"], mislabeled["school_only"]):
        call = session.get(Call, call_id)
        assert call.system_id == district
        assert call.site_id == district_eastside

    # the county's own call is untouched, on the row it always sat on.
    example_call = session.get(Call, mislabeled["example_call"])
    assert example_call.system_id == mislabeled["example"]
    assert example_call.site_id == mislabeled["central"]
    assert example_call.talkgroup_id == mislabeled["shared_tg"]
    assert example_call.radio_id == mislabeled["shared_radio"]

    # Nothing was deleted: three calls in, three calls out.
    assert session.scalar(select(func.count()).select_from(Call)) == 3


def test_twins_are_created_under_the_target_and_the_source_rows_survive(session, mislabeled) -> None:  # noqa: ANN001
    report = _apply(session)
    session.expire_all()
    district = mislabeled["district"]

    assert {row["tgid"] for row in report["talkgroups_created"]} == {101, 1201}
    assert {row["radio_id"] for row in report["radios_created"]} == {5200007, 9900001}

    twin_101 = session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == district, Talkgroup.tgid == 101)
    ).one()
    twin_1201 = session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == district, Talkgroup.tgid == 1201)
    ).one()
    # A freshly created twin is seeded from the source's identity, fill-only.
    assert twin_1201.alias == "DISTRICT BUS 1"
    assert twin_1201.category == "schools"
    assert twin_1201.service_tag == "Schools"

    assert session.get(Call, mislabeled["school_shared"]).talkgroup_id == twin_101.id
    assert session.get(Call, mislabeled["school_only"]).talkgroup_id == twin_1201.id
    # The county rows the school's traffic was merged into are still there, still the county's.
    assert session.get(Talkgroup, mislabeled["shared_tg"]).system_id == mislabeled["example"]
    assert session.get(Radio, mislabeled["shared_radio"]).system_id == mislabeled["example"]


def test_an_existing_twin_is_used_untouched(session, mislabeled) -> None:  # noqa: ANN001
    """A twin the operator already curated under the target is never overwritten."""
    curated = Talkgroup(
        system_id=mislabeled["district"], tgid=1201, alias="Transportation", category="schools"
    )
    session.add(curated)
    session.commit()
    curated_id = curated.id

    report = _apply(session)
    session.expire_all()

    assert 1201 not in {row["tgid"] for row in report["talkgroups_created"]}
    assert session.get(Talkgroup, curated_id).alias == "Transportation"
    assert session.get(Call, mislabeled["school_only"]).talkgroup_id == curated_id


def test_call_sources_follow_their_call(session, mislabeled) -> None:  # noqa: ANN001
    _apply(session)
    session.expire_all()
    district = mislabeled["district"]

    sources = session.scalars(
        select(CallSource).where(CallSource.call_id == mislabeled["school_only"])
    ).all()
    assert len(sources) == 2
    for source in sources:
        assert session.get(Radio, source.radio_id).system_id == district
    # …including the second talker, whose radio is the county's shared one.
    assert {source.src for source in sources} == {9900001, 5200007}


def test_only_the_exclusive_derived_rows_move(session, mislabeled) -> None:  # noqa: ANN001
    report = _apply(session)
    session.expire_all()
    district = mislabeled["district"]

    # The shared pair stays: the county's own call still puts that radio on that talkgroup.
    assert report["affiliations"] == {"moved": 1, "kept": 1}
    shared = session.scalars(
        select(RadioAffiliation).where(
            RadioAffiliation.radio_id_fk == mislabeled["shared_radio"],
            RadioAffiliation.talkgroup_id_fk == mislabeled["shared_tg"],
        )
    ).one()
    assert shared is not None

    # The school-only pair moved onto the twins.
    moved = session.scalars(
        select(RadioAffiliation).where(
            RadioAffiliation.radio_id_fk != mislabeled["shared_radio"],
            RadioAffiliation.talkgroup_id_fk != mislabeled["shared_tg"],
        )
    ).one()
    assert session.get(Radio, moved.radio_id_fk).system_id == district
    assert session.get(Talkgroup, moved.talkgroup_id_fk).system_id == district

    # The radio event moved with the pair, and took its site and system with it.
    event = session.scalars(select(RadioEvent)).one()
    assert event.system_id == district
    assert event.site_id == mislabeled["district_eastside"]
    assert session.get(Talkgroup, event.talkgroup_pk).system_id == district


def test_a_thread_moves_only_when_all_of_its_calls_did(session, mislabeled) -> None:  # noqa: ANN001
    report = _apply(session)
    session.expire_all()

    assert report["conversations"] == {"moved": 1, "kept": 1}
    school_thread = session.get(Conversation, mislabeled["school_thread"])
    assert session.get(Talkgroup, school_thread.talkgroup_id).system_id == mislabeled["district"]
    # the county's thread holds one call that moved and one that did not, so it is left alone.
    example_thread = session.get(Conversation, mislabeled["example_thread"])
    assert example_thread.talkgroup_id == mislabeled["shared_tg"]


def test_counters_are_recomputed_on_both_sides(session, mislabeled) -> None:  # noqa: ANN001
    _apply(session)
    session.expire_all()
    district = mislabeled["district"]

    # the county's shared talkgroup keeps only its own call; its radio likewise.
    assert session.get(Talkgroup, mislabeled["shared_tg"]).call_count == 1
    assert session.get(Radio, mislabeled["shared_radio"]).call_count == 1
    # The emptied source rows drop to zero rather than keeping the school's calls.
    assert session.get(Talkgroup, mislabeled["school_only_tg"]).call_count == 0
    assert session.get(Radio, mislabeled["school_radio"]).call_count == 0

    twin_101 = session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == district, Talkgroup.tgid == 101)
    ).one()
    twin_1201 = session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == district, Talkgroup.tgid == 1201)
    ).one()
    assert twin_101.call_count == 1
    assert twin_1201.call_count == 1


# --------------------------------------------------------------------------------------
# the emptied source site
# --------------------------------------------------------------------------------------


def test_the_emptied_source_site_is_reported_and_kept(session, mislabeled) -> None:  # noqa: ANN001
    report = _apply(session)
    session.expire_all()

    assert report["emptied_source_sites"] == [
        {"id": mislabeled["example_eastside"], "name": "Eastside", "system": "Example System"}
    ]
    assert "now holds no calls" in report["detail"]
    assert "dropped_source_sites" not in report
    assert session.get(Site, mislabeled["example_eastside"]) is not None


def test_drop_empty_source_site_deletes_it_in_the_same_run(session, mislabeled) -> None:  # noqa: ANN001
    report = _apply(session, drop_empty_source_site=True)
    session.expire_all()

    assert [row["id"] for row in report["dropped_source_sites"]] == [mislabeled["example_eastside"]]
    assert "was emptied by the move and deleted" in report["detail"]
    assert session.get(Site, mislabeled["example_eastside"]) is None
    # The site the calls landed on, and every other site, is untouched.
    assert session.get(Site, mislabeled["district_eastside"]) is not None
    assert session.get(Site, mislabeled["central"]) is not None


def test_a_source_site_something_else_references_is_not_dropped(session, mislabeled) -> None:  # noqa: ANN001
    session.add(
        RecorderObservation(
            recorder="trunkd",
            observed_at=STARTED,
            system_id=mislabeled["example"],
            site_id=mislabeled["example_eastside"],
            payload={"site": {"label": "Eastside"}},
        )
    )
    session.commit()

    report = _apply(session, drop_empty_source_site=True)
    session.expire_all()

    assert report["dropped_source_sites"] == []
    assert report["emptied_source_sites"][0]["blocked_by"] == {"recorder_observations": 1}
    assert session.get(Site, mislabeled["example_eastside"]) is not None


# --------------------------------------------------------------------------------------
# batching, resuming and re-running
# --------------------------------------------------------------------------------------


def test_the_move_commits_many_batches(session, busy) -> None:  # noqa: ANN001, ARG001
    seen: list[dict] = []
    report = _apply(session, batch_size=25, on_batch=seen.append)

    assert report["calls_moved"] == BULK_CALLS + 2
    assert report["batches"] >= 5
    assert len(seen) == report["batches"]
    assert seen[-1]["rows_done"] == seen[-1]["rows_total"] == BULK_CALLS + 2


def test_an_interrupted_move_resumes_where_it_stopped(session, busy) -> None:  # noqa: ANN001
    """Ctrl-C in the middle keeps the committed batches and leaves the rest to a re-run."""

    class Stop(RuntimeError):
        pass

    def die_after_two(event: dict) -> None:
        if event["batch"] == 2:
            raise Stop

    plan = map_refile(
        session,
        match=MATCH,
        to_system="EXAMPLE SCHOOL DISTRICT",
        to_site="Eastside",
    )
    with pytest.raises(Stop):
        repoint_refiled_calls(session, plan, batch_size=25, on_batch=die_after_two)
    session.rollback()
    session.expire_all()

    half = session.scalar(
        select(func.count()).select_from(Call).where(Call.system_id == busy["district"])
    )
    assert 0 < half < BULK_CALLS + 2
    # Phase C refuses to finish over a half-moved slice, because its exclusivity tests
    # would read the source side as still claiming pairs that are about to move.
    with pytest.raises(RefileIncomplete, match="have not moved yet"):
        finish_refile(session, plan)

    report = _apply(session, batch_size=25)
    session.expire_all()
    assert report["calls_moved"] == BULK_CALLS + 2 - half
    assert (
        session.scalar(select(func.count()).select_from(Call).where(Call.system_id == busy["district"]))
        == BULK_CALLS + 2
    )


def test_running_it_twice_is_a_no_op(session, mislabeled) -> None:  # noqa: ANN001
    first = _apply(session)
    session.expire_all()
    second = _apply(session)
    session.expire_all()

    assert first["calls_moved"] == 2
    assert second["calls_moved"] == 0
    assert second["talkgroups_created"] == []
    assert second["radios_created"] == []
    assert second["changed"] is False
    # No third talkgroup appeared under the target on the second pass.
    assert (
        session.scalar(
            select(func.count()).select_from(Talkgroup).where(Talkgroup.system_id == mislabeled["district"])
        )
        == 2
    )
    assert session.scalar(select(func.count()).select_from(Call)) == 3


def test_the_pattern_chooses_the_slice_and_nothing_else(session, mislabeled) -> None:  # noqa: ANN001
    """A pattern matching nothing moves nothing and still reports cleanly."""
    report = refile_calls(
        session,
        match="%NO_SUCH_DISTRICT%",
        to_system="EXAMPLE SCHOOL DISTRICT",
        to_site="Eastside",
        dry_run=False,
    )
    session.expire_all()

    assert report["calls_moved"] == 0
    assert report["changed"] is False
    assert session.get(Call, mislabeled["school_only"]).system_id == mislabeled["example"]


# --------------------------------------------------------------------------------------
# what holds a site
# --------------------------------------------------------------------------------------


def test_site_blockers_names_everything_that_holds_a_site(session, mislabeled) -> None:  # noqa: ANN001
    assert site_blockers(session, mislabeled["district_eastside"]) == {}
    assert site_blockers(session, mislabeled["example_eastside"]) == {"calls": 2, "radio_events": 1}


def test_a_tuner_focused_on_a_site_blocks_it(session, mislabeled) -> None:  # noqa: ANN001
    from app.models import Recorder

    recorder = Recorder(instance_id="trunkd-1")
    session.add(recorder)
    session.flush()
    session.add(
        Tuner(
            recorder_id=recorder.id,
            tuner_key="rtl-0",
            desired={"site_id": mislabeled["district_eastside"], "role": "control"},
        )
    )
    session.commit()

    assert site_blockers(session, mislabeled["district_eastside"]) == {"tuners": 1}


# --------------------------------------------------------------------------------------
# what a twin is seeded with, and the CLI the repair is run with
# --------------------------------------------------------------------------------------


def test_a_twin_is_seeded_only_from_a_source_row_the_move_empties(session, mislabeled) -> None:  # noqa: ANN001
    """tgid 1201 was only ever the school's; tgid 101 is the county's own channel too.

    Seeding the 101 twin from the county's row would put the county's label — "EC LAW 1", a law
    channel — on the school district's channel 101, which is a different conversation that
    happens to carry the same number.  So that twin gets the number and nothing else.
    """
    report = _apply(session)
    session.expire_all()
    district = mislabeled["district"]

    seeded = {row["tgid"]: row["seeded"] for row in report["talkgroups_created"]}
    assert seeded == {101: False, 1201: True}

    twin_101 = session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == district, Talkgroup.tgid == 101)
    ).one()
    twin_1201 = session.scalars(
        select(Talkgroup).where(Talkgroup.system_id == district, Talkgroup.tgid == 1201)
    ).one()

    assert twin_101.alias is None
    assert twin_101.category is None
    assert twin_1201.alias == "DISTRICT BUS 1"
    assert twin_1201.service_tag == "Schools"
    # And the county's own row is untouched by any of it.
    assert session.get(Talkgroup, mislabeled["shared_tg"]).alias == "EC LAW 1"


def test_the_move_drops_the_sites_cached_call_counts(session, mislabeled) -> None:  # noqa: ANN001
    """The Sites page caches its per-site counts for a minute; a refile must not be stale."""
    from app.services import sites as site_service

    site_service.call_counts(session)
    assert site_service._counts_cache is not None  # noqa: SLF001

    _apply(session)

    assert site_service._counts_cache is None  # noqa: SLF001


def test_a_literal_substring_becomes_a_pattern_that_means_itself() -> None:
    from app.cli import _like_substring

    # A filename's % and _ are characters, not wildcards, so they are escaped — and the
    # escaped pattern still selects exactly the slice (the CLI tests below run it).
    assert _like_substring(DISTRICT) == "%" + DISTRICT.replace("_", r"\_") + "%"
    assert _like_substring("a_b%c") == r"%a\_b\%c%"


def test_the_cli_reports_the_plan_without_writing(session, mislabeled) -> None:  # noqa: ANN001
    from typer.testing import CliRunner

    from app.cli import app as cli

    result = CliRunner().invoke(
        cli,
        [
            "refile-system",
            "--match", DISTRICT,
            "--to-system", "EXAMPLE SCHOOL DISTRICT",
            "--to-site", "Eastside",
            "--dry-run",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert '"calls_to_move": 2' in result.output
    assert '"dry_run": true' in result.output

    session.expire_all()
    assert session.get(Call, mislabeled["school_only"]).system_id == mislabeled["example"]


def test_the_cli_moves_the_slice(session, mislabeled) -> None:  # noqa: ANN001
    from typer.testing import CliRunner

    from app.cli import app as cli

    result = CliRunner().invoke(
        cli,
        [
            "refile-system",
            "--match", DISTRICT,
            "--to-system", "EXAMPLE SCHOOL DISTRICT",
            "--to-site", "Eastside",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert '"calls_moved": 2' in result.output

    session.expire_all()
    assert session.get(Call, mislabeled["school_only"]).system_id == mislabeled["district"]
    assert session.get(Call, mislabeled["school_shared"]).system_id == mislabeled["district"]
    # The county's own call did not move.
    assert session.get(Call, mislabeled["example_call"]).system_id == mislabeled["example"]


def test_the_cli_refuses_a_target_that_does_not_exist(session, mislabeled) -> None:  # noqa: ANN001, ARG001
    from typer.testing import CliRunner

    from app.cli import app as cli

    result = CliRunner().invoke(
        cli,
        [
            "refile-system",
            "--match", DISTRICT,
            "--to-system", "NO SUCH SYSTEM",
            "--to-site", "Eastside",
        ],
    )  # fmt: skip
    assert result.exit_code == 2
    assert "no system named" in result.output
