"""Retention: policy resolution, the audio tier and the row tier.

Every test writes real files under ``tmp_path`` and checks the filesystem as well as the
rows — the whole point of this module is that it moves and deletes bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.models import Call, RetentionPolicy, System, Talkgroup, Transcript
from app.retention import apply_retention, resolve_policies
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = requires_db


def make_settings(root: Path, **kw) -> Settings:  # noqa: ANN003
    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=root,
        tz="America/Winnipeg",
        **kw,
    )


def seed(
    session,  # noqa: ANN001
    root: Path,
    *,
    days_old: float,
    tgid: int = 1701,
    starred: bool = False,
    name: str | None = None,
    with_transcript: bool = False,
) -> Call:
    """A call whose MP3 really exists on disk, aged ``days_old`` days."""
    system = session.execute(select(System).where(System.name == "Example System")).scalar_one_or_none()
    if system is None:
        system = System(name="Example System")
        session.add(system)
        session.flush()
    talkgroup = session.execute(
        select(Talkgroup).where(Talkgroup.system_id == system.id, Talkgroup.tgid == tgid)
    ).scalar_one_or_none()
    if talkgroup is None:
        talkgroup = Talkgroup(system_id=system.id, tgid=tgid, alias=f"TG {tgid}")
        session.add(talkgroup)
        session.flush()

    started = datetime.now(UTC) - timedelta(days=days_old)
    relative = name or f"{tgid}/{started:%Y%m%d_%H%M%S}_{tgid}.mp3"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3" + b"\0" * 500)

    call = Call(
        system_id=system.id,
        talkgroup_id=talkgroup.id,
        talkgroup_ids=[tgid],
        recorded_at=started,
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=3),
        duration_s=Decimal("3.000"),
        file_path=relative,
        file_name=path.name,
        file_size=path.stat().st_size,
        starred=starred,
        transcript_status="done",
        transcript_text="engine one responding",
    )
    session.add(call)
    session.flush()
    if with_transcript:
        session.add(
            Transcript(
                call_id=call.id,
                engine="openai_compatible",
                text_="engine one responding",
                is_primary=True,
            )
        )
    session.commit()
    return call


# --------------------------------------------------------------------------------------
# policy resolution
# --------------------------------------------------------------------------------------


def test_the_default_policy_comes_from_settings(session, tmp_path) -> None:  # noqa: ANN001
    settings = make_settings(tmp_path, retention_keep_days=30, retention_keep_audio_days=7)
    default, by_system, by_talkgroup = resolve_policies(session, settings)
    assert (default.keep_days, default.keep_audio_days) == (30, 7)
    assert by_system == {} and by_talkgroup == {}


def test_the_most_specific_policy_wins(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=1)
    session.add_all(
        [
            RetentionPolicy(scope="global", keep_days=365, keep_audio_days=90),
            RetentionPolicy(scope="system", scope_id=call.system_id, keep_days=180, keep_audio_days=60),
            RetentionPolicy(
                scope="talkgroup", scope_id=call.talkgroup_id, keep_days=30, keep_audio_days=3
            ),
        ]
    )
    session.commit()

    default, by_system, by_talkgroup = resolve_policies(session, make_settings(tmp_path))

    assert default.keep_days == 365
    assert by_system[call.system_id].keep_days == 180
    assert by_talkgroup[call.talkgroup_id].keep_audio_days == 3


def test_a_talkgroups_own_retention_days_overrides_the_rest(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=1)
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    talkgroup.retention_days = 5
    session.commit()

    _, _, by_talkgroup = resolve_policies(session, make_settings(tmp_path, retention_keep_days=400))
    assert by_talkgroup[call.talkgroup_id].keep_days == 5


# --------------------------------------------------------------------------------------
# doing nothing
# --------------------------------------------------------------------------------------


def test_zero_everywhere_keeps_for_ever(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=4000)
    report = apply_retention(session, settings=make_settings(tmp_path))

    assert report.candidates == 0
    assert report.calls_deleted == 0
    assert session.get(Call, call.id) is not None
    assert (tmp_path / call.file_path).is_file()


def test_a_starred_call_is_never_touched(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=400, starred=True)
    settings = make_settings(tmp_path, retention_keep_days=30, retention_keep_audio_days=7)

    apply_retention(session, settings=settings)

    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored is not None
    assert stored.audio_state == "present"
    assert (tmp_path / call.file_path).is_file()


# --------------------------------------------------------------------------------------
# the audio tier
# --------------------------------------------------------------------------------------


def test_dry_run_reports_without_touching_anything(session, tmp_path) -> None:  # noqa: ANN001
    old = seed(session, tmp_path, days_old=40, name="old.mp3")
    young = seed(session, tmp_path, days_old=1, name="young.mp3")
    settings = make_settings(tmp_path, retention_keep_audio_days=30)

    report = apply_retention(session, settings=settings, dry_run=True)

    assert report.dry_run is True
    assert report.audio_deleted == 1
    assert report.candidates == 1  # only the 40-day-old call is even a candidate
    assert (tmp_path / old.file_path).is_file()
    assert (tmp_path / young.file_path).is_file()
    session.expire_all()
    assert session.get(Call, old.id).audio_state == "present"


def test_audio_is_moved_to_the_archive_directory(session, tmp_path) -> None:  # noqa: ANN001
    archive = tmp_path.parent / "archive"
    archive.mkdir(exist_ok=True)  # the mount: retention never creates an archive root
    call = seed(session, tmp_path, days_old=40)
    settings = make_settings(
        tmp_path, retention_keep_audio_days=30, retention_archive_path=str(archive)
    )
    relative = Path(call.file_path)
    call_id = call.id
    original = tmp_path / relative

    report = apply_retention(session, settings=settings)

    assert report.audio_archived == 1
    assert not original.exists()
    # L11: the leaf carries the call id, so two systems whose recorders produce the same
    # filename cannot silently overwrite each other inside the archive.
    moved = archive / relative.parent / f"{call_id}-{relative.name}"
    assert moved.is_file()

    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored.audio_state == "archived"
    assert stored.audio_archived_at is not None
    # file_path is absolute now, so ``audio_root / file_path`` still resolves for the API
    assert Path(stored.file_path) == moved
    assert tmp_path / stored.file_path == moved
    assert stored.transcript_text == "engine one responding"  # metadata survives


def test_audio_is_deleted_when_no_archive_is_configured(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=40)
    settings = make_settings(tmp_path, retention_keep_audio_days=30)

    report = apply_retention(session, settings=settings)

    assert report.audio_deleted == 1
    assert report.bytes_freed > 0
    assert not (tmp_path / call.file_path).exists()
    session.expire_all()
    assert session.get(Call, call.id).audio_state == "purged"


def test_purging_audio_can_drop_the_transcript_too(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=40, with_transcript=True)
    settings = make_settings(
        tmp_path, retention_keep_audio_days=30, retention_keep_transcripts=False
    )

    report = apply_retention(session, settings=settings)

    assert report.transcripts_deleted == 1
    session.expire_all()
    assert session.execute(select(Transcript).where(Transcript.call_id == call.id)).first() is None
    assert session.get(Call, call.id).transcript_text is None


def test_a_second_pass_leaves_already_purged_audio_alone(session, tmp_path) -> None:  # noqa: ANN001
    seed(session, tmp_path, days_old=40)
    settings = make_settings(tmp_path, retention_keep_audio_days=30)

    apply_retention(session, settings=settings)
    second = apply_retention(session, settings=settings)

    assert second.audio_deleted == 0
    assert second.errors == []


# --------------------------------------------------------------------------------------
# the row tier
# --------------------------------------------------------------------------------------


def test_calls_past_keep_days_are_deleted_with_their_audio(session, tmp_path) -> None:  # noqa: ANN001
    doomed = seed(session, tmp_path, days_old=400, name="doomed.mp3", with_transcript=True)
    kept = seed(session, tmp_path, days_old=10, name="kept.mp3")
    settings = make_settings(tmp_path, retention_keep_days=365)

    report = apply_retention(session, settings=settings)

    assert report.calls_deleted == 1
    session.expire_all()
    assert session.get(Call, doomed.id) is None
    assert session.get(Call, kept.id) is not None
    assert not (tmp_path / doomed.file_path).exists()
    # the transcript went with it (ON DELETE CASCADE)
    assert session.execute(select(Transcript).where(Transcript.call_id == doomed.id)).first() is None


def test_a_per_talkgroup_policy_deletes_only_its_own_traffic(session, tmp_path) -> None:  # noqa: ANN001
    short_lived = seed(session, tmp_path, days_old=20, tgid=1701, name="a.mp3")
    long_lived = seed(session, tmp_path, days_old=20, tgid=1102, name="b.mp3")
    session.add(
        RetentionPolicy(scope="talkgroup", scope_id=short_lived.talkgroup_id, keep_days=7)
    )
    session.commit()

    report = apply_retention(session, settings=make_settings(tmp_path))

    assert report.calls_deleted == 1
    session.expire_all()
    assert session.get(Call, short_lived.id) is None
    assert session.get(Call, long_lived.id) is not None


def test_a_missing_file_is_reported_not_fatal(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=40)
    (tmp_path / call.file_path).unlink()

    report = apply_retention(session, settings=make_settings(tmp_path, retention_keep_audio_days=30))

    assert report.audio_missing == 1
    assert report.errors == []
    session.expire_all()
    # ``missing``, not ``purged``: nobody decided this file should go (R4)
    assert session.get(Call, call.id).audio_state == "missing"


def test_a_file_path_outside_the_archive_is_never_unlinked(session, tmp_path) -> None:  # noqa: ANN001
    """This pass deletes files, so the row's path is checked, not believed."""
    outsider = tmp_path.parent / "not-the-archive.mp3"
    outsider.write_bytes(b"\xff\xfb\x90d" + b"\x00" * 64)
    call = seed(session, tmp_path, days_old=40)
    call.file_path = str(outsider)
    session.commit()
    settings = make_settings(tmp_path, retention_keep_audio_days=30)

    report = apply_retention(session, settings=settings)

    assert outsider.is_file()
    assert report.audio_deleted == 0
    assert any("outside the archive" in e for e in report.errors)


# --------------------------------------------------------------------------------------
# radio events — a clock of their own (docs/RADIO_EVENTS.md, "retention and privacy")
# --------------------------------------------------------------------------------------


def seed_events(session, *, ages_days: list[float]) -> int:  # noqa: ANN001
    """One ``radio_events`` row per age, all for the same radio."""
    from app.models import RadioEvent

    system = session.execute(select(System).where(System.name == "Example System")).scalar_one_or_none()
    if system is None:
        system = System(name="Example System")
        session.add(system)
        session.flush()
    now = datetime.now(UTC)
    for index, days in enumerate(ages_days):
        session.add(
            RadioEvent(
                at=now - timedelta(days=days),
                system_id=system.id,
                kind="unit_registration",
                opcode=44,
                radio_id=5201234,
                # the identity index is (site, at, kind, opcode, radio, talkgroup) and
                # ``at`` alone separates these
                talkgroup_tgid=index,
            )
        )
    session.commit()
    return len(ages_days)


def test_radio_events_are_purged_on_their_own_clock(session, tmp_path) -> None:  # noqa: ANN001
    from app.models import RadioEvent

    seed_events(session, ages_days=[1, 30, 89, 91, 400])
    settings = make_settings(tmp_path, radio_events_keep_days=90)

    # Every *call* policy keeps for ever — the presence purge still runs.
    report = apply_retention(session, settings=settings, audio_root=tmp_path)

    assert report.radio_events_deleted == 2
    assert report.as_dict()["radio_events_deleted"] == 2
    remaining = session.scalars(select(RadioEvent.at)).all()
    assert len(remaining) == 3
    assert all((datetime.now(UTC) - at).days < 90 for at in remaining)


def test_a_dry_run_counts_radio_events_without_deleting_them(session, tmp_path) -> None:  # noqa: ANN001
    from app.models import RadioEvent

    seed_events(session, ages_days=[91, 400])
    settings = make_settings(tmp_path, radio_events_keep_days=90)

    report = apply_retention(session, settings=settings, audio_root=tmp_path, dry_run=True)

    assert report.radio_events_deleted == 2
    assert len(session.scalars(select(RadioEvent.id)).all()) == 2


def test_zero_keeps_radio_events_for_ever(session, tmp_path) -> None:  # noqa: ANN001
    from app.models import RadioEvent

    seed_events(session, ages_days=[400])
    settings = make_settings(tmp_path, radio_events_keep_days=0)

    report = apply_retention(session, settings=settings, audio_root=tmp_path)

    assert report.radio_events_deleted == 0
    assert len(session.scalars(select(RadioEvent.id)).all()) == 1


# --------------------------------------------------------------------------------------
# the activity log — a fourth clock, and the only one that is about volume
# --------------------------------------------------------------------------------------


#: A kind no application code emits.  The ``session`` fixture truncates every table, so
#: these tests would count correctly without it — but ``activity_events`` is unlike
#: ``radio_events`` in that application code writes to it *incidentally*, and a retention
#: pass that one day records its own "purged N rows" line should not silently change what
#: these assertions mean.  Counting only the rows this module seeded is a cent's worth of
#: insurance against that.
MARKER_KIND = "test.retention.marker"


def seed_activity(session, *, ages_days: list[float]) -> int:  # noqa: ANN001
    """One ``activity_events`` row per age, tagged with :data:`MARKER_KIND`."""
    from app.models import ActivityEvent

    now = datetime.now(UTC)
    for index, days in enumerate(ages_days):
        session.add(
            ActivityEvent(
                at=now - timedelta(days=days),
                source="ingest",
                level="info",
                kind=MARKER_KIND,
                message=f"row {index}",
            )
        )
    session.commit()
    return len(ages_days)


def marker_rows(session):  # noqa: ANN001, ANN201
    """The ``at`` of every row this module seeded, and nothing else's."""
    from app.models import ActivityEvent

    return session.scalars(
        select(ActivityEvent.at).where(ActivityEvent.kind == MARKER_KIND)
    ).all()


def test_activity_events_are_purged_on_their_own_clock(session, tmp_path) -> None:  # noqa: ANN001
    seed_activity(session, ages_days=[1, 29, 31, 400])
    settings = make_settings(tmp_path, activity_retention_days=30)

    # As with the radio events: every *call* policy here keeps for ever, and the log is
    # still bounded.  That is the whole point of it being a separate clock.
    report = apply_retention(session, settings=settings, audio_root=tmp_path)

    assert report.activity_events_deleted == 2
    assert report.as_dict()["activity_events_deleted"] == 2
    remaining = marker_rows(session)
    assert len(remaining) == 2
    assert all((datetime.now(UTC) - at).days < 30 for at in remaining)


def test_a_dry_run_counts_activity_events_without_deleting_them(session, tmp_path) -> None:  # noqa: ANN001
    seed_activity(session, ages_days=[31, 400])
    settings = make_settings(tmp_path, activity_retention_days=30)

    report = apply_retention(session, settings=settings, audio_root=tmp_path, dry_run=True)

    assert report.activity_events_deleted == 2
    # the point of the test: nothing went
    assert len(marker_rows(session)) == 2


def test_zero_keeps_activity_events_for_ever(session, tmp_path) -> None:  # noqa: ANN001
    seed_activity(session, ages_days=[400])
    settings = make_settings(tmp_path, activity_retention_days=0)

    report = apply_retention(session, settings=settings, audio_root=tmp_path)

    assert report.activity_events_deleted == 0
    assert len(marker_rows(session)) == 1


def test_the_activity_purge_batches_rather_than_deleting_in_one_statement(  # noqa: ANN201
    session,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
):
    """More rows than one batch holds still all go, and in more than one statement.

    The batching is the reason this function exists rather than the single DELETE the
    activity module already had, so it is worth a test that would fail if somebody
    "simplified" it back.  ``ACTIVITY_BATCH`` is patched down to 3 instead of seeding ten
    thousand rows — the loop is what is under test, not Postgres's ability to count.
    """
    from app import retention as retention_module

    monkeypatch.setattr(retention_module, "ACTIVITY_BATCH", 3)

    statements: list[int] = []
    original = retention_module.delete

    def counting_delete(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        statements.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(retention_module, "delete", counting_delete)

    seed_activity(session, ages_days=[40] * 7 + [1])
    settings = make_settings(tmp_path, activity_retention_days=30)

    removed = retention_module.purge_activity_events(
        session, settings=settings, now=datetime.now(UTC)
    )

    assert removed == 7
    # 7 doomed rows at 3 per batch: two full batches and a short one that ends the loop.
    assert len(statements) == 3
    assert len(marker_rows(session)) == 1


# --------------------------------------------------------------------------------------
# Retention safety
# --------------------------------------------------------------------------------------


def _archive_dir(tmp_path: Path, name: str = "archive") -> Path:
    """An archive root beside the audio root — existing, because it stands for a mount."""
    path = tmp_path.parent / f"{tmp_path.name}-{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_a_talkgroup_retention_days_override_keeps_its_system_policy(session, tmp_path) -> None:  # noqa: ANN001
    """R2: the column overrides keep_days only — the system's audio clock and archive stay."""
    archive = _archive_dir(tmp_path)
    call = seed(session, tmp_path, days_old=1)
    session.add(
        RetentionPolicy(
            scope="system",
            scope_id=call.system_id,
            keep_days=365,
            keep_audio_days=30,
            keep_transcripts=False,
            archive_path=str(archive),
        )
    )
    talkgroup = session.get(Talkgroup, call.talkgroup_id)
    talkgroup.retention_days = 90
    session.commit()

    _, by_system, by_talkgroup = resolve_policies(session, make_settings(tmp_path))
    policy = by_talkgroup[call.talkgroup_id]

    assert policy.keep_days == 90
    assert policy.keep_audio_days == 30
    assert policy.keep_transcripts is False
    assert policy.archive_path == str(archive)
    assert by_system[call.system_id].keep_days == 365


def test_a_policys_archive_path_stays_playable_and_is_deleted_when_the_row_expires(  # noqa: ANN201
    session,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
):
    """R1: a policy's own archive path is an allowed root — for playback and for the unlink."""
    from app.audio import audio_path, resolve_audio
    from app.runtime_settings import effective_settings

    archive = _archive_dir(tmp_path, "policy-archive")
    call = seed(session, tmp_path, days_old=40)
    session.add(
        RetentionPolicy(scope="system", scope_id=call.system_id, keep_audio_days=30, archive_path=str(archive))
    )
    session.commit()

    report = apply_retention(session, settings=make_settings(tmp_path))
    assert report.audio_archived == 1, report.errors

    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored.audio_state == "archived"
    moved = Path(stored.file_path)
    assert moved.is_file() and moved.is_relative_to(archive)
    # the pass remembered the path, so the runtime settings every reader uses allow it
    runtime = effective_settings(session, base=make_settings(tmp_path))
    assert str(archive.resolve()) in runtime.retention_archive_roots
    assert resolve_audio(stored, runtime) == moved.resolve()

    # The policy goes away and a row clock arrives: the archived file is still inside an
    # allowed root, so it is unlinked with the row rather than refused and orphaned.
    session.query(RetentionPolicy).delete()
    session.commit()
    report = apply_retention(session, settings=make_settings(tmp_path, retention_keep_days=35))
    assert report.calls_deleted == 1
    assert not any("outside the archive" in e for e in report.errors), report.errors
    assert not moved.exists()
    assert audio_path(stored, runtime) is not None


def test_a_changed_global_archive_path_does_not_orphan_what_was_archived_under_the_old_one(  # noqa: ANN201
    session,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
):
    from app.audio import resolve_audio
    from app.runtime_settings import effective_settings

    first = _archive_dir(tmp_path, "first")
    second = _archive_dir(tmp_path, "second")
    call = seed(session, tmp_path, days_old=40)
    apply_retention(
        session, settings=make_settings(tmp_path, retention_keep_audio_days=30, retention_archive_path=str(first))
    )
    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored.audio_state == "archived"

    # the operator points the global path somewhere else
    runtime = effective_settings(
        session, base=make_settings(tmp_path, retention_archive_path=str(second))
    )
    assert resolve_audio(stored, runtime).is_relative_to(first.resolve())


def test_an_absent_archive_directory_is_never_created_and_the_audio_stays(session, tmp_path) -> None:  # noqa: ANN001
    absent = tmp_path.parent / f"{tmp_path.name}-unmounted"
    call = seed(session, tmp_path, days_old=40)
    settings = make_settings(tmp_path, retention_keep_audio_days=30, retention_archive_path=str(absent))

    report = apply_retention(session, settings=settings)

    assert report.audio_archived == 0 and report.audio_deleted == 0
    assert any("not an existing directory" in e for e in report.errors)
    assert not absent.exists()
    assert (tmp_path / call.file_path).is_file()
    session.expire_all()
    assert session.get(Call, call.id).audio_state == "present"


def test_an_empty_audio_root_stops_the_pass_before_anything_changes(session, tmp_path) -> None:  # noqa: ANN001
    """R4: an unmounted volume looks like an empty directory; nothing is purged or deleted."""
    from app.models import ActivityEvent

    mounted = tmp_path / "audio"
    mounted.mkdir()
    doomed = seed(session, mounted, days_old=400, name="doomed.mp3")
    aged = seed(session, mounted, days_old=40, name="aged.mp3")
    for path in mounted.iterdir():
        path.unlink()  # the share is gone

    settings = make_settings(mounted, retention_keep_days=365, retention_keep_audio_days=30)
    report = apply_retention(session, settings=settings)

    assert report.aborted and "empty" in report.aborted
    session.expire_all()
    assert session.get(Call, doomed.id) is not None
    assert session.get(Call, aged.id).audio_state == "present"
    kinds = [e.kind for e in session.scalars(select(ActivityEvent))]
    assert "retention.aborted" in kinds


def test_too_many_missing_files_in_a_batch_stops_the_pass(session, tmp_path) -> None:  # noqa: ANN001
    """R4: past the threshold the batch is left exactly as it was — no ``missing``, no ``purged``."""
    (tmp_path / "keep.txt").write_text("the root is mounted")
    calls = [seed(session, tmp_path, days_old=40, name=f"gone-{i}.mp3") for i in range(10)]
    for call in calls:
        (tmp_path / call.file_path).unlink()

    report = apply_retention(session, settings=make_settings(tmp_path, retention_keep_audio_days=30))

    assert report.aborted and "missing" in report.aborted
    session.expire_all()
    assert {session.get(Call, c.id).audio_state for c in calls} == {"present"}


def test_too_many_missing_files_among_calls_due_for_deletion_keeps_the_rows(session, tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "keep.txt").write_text("the root is mounted")
    calls = [seed(session, tmp_path, days_old=400, name=f"old-{i}.mp3") for i in range(10)]
    for call in calls[:9]:
        (tmp_path / call.file_path).unlink()

    report = apply_retention(session, settings=make_settings(tmp_path, retention_keep_days=365))

    assert report.aborted
    assert report.calls_deleted == 0
    session.expire_all()
    assert all(session.get(Call, c.id) is not None for c in calls)
    # the one file that was there is still there: the DELETE rolled back before any unlink
    assert (tmp_path / calls[9].file_path).is_file()


def test_a_missing_file_that_comes_back_is_retired_on_the_next_pass(session, tmp_path) -> None:  # noqa: ANN001
    call = seed(session, tmp_path, days_old=40)
    original = tmp_path / call.file_path
    stash = tmp_path.parent / f"{tmp_path.name}-stash.mp3"
    original.rename(stash)
    settings = make_settings(tmp_path, retention_keep_audio_days=30)

    apply_retention(session, settings=settings)
    session.expire_all()
    assert session.get(Call, call.id).audio_state == "missing"

    stash.rename(original)  # the mount is back
    report = apply_retention(session, settings=settings)
    assert report.audio_deleted == 1
    session.expire_all()
    assert session.get(Call, call.id).audio_state == "purged"
    assert not original.exists()


def test_a_move_whose_row_never_committed_is_repaired_from_the_archive(session, tmp_path) -> None:  # noqa: ANN001
    """R3: the file already sits at its archive target; the row still says ``present``."""
    from app.retention import _archive_target

    archive = _archive_dir(tmp_path)
    call = seed(session, tmp_path, days_old=40)
    source = tmp_path / call.file_path
    target = _archive_target(call, source, tmp_path, archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)  # what a crash between the move and the commit leaves behind

    report = apply_retention(
        session, settings=make_settings(tmp_path, retention_keep_audio_days=30, retention_archive_path=str(archive))
    )

    assert report.audio_archived == 1 and report.audio_missing == 0
    session.expire_all()
    stored = session.get(Call, call.id)
    assert stored.audio_state == "archived"
    assert Path(stored.file_path) == target


def test_each_moved_file_is_committed_before_the_next_is_touched(session, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """R3: a failure on the second file leaves the first one's row saying where it went."""
    import shutil as shutil_module

    from app import retention as retention_module
    from app.db import SessionLocal

    archive = _archive_dir(tmp_path)
    first = seed(session, tmp_path, days_old=41, name="first.mp3")
    second = seed(session, tmp_path, days_old=40, name="second.mp3")
    moves: list[str] = []
    real_move = shutil_module.move

    def failing_move(src, dst):  # noqa: ANN001, ANN202
        moves.append(src)
        if len(moves) == 2:
            raise RuntimeError("the process died here")
        return real_move(src, dst)

    monkeypatch.setattr(retention_module.shutil, "move", failing_move)
    settings = make_settings(tmp_path, retention_keep_audio_days=30, retention_archive_path=str(archive))

    import pytest

    with pytest.raises(RuntimeError):
        apply_retention(session, settings=settings)
    session.rollback()

    fresh = SessionLocal()
    try:
        done = fresh.get(Call, first.id)
        assert done.audio_state == "archived"
        assert Path(done.file_path).is_file()
        untouched = fresh.get(Call, second.id)
        assert untouched.audio_state == "present"
        assert (tmp_path / untouched.file_path).is_file()
    finally:
        fresh.close()


def test_a_call_starred_after_the_walk_is_not_deleted(session, tmp_path) -> None:  # noqa: ANN001
    """R5: the star is re-checked in the DELETE itself, and its file is left alone."""
    from app.retention import RetentionReport, _delete_batch

    call = seed(session, tmp_path, days_old=400)
    session.get(Call, call.id).starred = True  # the star lands between the walk and the delete
    session.commit()

    report = RetentionReport()
    _delete_batch(session, [call.id], make_settings(tmp_path), report)

    assert report.calls_deleted == 0
    session.expire_all()
    assert session.get(Call, call.id) is not None
    assert (tmp_path / call.file_path).is_file()


def test_a_call_starred_after_the_walk_keeps_its_audio(session, tmp_path) -> None:  # noqa: ANN001
    from app.retention import Policy, RetentionReport, _retire_audio

    call = seed(session, tmp_path, days_old=40)
    session.get(Call, call.id).starred = True
    session.commit()

    report = RetentionReport()
    _retire_audio(
        session, call.id, Policy(keep_audio_days=30), tmp_path, make_settings(tmp_path), report, dry_run=False
    )

    assert report.audio_deleted == 0
    assert (tmp_path / call.file_path).is_file()
    session.expire_all()
    assert session.get(Call, call.id).audio_state == "present"


def test_rows_go_first_and_files_after(session, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """R5: nothing is unlinked until the DELETE has committed."""
    from app import retention as retention_module

    call = seed(session, tmp_path, days_old=400)
    path = tmp_path / call.file_path
    seen: list[bool] = []
    real_commit = session.commit

    def watching_commit() -> None:
        seen.append(path.exists())
        real_commit()

    monkeypatch.setattr(session, "commit", watching_commit)
    report = retention_module.RetentionReport()
    retention_module._delete_batch(session, [call.id], make_settings(tmp_path), report)

    assert seen and seen[0] is True  # the file was still there when the row went
    assert not path.exists()
    assert report.calls_deleted == 1


def test_validate_archive_path(tmp_path) -> None:  # noqa: ANN001
    import pytest

    from app.retention import validate_archive_path

    allowed = tmp_path / "srv"
    audio = allowed / "audio"
    good = allowed / "archive"
    for path in (audio, good):
        path.mkdir(parents=True)
    settings = make_settings(audio, retention_archive_allowed_roots=str(allowed), export_dir=allowed / "exports")

    assert validate_archive_path(str(good), settings) == str(good.resolve())
    for bad, why in [
        ("archive", "absolute"),
        (str(audio / "old"), "overlaps AUDIO_ROOT"),
        (str(allowed), "overlaps AUDIO_ROOT"),
        (str(allowed / "absent"), "existing directory"),
        (str(tmp_path), "overlaps AUDIO_ROOT"),
    ]:
        with pytest.raises(ValueError, match=why):
            validate_archive_path(bad, settings)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError, match="allowed archive location"):
        validate_archive_path(str(outside), settings)
    (allowed / "exports").mkdir()
    with pytest.raises(ValueError, match="EXPORT_DIR"):
        validate_archive_path(str(allowed / "exports"), settings)


# --------------------------------------------------------------------------------------
# The walk reads only what a clock can act on; purges are set-based
# --------------------------------------------------------------------------------------


def test_the_walk_does_not_read_audio_it_already_retired(session, tmp_path) -> None:  # noqa: ANN001
    """A purged or archived recording is not a candidate again: only row deletion could want it."""
    seed(session, tmp_path, days_old=40)
    settings = make_settings(tmp_path, retention_keep_audio_days=30)

    first = apply_retention(session, settings=settings)
    second = apply_retention(session, settings=settings)

    assert first.candidates == 1 and first.audio_deleted == 1
    assert second.candidates == 0
    # …but a row clock still reaches it
    third = apply_retention(session, settings=make_settings(tmp_path, retention_keep_audio_days=30, retention_keep_days=35))
    assert third.candidates == 1 and third.calls_deleted == 1


def test_the_walk_pages_through_ties_and_out_of_order_ids(session, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Keyset on (started_at, id): calls sharing a start time, and ids that are not in time
    order (a backfilled archive), are each visited once across batch boundaries."""
    from app import retention as retention_module

    monkeypatch.setattr(retention_module, "BATCH", 2)
    calls = [seed(session, tmp_path, days_old=40, name=f"tie-{i}.mp3") for i in range(5)]
    older = seed(session, tmp_path, days_old=41, name="older.mp3")  # the highest id, the oldest call
    tie = calls[0].started_at
    for call in calls:
        session.get(Call, call.id).started_at = tie
    session.commit()

    report = apply_retention(session, settings=make_settings(tmp_path, retention_keep_audio_days=30))

    assert report.candidates == 6
    assert report.audio_deleted == 6
    session.expire_all()
    assert {session.get(Call, c.id).audio_state for c in [*calls, older]} == {"purged"}


def test_a_batch_of_purges_commits_once_and_unlinks_after(session, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app import retention as retention_module
    from app.retention import Policy, RetentionReport

    calls = [seed(session, tmp_path, days_old=40, name=f"p-{i}.mp3", with_transcript=True) for i in range(3)]
    paths = [tmp_path / c.file_path for c in calls]
    seen: list[list[bool]] = []
    real_commit = session.commit

    def watching_commit() -> None:
        seen.append([p.exists() for p in paths])
        real_commit()

    monkeypatch.setattr(session, "commit", watching_commit)
    report = RetentionReport()
    policy = Policy(keep_audio_days=30, keep_transcripts=False)
    retention_module._purge_audio(
        session, [(c.id, policy) for c in calls], make_settings(tmp_path), report, dry_run=False
    )

    assert seen == [[True, True, True]]  # one commit, and every file was still there for it
    assert not any(p.exists() for p in paths)
    assert report.audio_deleted == 3 and report.transcripts_deleted == 3
    session.expire_all()
    for call in calls:
        stored = session.get(Call, call.id)
        assert stored.audio_state == "purged"
        assert stored.audio_archived_at is not None
        assert stored.transcript_text is None
    assert session.execute(select(Transcript).where(Transcript.call_id.in_([c.id for c in calls]))).first() is None


def test_a_purge_whose_unlink_fails_puts_the_row_back(session, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    stuck = seed(session, tmp_path, days_old=40, name="stuck.mp3")
    fine = seed(session, tmp_path, days_old=40, name="fine.mp3")
    stuck_path = tmp_path / stuck.file_path
    real_unlink = Path.unlink

    def refusing_unlink(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if self == stuck_path:
            raise PermissionError("read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)
    report = apply_retention(session, settings=make_settings(tmp_path, retention_keep_audio_days=30))

    assert report.audio_deleted == 1
    assert any("purge" in e and "read-only" in e for e in report.errors)
    session.expire_all()
    assert session.get(Call, stuck.id).audio_state == "present"  # the next pass tries again
    assert session.get(Call, stuck.id).audio_archived_at is None
    assert stuck_path.is_file()
    assert session.get(Call, fine.id).audio_state == "purged"
