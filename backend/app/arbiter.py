"""The standby arbiter: which recorder is supposed to be covering a site right now.

Several trunkd instances hear the same P25 site.  Running them all at once works, and it
is wasteful: every one of them uploads every call, and the archive spends its dedupe
budget throwing the copies away.  The alternative an operator actually wants is a *spare* —
a box that sits registered and idle, and starts recording only when the recorder that
normally covers the site stops reporting.

That is all this module does.  It is deliberately the simplest thing that can work:

**Three standby classes** (``recorders.standby``).  ``hot`` records whenever it is healthy
and the arbiter never touches it.  ``warm`` is the spare: registered, tuners disabled, and
the *only* class the arbiter will enable.  ``cold`` is never looked at — a bench radio, a
laptop, a box somebody is testing on — because the difference between "idle spare" and
"idle because a person is holding it" is invisible from the outside and guessing wrong
means keying up somebody's workbench.

**Down is staleness, nothing cleverer.**  A recorder is DOWN when its ``reported_at`` is
older than ``arbiter_stale_seconds``.  ``reported_at`` moves on every status push that
carries ``station`` or ``counters``, which trunkd always sends, so it is a true liveness
signal and not merely "the process is up".  No probing, no heuristics about call rates: a
recorder that cannot post its own status cannot upload calls either.

**Hysteresis, in one direction only.**  Promotion is immediate — the site is off the air
and every second costs traffic.  Demotion waits for ``arbiter_recovery_seconds`` of
*continuous* health from every hot recorder for the site, because a recorder that comes
back and drops out again would otherwise hand the site to and fro every half minute, and
each handover loses whatever was in flight.

**"Healthy since" is derived, conservatively, in memory.**  Each pass records the first
time it *observed* a recorder healthy (:attr:`Arbiter._healthy_since`) and forgets it the
moment the recorder goes stale.  Nothing is stored, so a keyup restart makes every
recovery clock start again from the restart — the spare then stays up for one more full
window rather than standing down early.  Waiting too long is a spare that keeps recording;
standing down too early is a site nobody is covering, so the error is taken in the safe
direction on purpose.  What *is* stored is the fact of the promotion itself
(``recorders.promoted_at``): a restart therefore never orphans an enabled spare, and a warm
recorder an operator enabled by hand — ``promoted_at`` NULL — is never switched off by this
job, whatever its recovery clocks say.

**It only ever flips ``enabled``.**  Every write goes through
:func:`app.api.recorders.apply_tuner_desired` under :func:`~app.api.recorders.lock_recorder`
— the same locked read-modify-write the operator's ``PUT`` uses, so a person editing a
recorder and this job are simply two writers queueing — and the only keys it merges are
``enabled`` and its own :data:`PROMOTED_KEY` bookkeeping stamp.  Role, focus site, center
frequency, spectrum: the arbiter never touches them, so an operator's tuning of a spare
survives being promoted and demoted any number of times.

**It only ever undoes its own work.**  Enabling a tuner stamps :data:`PROMOTED_KEY` on it;
standing one down is refused unless that stamp is there, and removes it.  So a spare that is
promoted for one site and switched on by hand for another loses only the half the arbiter
switched on, and ``recorders.promoted_at`` is cleared when no stamped tuner is left.

A pass that changes nothing writes nothing: no activity rows, no version bumps, no bus
traffic.  The log is for handovers, and a quiet night must read as a quiet night.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import activity
from app.api.recorders import apply_tuner_desired, lock_recorder
from app.config import Settings
from app.models import Recorder, Tuner
from app.runtime_settings import settings_for

log = logging.getLogger("app.arbiter")

__all__ = [
    "ARBITER_LABEL",
    "PROMOTED_KEY",
    "Arbiter",
    "ArbiterReport",
    "arbiter",
    "arbiter_promoted",
    "holds",
    "run_arbiter",
    "tuner_enabled",
    "tuner_sites",
]

#: What the arbiter writes into ``tuners.updated_by``.  An operator reading "who changed
#: this" must be able to tell a job from a person at a glance.
ARBITER_LABEL = "arbiter"

#: The key the arbiter stamps into a tuner's ``desired`` when *it* switches that tuner on,
#: and removes when it stands it down again.  ``recorders.promoted_at`` says only that the
#: arbiter promoted this recorder *for something*; on a box covering two sites that is not
#: enough to know which tuners it enabled, and demoting one site would otherwise reach for a
#: tuner an operator had switched on by hand.  The stamp is per-tuner, so what the job undoes
#: is exactly what the job did.  The recorder never reads it: trunkd's desired-state parser
#: takes the keys it knows by name and ignores the rest, so this rides along inert.
PROMOTED_KEY = "arbiter_promoted"


def tuner_enabled(tuner: Tuner) -> bool:
    """Whether this tuner is *meant* to be recording.

    Absent means enabled: a tuner nobody has ever edited has no ``desired`` at all and is
    running, so only an explicit ``enabled: false`` counts as off.
    """
    return (tuner.desired or {}).get("enabled", True) is not False


def arbiter_promoted(tuner: Tuner) -> bool:
    """Whether *this job* is what switched this tuner on (:data:`PROMOTED_KEY`).

    False for every tuner an operator enabled, for every tuner that was never disabled in
    the first place, and for every tuner promoted by a keyup old enough not to have stamped
    them — all three read as "not ours", which is the safe way round: the arbiter leaves it
    alone and a person switches it off when they want it off.
    """
    return bool((tuner.desired or {}).get(PROMOTED_KEY))


def _label(value: object) -> str | None:
    """A usable site label out of a JSONB value, or ``None``."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def tuner_sites(tuner: Tuner) -> set[str]:
    """Every site label this tuner is associated with — reported *or* desired.

    The reported label is the fact ("this tuner is on Central right now").  The desired one
    matters for exactly the case the arbiter exists to serve: a warm spare whose tuners are
    disabled may well stop reporting a site, and if candidacy were judged on the reported
    label alone a spare could be promoted once and then never again.  Which sites are
    *arbitrated* at all is still decided on reported labels only (:func:`reported_sites`) —
    what a tuner wants to be on is not evidence that the site exists.
    """
    sites = {_label((tuner.reported or {}).get("site")), _label((tuner.desired or {}).get("site"))}
    return {s for s in sites if s is not None}


def reported_sites(recorder: Recorder) -> set[str]:
    """The site labels this recorder's tuners actually report."""
    return {
        site
        for tuner in recorder.tuners
        if (site := _label((tuner.reported or {}).get("site"))) is not None
    }


def known_sites(recorder: Recorder) -> set[str]:
    """Every site this recorder is associated with, reported or configured."""
    return {site for tuner in recorder.tuners for site in tuner_sites(tuner)}


def holds(recorder: Recorder, site: str) -> bool:
    """True when this recorder has an **enabled** tuner on ``site`` — i.e. it is covering it.

    Both halves are needed: a tuner that is on the site but disabled is not recording it,
    and an enabled tuner on some other site is not either.  "On the site" is
    :func:`tuner_sites` — reported *or* desired — rather than the reported label alone,
    because a tuner that has only just been enabled has not reported yet, and judging on the
    report would let one pass promote a second spare on top of the one it promoted a moment
    ago.
    """
    return any(tuner_enabled(tuner) and site in tuner_sites(tuner) for tuner in recorder.tuners)


@dataclass
class ArbiterReport:
    """What one pass did — the return value the worker logs and the tests assert on."""

    #: sites where a warm spare was switched on this pass: ``(site, instance_id)``
    promoted: list[tuple[str, str]] = field(default_factory=list)
    #: sites where a promoted spare was switched off again
    demoted: list[tuple[str, str]] = field(default_factory=list)
    #: sites with no healthy hot recorder and no spare available to take them
    uncovered: list[str] = field(default_factory=list)
    #: false when the setting is off — "nothing to do" and "did not look" are different
    ran: bool = True

    @property
    def changed(self) -> bool:
        return bool(self.promoted or self.demoted)


class Arbiter:
    """The pass, plus the only state it keeps: when each recorder was first seen healthy.

    An instance is long-lived (the worker holds one) so that the recovery window means
    something across passes.  A fresh instance is a correct, merely more patient, arbiter:
    it knows nothing about the past, so every recovery clock starts now and every promotion
    it inherits from ``recorders.promoted_at`` is held for one more full window.
    """

    def __init__(self) -> None:
        #: recorder id → the first pass at which it was observed healthy, cleared on staleness
        self._healthy_since: dict[int, datetime] = {}

    # -------------------------------------------------------------------- health ---

    def _observe(self, recorders: list[Recorder], now: datetime, stale_after: timedelta) -> dict[int, bool]:
        """Update the healthy-since clocks and answer "is each recorder up?".

        A recorder that has *never* reported is down: it has registered (polled for its
        desired state) but has not told us it is running, and a spare must not be stood down
        because a box that has never said anything happens to have a row.
        """
        healthy: dict[int, bool] = {}
        for recorder in recorders:
            reported_at = recorder.reported_at
            up = reported_at is not None and (now - reported_at) <= stale_after
            healthy[recorder.id] = up
            if up:
                self._healthy_since.setdefault(recorder.id, now)
            else:
                self._healthy_since.pop(recorder.id, None)
        # Forget recorders that have gone from the table entirely.
        live = {r.id for r in recorders}
        for gone in [rid for rid in self._healthy_since if rid not in live]:
            del self._healthy_since[gone]
        return healthy

    def _recovered(self, recorder: Recorder, now: datetime, recovery: timedelta) -> bool:
        """Has this recorder been continuously healthy for the whole recovery window?"""
        since = self._healthy_since.get(recorder.id)
        return since is not None and (now - since) >= recovery

    # ---------------------------------------------------------------------- pass ---

    def run(
        self,
        session: Session,
        settings: Settings | None = None,
        *,
        now: datetime | None = None,
    ) -> ArbiterReport:
        """One arbitration pass.  Commits its own work; never raises for a policy reason.

        Reads the settings through :func:`app.runtime_settings.settings_for` on every pass,
        so flipping the switch in the Admin page takes effect on the next 30-second tick
        rather than on the next deploy.
        """
        effective = settings_for(session, settings)
        report = ArbiterReport()
        if not effective.arbiter_enabled:
            report.ran = False
            return report

        now = now or datetime.now(UTC)
        stale_after = timedelta(seconds=max(1, int(effective.arbiter_stale_seconds)))
        recovery = timedelta(seconds=max(0, int(effective.arbiter_recovery_seconds)))

        recorders = list(
            session.scalars(
                select(Recorder).options(selectinload(Recorder.tuners)).order_by(Recorder.id)
            )
        )
        healthy = self._observe(recorders, now, stale_after)

        # Only sites a *hot* recorder is responsible for are arbitrated.  A site no hot
        # recorder has ever reported has no normal owner to fail over from, and promoting a
        # spare for it would be inventing coverage rather than restoring it.
        hot = [r for r in recorders if r.standby == "hot"]
        warm = [r for r in recorders if r.standby == "warm"]
        sites = sorted({site for r in hot for site in reported_sites(r)})

        for site in sites:
            owners = [r for r in hot if site in known_sites(r)]
            if not owners:  # pragma: no cover - `sites` is built from these very recorders
                continue
            if any(healthy[r.id] for r in owners):
                self._maybe_demote(session, site, owners, warm, now, recovery, report)
            else:
                self._maybe_promote(session, site, recorders, warm, healthy, now, report)

        if report.changed:
            session.commit()
        else:
            # Nothing was written, but the pass did read: end the transaction so a long-lived
            # worker session does not hold a snapshot open between ticks.
            session.rollback()
        return report

    # ------------------------------------------------------------------- actions ---

    def _maybe_promote(
        self,
        session: Session,
        site: str,
        recorders: list[Recorder],
        warm: list[Recorder],
        healthy: dict[int, bool],
        now: datetime,
        report: ArbiterReport,
    ) -> None:
        """Every hot recorder for ``site`` is down — put the best spare on it."""
        # Somebody healthy may already be recording it: the spare promoted on an earlier
        # pass, or a warm box an operator turned on by hand.  Either way the site is covered
        # and a second spare would only duplicate it.  `cold` is excluded from this test as
        # from everything else: whatever a bench box is doing is not coverage keyup may rely on.
        if any(healthy[r.id] and r.standby != "cold" and holds(r, site) for r in recorders):
            return

        # Lowest priority value wins, oldest row breaks the tie — so promotion is
        # predictable and an operator can express "the tower before the garage" as a number.
        candidates = sorted(
            (r for r in warm if healthy[r.id] and site in known_sites(r)),
            key=lambda r: (r.priority, r.id),
        )
        if not candidates:
            report.uncovered.append(site)
            log.warning("arbiter: %s has no healthy recorder and no warm spare that knows it", site)
            return

        chosen = candidates[0]
        recorder = lock_recorder(session, chosen.id)
        if recorder is None:  # pragma: no cover - it was in the list a moment ago
            return
        flipped = self._set_enabled(session, recorder, site, enabled=True)
        if not flipped:
            # Nothing to switch on: its tuners for this site are already enabled and simply
            # have not reported yet.  Not a promotion, so not a log line.
            return
        recorder.promoted_at = now
        session.flush()
        activity.record(
            session,
            source="arbiter",
            level="warn",
            kind="recorder.promoted",
            message=f"{site}: promoted standby {recorder.instance_id} — no hot recorder reporting",
            meta={
                "site": site,
                "instance": recorder.instance_id,
                "recorder_id": recorder.id,
                "priority": recorder.priority,
                "tuners": flipped,
            },
        )
        report.promoted.append((site, recorder.instance_id))
        log.warning("arbiter: promoted %s for %s (tuners %s)", recorder.instance_id, site, flipped)

    def _maybe_demote(
        self,
        session: Session,
        site: str,
        owners: list[Recorder],
        warm: list[Recorder],
        now: datetime,
        recovery: timedelta,
        report: ArbiterReport,
    ) -> None:
        """The hot side is back — stand the spare down, but only after the whole window.

        The spare's own health is not consulted: disabling a tuner is a *desired* state, and
        a spare that is off the air when it is stood down simply applies it when it returns.

        *Every* hot recorder for the site must have been healthy for the window, not merely
        one of them: a site an operator gave two recorders is a site they wanted two
        recorders on, and standing the spare down while one of them is still missing would
        quietly leave the site on half its intended coverage.
        """
        if not all(self._recovered(r, now, recovery) for r in owners):
            return
        for candidate in warm:
            # `promoted_at` is the first of two gates, and the coarse one: it says *keyup*
            # turned this recorder on for something.  A spare an operator enabled by hand has
            # NULL here and is left alone for ever — undoing a person's deliberate change is
            # the one thing a background job must never do.  The fine gate is per-tuner and
            # lives in `_set_enabled`: only tuners carrying the arbiter's own stamp are
            # switched off, so a recorder that is promoted for one site and hand-tuned for
            # another keeps the hand-tuned half.
            if candidate.promoted_at is None or not holds(candidate, site):
                continue
            recorder = lock_recorder(session, candidate.id)
            if recorder is None:  # pragma: no cover - it was in the list a moment ago
                continue
            flipped = self._set_enabled(session, recorder, site, enabled=False)
            if not flipped:
                continue
            # The recorder may have been promoted for a second site as well; it stays
            # "promoted" until no tuner still carries the arbiter's stamp.  Counting *enabled*
            # tuners instead — as this used to — kept `promoted_at` set for as long as any
            # tuner was running, including one an operator had switched on themselves, which
            # left the flag standing on a recorder the arbiter no longer owned any part of.
            if not any(arbiter_promoted(t) for t in recorder.tuners):
                recorder.promoted_at = None
            session.flush()
            activity.record(
                session,
                source="arbiter",
                level="info",
                kind="recorder.demoted",
                message=(
                    f"{site}: stood standby {recorder.instance_id} down — "
                    f"hot recorders healthy for {int(recovery.total_seconds())}s"
                ),
                meta={
                    "site": site,
                    "instance": recorder.instance_id,
                    "recorder_id": recorder.id,
                    "tuners": flipped,
                },
            )
            report.demoted.append((site, recorder.instance_id))
            log.info("arbiter: demoted %s for %s (tuners %s)", recorder.instance_id, site, flipped)

    def _set_enabled(
        self, session: Session, recorder: Recorder, site: str, *, enabled: bool
    ) -> list[str]:
        """Flip ``enabled`` on this recorder's tuners for one site; return the keys changed.

        Only tuners that are not already in the wanted state are written, so a pass that
        would be a no-op bumps no versions and the recorder is not handed a desired-state
        generation with nothing new in it.

        Switching **on** stamps :data:`PROMOTED_KEY` into the same desired write, and
        switching off is allowed only on tuners carrying that stamp — and removes it.  That
        pairing is what makes demotion undo the arbiter's own work and nothing else: a tuner
        an operator enabled by hand has no stamp and is skipped here even on a recorder the
        arbiter promoted for some other site.
        """
        changed: list[str] = []
        for tuner in sorted(recorder.tuners, key=lambda t: t.tuner_key):
            if site not in tuner_sites(tuner) or tuner_enabled(tuner) is enabled:
                continue
            if not enabled and not arbiter_promoted(tuner):
                continue
            apply_tuner_desired(
                session,
                recorder,
                tuner.tuner_key,
                # `None` clears the key, which is the merge rule `apply_tuner_desired`
                # already implements: a stood-down tuner carries no stamp at all.
                {"enabled": enabled, PROMOTED_KEY: True if enabled else None},
                updated_by=ARBITER_LABEL,
            )
            changed.append(tuner.tuner_key)
        return changed


#: The process-wide arbiter the scheduled job drives, so the recovery clocks survive between
#: ticks.  Tests build their own :class:`Arbiter` — a fresh one is what a restart looks like.
arbiter = Arbiter()


def run_arbiter(session: Session, settings: Settings | None = None) -> ArbiterReport:
    """One pass of the process-wide :data:`arbiter` — what the scheduler calls."""
    return arbiter.run(session, settings)
