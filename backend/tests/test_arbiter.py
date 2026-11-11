"""The standby arbiter: promotion, demotion, and everything it must refuse to touch.

The arbiter switches other people's receivers on and off, so most of what is worth testing
is what it *does not* do: a cold box is never touched, a spare an operator enabled by hand
is never stood down, a hot recorder that is merely slow is not failed over, and a pass that
changes nothing writes nothing at all.

Time is always passed in explicitly (``run(..., now=…)``) rather than slept through: the
hysteresis is a five-minute window and a test that waited for it would be a test nobody
runs.  Each case builds its recorders, commits, and then drives the passes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.arbiter import ARBITER_LABEL, PROMOTED_KEY, Arbiter
from app.models import ActivityEvent, Recorder, Tuner
from tests.conftest import requires_db

pytestmark = requires_db

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SITE = "Central"


@pytest.fixture
def arbiter_settings(settings):  # noqa: ANN001, ANN201
    """The defaults, with the switch on — the tests that need it off say so."""
    return settings.model_copy(
        update={
            "arbiter_enabled": True,
            "arbiter_stale_seconds": 60,
            "arbiter_recovery_seconds": 300,
        }
    )


def make_recorder(  # noqa: PLR0913
    session,  # noqa: ANN001
    instance: str,
    *,
    standby: str = "hot",
    priority: int = 100,
    reported_ago: float | None = 5,
    at: datetime = T0,
    site: str | None = SITE,
    enabled: bool | None = None,
    tuner_key: str = "rtl-0",
    promoted_at: datetime | None = None,
) -> Recorder:
    """One recorder with one tuner on ``site``.

    ``reported_ago=None`` is a recorder that has registered and never reported — which is
    down, not merely quiet.  ``enabled=None`` leaves ``desired`` empty, the state of a tuner
    nobody has ever edited (and therefore enabled).
    """
    recorder = Recorder(
        instance_id=instance,
        standby=standby,
        priority=priority,
        promoted_at=promoted_at,
        reported_at=None if reported_ago is None else at - timedelta(seconds=reported_ago),
    )
    session.add(recorder)
    session.flush()
    session.add(
        Tuner(
            recorder_id=recorder.id,
            tuner_key=tuner_key,
            reported={"site": site, "role": "control"} if site else {},
            reported_at=recorder.reported_at,
            desired=None if enabled is None else {"site": site, "enabled": enabled},
            desired_version=0 if enabled is None else 1,
        )
    )
    session.commit()
    return recorder


def add_tuner(  # noqa: ANN001
    session,
    recorder: Recorder,
    tuner_key: str,
    *,
    site: str,
    desired: dict | None = None,
) -> None:
    """A second tuner on a recorder that already has one — a box covering two sites."""
    session.add(
        Tuner(
            recorder_id=recorder.id,
            tuner_key=tuner_key,
            reported={"site": site, "role": "control"},
            reported_at=recorder.reported_at,
            desired=desired,
            desired_version=0 if desired is None else 1,
        )
    )
    session.commit()


def tuner_of(session, recorder: Recorder) -> Tuner:  # noqa: ANN001
    session.expire_all()
    return session.scalars(select(Tuner).where(Tuner.recorder_id == recorder.id)).one()


def tuner_named(session, recorder: Recorder, tuner_key: str) -> Tuner:  # noqa: ANN001
    session.expire_all()
    return session.scalars(
        select(Tuner).where(Tuner.recorder_id == recorder.id, Tuner.tuner_key == tuner_key)
    ).one()


def desired_of(session, recorder: Recorder, tuner_key: str) -> dict:  # noqa: ANN001
    return tuner_named(session, recorder, tuner_key).desired or {}


def enabled_of(session, recorder: Recorder) -> bool | None:  # noqa: ANN001
    return (tuner_of(session, recorder).desired or {}).get("enabled")


def kinds(session) -> list[str]:  # noqa: ANN001
    return list(session.scalars(select(ActivityEvent.kind).order_by(ActivityEvent.id)))


def fresh(session, recorder: Recorder) -> Recorder:  # noqa: ANN001
    session.expire_all()
    return session.get(Recorder, recorder.id)


# --------------------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------------------


def test_a_disabled_arbiter_does_not_even_look(session, settings) -> None:  # noqa: ANN001
    make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    report = Arbiter().run(session, settings.model_copy(update={"arbiter_enabled": False}), now=T0)

    assert report.ran is False
    assert report.changed is False
    assert enabled_of(session, warm) is False
    assert kinds(session) == []


def test_a_pass_that_changes_nothing_is_silent(session, arbiter_settings) -> None:  # noqa: ANN001
    """Everything healthy, nothing to do: no activity rows, no desired-version bumps."""
    hot = make_recorder(session, "hot-1")
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    report = Arbiter().run(session, arbiter_settings, now=T0)

    assert (report.promoted, report.demoted, report.changed) == ([], [], False)
    assert kinds(session) == []
    assert tuner_of(session, hot).desired_version == 0
    assert tuner_of(session, warm).desired_version == 1  # unchanged from its fixture


# --------------------------------------------------------------------------------------
# promotion
# --------------------------------------------------------------------------------------


def test_promotes_a_warm_spare_when_every_hot_recorder_is_stale(session, arbiter_settings) -> None:  # noqa: ANN001
    make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    report = Arbiter().run(session, arbiter_settings, now=T0)

    assert report.promoted == [(SITE, "warm-1")]
    assert enabled_of(session, warm) is True
    assert fresh(session, warm).promoted_at == T0
    # Written through the same locked path the operator's PUT uses, and it says who did it.
    tuner = tuner_of(session, warm)
    assert (tuner.desired_version, tuner.updated_by) == (2, ARBITER_LABEL)
    assert fresh(session, warm).version == 1
    assert kinds(session) == ["recorder.promoted"]


def test_one_healthy_hot_recorder_is_enough_to_hold_the_site(session, arbiter_settings) -> None:  # noqa: ANN001
    make_recorder(session, "hot-1", reported_ago=600)
    make_recorder(session, "hot-2", reported_ago=5)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    assert Arbiter().run(session, arbiter_settings, now=T0).promoted == []
    assert enabled_of(session, warm) is False


def test_a_recorder_that_has_never_reported_is_down(session, arbiter_settings) -> None:  # noqa: ANN001
    """Registered and silent is not healthy: it has told us nothing about hearing anything."""
    make_recorder(session, "hot-1", reported_ago=None)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    assert Arbiter().run(session, arbiter_settings, now=T0).promoted == [(SITE, "warm-1")]
    assert enabled_of(session, warm) is True


def test_the_lowest_priority_value_wins_and_ties_go_to_the_older_row(session, arbiter_settings) -> None:  # noqa: ANN001
    make_recorder(session, "hot-1", reported_ago=600)
    late = make_recorder(session, "warm-late", standby="warm", priority=5, enabled=False)
    high = make_recorder(session, "warm-high", standby="warm", priority=50, enabled=False)

    assert Arbiter().run(session, arbiter_settings, now=T0).promoted == [(SITE, "warm-late")]
    assert enabled_of(session, late) is True
    assert enabled_of(session, high) is False  # only one spare is put on the site


def test_a_second_pass_does_not_promote_a_second_spare(session, arbiter_settings) -> None:  # noqa: ANN001
    make_recorder(session, "hot-1", reported_ago=600)
    first = make_recorder(session, "warm-1", standby="warm", priority=1, enabled=False)
    second = make_recorder(session, "warm-2", standby="warm", priority=2, enabled=False)

    arbiter = Arbiter()
    arbiter.run(session, arbiter_settings, now=T0)
    report = arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=30))

    assert report.changed is False
    assert enabled_of(session, first) is True
    assert enabled_of(session, second) is False
    assert kinds(session) == ["recorder.promoted"]


def test_a_cold_recorder_is_never_promoted(session, arbiter_settings) -> None:  # noqa: ANN001
    """A bench box stays a bench box, however badly the site needs covering."""
    make_recorder(session, "hot-1", reported_ago=600)
    cold = make_recorder(session, "cold-1", standby="cold", priority=1, enabled=False)

    report = Arbiter().run(session, arbiter_settings, now=T0)

    assert report.promoted == []
    assert report.uncovered == [SITE]
    assert enabled_of(session, cold) is False
    assert tuner_of(session, cold).desired_version == 1
    assert kinds(session) == []


def test_a_stale_warm_spare_is_no_use(session, arbiter_settings) -> None:  # noqa: ANN001
    make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", reported_ago=600, enabled=False)

    assert Arbiter().run(session, arbiter_settings, now=T0).uncovered == [SITE]
    assert enabled_of(session, warm) is False


def test_a_spare_that_has_never_heard_the_site_is_not_a_candidate(session, arbiter_settings) -> None:  # noqa: ANN001
    make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", site="Gardner", enabled=False)

    assert Arbiter().run(session, arbiter_settings, now=T0).promoted == []
    assert enabled_of(session, warm) is False


def test_a_site_no_hot_recorder_owns_is_not_arbitrated(session, arbiter_settings) -> None:  # noqa: ANN001
    """Promoting a spare for a site nothing normally covers invents coverage, not restores it."""
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    report = Arbiter().run(session, arbiter_settings, now=T0)

    assert (report.promoted, report.uncovered) == ([], [])
    assert enabled_of(session, warm) is False


# --------------------------------------------------------------------------------------
# demotion
# --------------------------------------------------------------------------------------


def recover(session, recorder: Recorder, at: datetime) -> None:  # noqa: ANN001
    """The hot recorder starts reporting again, as of ``at``."""
    row = session.get(Recorder, recorder.id)
    row.reported_at = at
    session.commit()


def test_demotes_only_after_the_whole_recovery_window(session, arbiter_settings) -> None:  # noqa: ANN001
    hot = make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    arbiter = Arbiter()
    arbiter.run(session, arbiter_settings, now=T0)
    assert enabled_of(session, warm) is True

    # The hot recorder comes back.  The clock starts *now*, not when it says it reported.
    recover(session, hot, T0 + timedelta(seconds=60))
    assert arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=60)).demoted == []
    assert enabled_of(session, warm) is True

    # Four minutes back is not five.
    recover(session, hot, T0 + timedelta(seconds=300))
    assert arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=300)).demoted == []
    assert enabled_of(session, warm) is True

    recover(session, hot, T0 + timedelta(seconds=400))
    report = arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=400))

    assert report.demoted == [(SITE, "warm-1")]
    assert enabled_of(session, warm) is False
    assert fresh(session, warm).promoted_at is None
    assert kinds(session) == ["recorder.promoted", "recorder.demoted"]


def test_a_recorder_that_drops_out_again_restarts_the_clock(session, arbiter_settings) -> None:  # noqa: ANN001
    """The whole point of the hysteresis: a flapping recorder never gets the site back."""
    hot = make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    arbiter = Arbiter()
    arbiter.run(session, arbiter_settings, now=T0)

    recover(session, hot, T0 + timedelta(seconds=60))
    arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=60))
    # …and away again, then back: the four minutes of health before the gap count for nothing.
    recover(session, hot, T0 + timedelta(seconds=100))
    arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=300))  # stale again
    recover(session, hot, T0 + timedelta(seconds=310))
    assert arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=310)).demoted == []

    assert enabled_of(session, warm) is True


def test_every_hot_recorder_must_be_back_before_the_spare_stands_down(session, arbiter_settings) -> None:  # noqa: ANN001
    """Two hot recorders on a site is two recorders somebody wanted on it."""
    first = make_recorder(session, "hot-1", reported_ago=600)
    make_recorder(session, "hot-2", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    arbiter = Arbiter()
    arbiter.run(session, arbiter_settings, now=T0)

    for offset in (60, 400, 700):
        recover(session, first, T0 + timedelta(seconds=offset))
        arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=offset))

    assert enabled_of(session, warm) is True  # hot-2 has still never come back


def test_a_manually_enabled_spare_is_never_demoted(session, arbiter_settings) -> None:  # noqa: ANN001
    """``promoted_at`` NULL means a person did this, and a job does not undo a person."""
    hot = make_recorder(session, "hot-1", reported_ago=5)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=True, promoted_at=None)

    arbiter = Arbiter()
    for offset in (0, 400, 800):
        recover(session, hot, T0 + timedelta(seconds=offset))
        report = arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=offset))
        assert report.demoted == []

    assert enabled_of(session, warm) is True
    assert kinds(session) == []


def test_a_fresh_arbiter_still_demotes_what_a_previous_one_promoted(session, arbiter_settings) -> None:  # noqa: ANN001
    """Restart semantics: the promotion is a column, so a new process still owns it.

    The recovery clock is *not* a column, so the new process is merely more patient — it
    waits the full window from its own first pass before standing the spare down, which is
    the safe direction to be wrong in.
    """
    hot = make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)
    Arbiter().run(session, arbiter_settings, now=T0)
    assert fresh(session, warm).promoted_at == T0

    restarted = Arbiter()  # what an API/worker restart looks like: no memory at all
    recover(session, hot, T0 + timedelta(seconds=1000))
    assert restarted.run(session, arbiter_settings, now=T0 + timedelta(seconds=1000)).demoted == []
    assert enabled_of(session, warm) is True  # patient, not wrong

    recover(session, hot, T0 + timedelta(seconds=1400))
    assert restarted.run(session, arbiter_settings, now=T0 + timedelta(seconds=1400)).demoted == [
        (SITE, "warm-1")
    ]
    assert enabled_of(session, warm) is False


def test_a_promoted_spare_turned_cold_is_left_exactly_where_it_is(session, arbiter_settings) -> None:  # noqa: ANN001
    """Cold means hands off — including the hand that switched it on.

    The admin PUT clears ``promoted_at`` when a recorder is turned cold (see
    ``tests/api/test_recorders.py``), and this is the other half of that contract: with the
    promotion disowned, the arbiter neither stands the recorder down nor counts it out.
    """
    hot = make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)
    arbiter = Arbiter()
    arbiter.run(session, arbiter_settings, now=T0)

    row = session.get(Recorder, warm.id)
    row.standby, row.promoted_at = "cold", None
    session.commit()

    recover(session, hot, T0 + timedelta(seconds=1000))
    assert arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=1000)).demoted == []
    assert enabled_of(session, warm) is True


# --------------------------------------------------------------------------------------
# whose tuner is it: the per-tuner promotion stamp
# --------------------------------------------------------------------------------------


def test_a_promotion_stamps_the_tuner_it_switched_on(session, arbiter_settings) -> None:  # noqa: ANN001
    """The stamp is what makes demotion able to undo the arbiter's work and nothing else.

    ``promoted_at`` is per *recorder* and says only "keyup promoted this box for something";
    a box covering two sites needs to know which tuners it was, so the fact is written where
    the fact belongs.  It rides down to the recorder in the same desired write — trunkd's
    parser reads the keys it knows by name and ignores the rest.
    """
    hot = make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    arbiter = Arbiter()
    arbiter.run(session, arbiter_settings, now=T0)

    desired = desired_of(session, warm, "rtl-0")
    assert desired["enabled"] is True
    assert desired[PROMOTED_KEY] is True
    assert desired["site"] == SITE  # the operator's own keys are untouched by the merge

    # …and standing it down again takes the stamp back off: the row must not claim a
    # promotion that has been undone, or the next demotion would act on a stale flag.
    report = None
    for offset in (400, 800):  # the first pass only starts the recovery clock
        recover(session, hot, T0 + timedelta(seconds=offset))
        report = arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=offset))
    assert report.demoted == [(SITE, "warm-1")]
    desired = desired_of(session, warm, "rtl-0")
    assert desired["enabled"] is False
    assert PROMOTED_KEY not in desired
    assert fresh(session, warm).promoted_at is None


def test_a_hand_enabled_tuner_survives_demotion_of_the_same_recorder(session, arbiter_settings) -> None:  # noqa: ANN001
    """The bug the stamp exists for: ``promoted_at`` is per recorder, tuners are not.

    This spare is promoted by the arbiter for Central *and* switched on by an operator for
    Gardner.  Standing the Central promotion down used to reach for every tuner of the
    recorder on the site being demoted, and left ``promoted_at`` set for as long as *any*
    tuner was enabled — so a person's deliberate change sat under a flag that said the job
    owned it.  Only the stamped tuner is switched off now, and the flag is cleared with it.
    """
    central = make_recorder(session, "hot-central", reported_ago=600)
    gardner = make_recorder(session, "hot-gardner", site="Gardner", reported_ago=5)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)
    add_tuner(session, warm, "rtl-1", site="Gardner", desired={"site": "Gardner", "enabled": True})

    arbiter = Arbiter()
    assert arbiter.run(session, arbiter_settings, now=T0).promoted == [(SITE, "warm-1")]
    assert desired_of(session, warm, "rtl-0")[PROMOTED_KEY] is True
    assert PROMOTED_KEY not in desired_of(session, warm, "rtl-1")  # nobody stamped a person

    report = None
    for offset in (400, 800):
        recover(session, central, T0 + timedelta(seconds=offset))
        recover(session, gardner, T0 + timedelta(seconds=offset))
        report = arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=offset))

    assert report.demoted == [(SITE, "warm-1")]
    assert desired_of(session, warm, "rtl-0")["enabled"] is False
    # The operator's tuner is exactly as they left it — not switched off, not even written.
    hand_tuned = tuner_named(session, warm, "rtl-1")
    assert hand_tuned.desired == {"site": "Gardner", "enabled": True}
    assert (hand_tuned.desired_version, hand_tuned.updated_by) == (1, None)
    # Nothing stamped is left, so the recorder is no longer the arbiter's in any part.
    assert fresh(session, warm).promoted_at is None


def test_promoted_at_survives_until_the_last_stamped_tuner_is_demoted(session, arbiter_settings) -> None:  # noqa: ANN001
    """One spare covering two failed sites is stood down one site at a time."""
    central = make_recorder(session, "hot-central", reported_ago=600)
    gardner = make_recorder(session, "hot-gardner", site="Gardner", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)
    add_tuner(session, warm, "rtl-1", site="Gardner", desired={"site": "Gardner", "enabled": False})

    arbiter = Arbiter()
    report = arbiter.run(session, arbiter_settings, now=T0)
    assert sorted(report.promoted) == sorted([("Gardner", "warm-1"), (SITE, "warm-1")])
    assert desired_of(session, warm, "rtl-0")[PROMOTED_KEY] is True
    assert desired_of(session, warm, "rtl-1")[PROMOTED_KEY] is True

    # Central comes back; Gardner is still off the air.
    for offset in (400, 800):
        recover(session, central, T0 + timedelta(seconds=offset))
        arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=offset))

    assert desired_of(session, warm, "rtl-0")["enabled"] is False
    assert desired_of(session, warm, "rtl-1")["enabled"] is True
    assert fresh(session, warm).promoted_at == T0  # still covering Gardner for us

    for offset in (1200, 1600):
        recover(session, central, T0 + timedelta(seconds=offset))
        recover(session, gardner, T0 + timedelta(seconds=offset))
        arbiter.run(session, arbiter_settings, now=T0 + timedelta(seconds=offset))

    assert desired_of(session, warm, "rtl-1")["enabled"] is False
    assert fresh(session, warm).promoted_at is None


# --------------------------------------------------------------------------------------
# the lock
# --------------------------------------------------------------------------------------


def test_the_locked_write_merges_over_what_an_operator_just_committed(  # noqa: ANN001
    session, arbiter_settings, monkeypatch
) -> None:
    """A pass reads every recorder up front and locks one of them several statements later.

    A plain ``SELECT … FOR UPDATE`` hands back the objects already in the identity map,
    attributes and all — so the read-modify-write behind the lock would merge onto the
    snapshot from *before* the lock and silently drop whatever an operator committed in the
    gap.  The interleaving is forced here rather than raced: a second connection does the
    operator's ``PUT`` at the exact moment the arbiter reaches for the lock.
    """
    from app import arbiter as arbiter_module
    from app.api.recorders import lock_recorder as real_lock_recorder
    from app.db import SessionLocal

    make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)
    warm_id = warm.id

    def operator_writes_then_locks(db, recorder_id):  # noqa: ANN001, ANN202
        monkeypatch.setattr(arbiter_module, "lock_recorder", real_lock_recorder)  # once only
        other = SessionLocal()
        try:
            tuner = other.scalars(select(Tuner).where(Tuner.recorder_id == warm_id)).one()
            tuner.desired = dict(tuner.desired or {}, center_frequency=851_012_500)
            tuner.desired_version += 1
            other.commit()
        finally:
            other.close()
        return real_lock_recorder(db, recorder_id)

    monkeypatch.setattr(arbiter_module, "lock_recorder", operator_writes_then_locks)
    assert Arbiter().run(session, arbiter_settings, now=T0).promoted == [(SITE, "warm-1")]

    tuner = tuner_of(session, warm)
    assert tuner.desired["enabled"] is True  # the promotion applied…
    assert tuner.desired["center_frequency"] == 851_012_500  # …on top of the operator's edit
    # Both writes counted: the operator's bump was read, not overwritten with a stale one.
    assert tuner.desired_version == 3


# --------------------------------------------------------------------------------------
# the settings
# --------------------------------------------------------------------------------------


def test_the_knobs_are_settable_at_runtime_and_ship_off() -> None:
    from app.config import Settings
    from app.runtime_settings import SETTINGS_SPEC

    plain = Settings(_env_file=None)
    assert plain.arbiter_enabled is False  # the hard requirement: it must not default to on
    assert (plain.arbiter_stale_seconds, plain.arbiter_recovery_seconds) == (60, 300)
    assert plain.best_source_enabled is True

    spec = {key: (kind, group, label, description) for key, kind, group, label, description in SETTINGS_SPEC}
    for key in (
        "arbiter_enabled",
        "arbiter_stale_seconds",
        "arbiter_recovery_seconds",
        "best_source_enabled",
    ):
        assert key in spec, key
        kind, group, label, description = spec[key]
        assert group == "Recorders"
        assert label and label[0].isupper()
        # The descriptions render as helper text under the control: they have to say *why*.
        assert len(description) > 120, key


def test_the_arbiter_reads_the_switch_from_the_settings_table(session, arbiter_settings) -> None:  # noqa: ANN001
    """Flipping it in the Admin page takes effect on the next tick, not the next deploy."""
    from app import runtime_settings
    from app.models import Setting

    make_recorder(session, "hot-1", reported_ago=600)
    warm = make_recorder(session, "warm-1", standby="warm", enabled=False)

    session.add(Setting(key="arbiter_enabled", value=False))
    session.commit()
    runtime_settings.invalidate()
    assert Arbiter().run(session, arbiter_settings, now=T0).ran is False
    assert enabled_of(session, warm) is False

    session.query(Setting).delete()
    session.commit()
    runtime_settings.invalidate()
    assert Arbiter().run(session, arbiter_settings, now=T0).promoted == [(SITE, "warm-1")]
