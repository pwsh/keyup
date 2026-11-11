"""Cross-recorder call reconciliation — docs/RECONCILIATION.md, ARCHITECTURE §5.1 step 4.

Two recorders cover the same P25 system at the same time: the old sdrtrunk chain (whose
backlog is bulk-uploaded later and which may keep running for a while) and trunkd (live,
near real time).  The same transmission therefore lands twice — different tuner, different
software, slightly different start, duration and metadata, sometimes days apart.  Keyup
has to recognize the two as one transmission, keep the **better** copy as the primary row,
fold the other's metadata into it, and hide the other behind ``duplicate_of``.

The old dedupe could not do that.  It matched on ``|Δstart| ≤ 1 s`` and ``|Δduration| ≤
10 %``, which two tuners rarely satisfy; it always kept whichever arrived first, so a
better later copy lost; and on the push path it did not store the loser at all.

Three ideas replace it:

**Matching is interval overlap, not start equality** (:func:`matches`).  On one talkgroup —
and on one traffic frequency — two transmissions cannot occupy the same time slice, so two
calls of one system are the same transmission when their talkgroup sets overlap (or both
name the same frequency and one of them names no talkgroup) *and* their ``[started_at,
ended_at]`` intervals, widened by ``RECONCILE_CLOCK_SKEW_SECONDS`` of clock disagreement,
overlap by at least ``RECONCILE_MIN_OVERLAP`` of the shorter one — or the two agree on
*both* edges within that allowance, which is what carries the short calls that make up most
of the archive (an sdrtrunk file's start is derived from a stamp written at ``call end +
1.7 s``, and on a 2.4 s call that offset leaves too little overlap for the floor), or the
shorter copy is no longer than ``RECONCILE_FRAGMENT_SECONDS`` and lies *inside* the other,
which is a late-granted trunkd fragment of a call sdrtrunk recorded whole.  Back-to-back
transmissions on one talkgroup satisfy none of the three and so never match.

**The primary is chosen by score, not by arrival** (:func:`score_call`).  Having audio
dominates everything; then the longer (more complete) recording, with its credit capped at
``RECONCILE_MERGE_RATIO`` because a copy several times longer is trunkd's hang timer
merging two calls rather than a better recording; then the receiver that actually heard it
better, as its own decoder counted (``calls.source_quality``); then an exact time source,
metadata richness, audio quality, a finished transcript, and last — a tie-break only — the
recorder preference ``RECONCILE_PREFER_SOURCES``.  Every component is kept in
``calls.reconcile_reason`` so the decision can be read back.

**Losing does not mean being thrown away** (:func:`reconcile_pair`).  The loser is stored
with ``duplicate_of`` pointing at the winner, and everything it knows that the winner does
not — the radio, the talker alias, the encryption flags, the frequency, the site, the patch
members, its ``call_sources`` rows, its whole ``raw_meta`` under ``merged_from`` — is
merged into the winner.  When the newcomer wins, the pair *swaps*: the old primary and
everything that pointed at it move to the newcomer, the conversation membership moves with
it, and the newcomer is queued for transcription at priority 1 if the copy it displaced had
a transcript and it has none.  Audio is never copied across: two tuners produce two
different recordings, and a transcript belongs to the bytes it was made from.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Select, and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import lock_calls, notify
from app.models import (
    AudioJob,
    Call,
    CallSource,
    Conversation,
    Radio,
    Setting,
    TranscriptionJob,
)

log = logging.getLogger("app.ingest.reconcile")

__all__ = [
    "RECONCILE_LOCK_CLASS",
    "SHORT_CALL_MAX_DURATION_DELTA_S",
    "SHORT_CALL_MAX_START_DELTA_S",
    "UNIT_RECONCILE_LOCK_CLASS",
    "MatchResult",
    "ReconcileOutcome",
    "ReconcileReport",
    "Score",
    "find_match",
    "find_match_for_record",
    "lock_reconcile",
    "make_primary",
    "matches",
    "quality_score",
    "reconcile_batch",
    "reconcile_lookup",
    "reconcile_pair",
    "reconcile_upload",
    "score_call",
]

#: ``pg_advisory_xact_lock`` class id.  4242 is conversation assignment and 4243 the old
#: dedupe; this is the third namespace, so no key can collide with either.
RECONCILE_LOCK_CLASS = 4244
#: The same, for unit calls, which key on a pair of radio ids rather than a tgid.  A class
#: of its own so the two key spaces cannot collide (:func:`lock_reconcile`).
UNIT_RECONCILE_LOCK_CLASS = 4245

#: the key of the ``settings`` row holding the batch pass' watermark
LAST_PASS_KEY = "reconcile_last_pass_at"

#: How far two copies of one sub-second key-up may differ in *length* and still be the same
#: transmission (the ``short-call`` rung).  Two recorders measure the same key-up to within a
#: frame or two; a quarter of a second apart is a different transmission, not a different
#: measurement of one.
SHORT_CALL_MAX_DURATION_DELTA_S = 0.25

#: …and how far their *starts* may differ.  Deliberately **not** ``skew_s`` (2 s): this rung
#: exists for one fault — a recorder reporting its call start in whole seconds — and rounding
#: one instant to seconds on two clocks can move the two answers at most one second apart.
#: The allowance is the size of the error being corrected, and nothing wider: at 2 s the rung
#: would collapse two 1.2 s key-ups 1.3 s apart, which every other rung is careful not to do.
#: A deployment that lowers ``skew_s`` below a second tightens this too.
SHORT_CALL_MAX_START_DELTA_S = 1.0

#: How far back a candidate's ``started_at`` may be and still overlap this call.  The
#: longest recording in the corpus is 44.5 s and trunkd's hang timer can join a few of
#: them, so ten minutes is far past anything real — it exists only to bound the index
#: range the candidate query scans, not to express a rule.
MAX_CALL_SECONDS = 600.0

#: How many rows sharing one ``sha256`` the exact rung will look at before giving up on it
#: and falling through to the interval rung.  More than one row can carry a hash — a file
#: retransmitted, plus every copy already ruled a duplicate of it — and each is followed to
#: its primary, so the scan is bounded rather than "the first by id" (which could be a copy
#: of the caller) or unbounded (which a pathological hash would turn into a table sweep).
EXACT_HASH_SCAN_LIMIT = 25

#: How far apart two copies' ``started_at`` may be before an identical ``sha256`` stops
#: being evidence that they are the same transmission.  Two recorders' copies of one
#: key-up differ by a second or two of clock skew, and a file retransmitted carries its
#: original timestamp, so 60 s is generous for every real case — while ruling out the one
#: the hash cannot distinguish: a recording with no voice in it (a squelch tail, 0.14-2 s)
#: encodes to *byte-identical* MP3 bytes every time, so hundreds of unrelated calls across
#: months share one hash.
EXACT_HASH_MAX_SKEW_S = 60.0

#: Time sources that name the call's own start.  ``file_write`` is the one that does not:
#: an sdrtrunk file is stamped when the recorder closes it — ``call end + 1.7 s`` measured
#: against trunkd's grant times — so its ``started_at`` is that minus the duration and sits
#: ≈ 2 s late.  That is precisely the copy whose timestamp should lose a tie, and precisely
#: why the default clock-skew allowance is 2 s.
EXACT_TIME_SOURCES = frozenset({"upload", "sidecar", "event_log"})

#: How late a ``file_write`` copy sits against the air: the sdrtrunk stamp is written at
#: ``call end + 1.7 s`` (docs/RECONCILIATION.md §5), so both edges of such a row are 1.7 s
#: after the transmission's.  Only the ``fragment inside`` rung takes it back out, to ask
#: whether the two copies were on the air at the same moment at all (:func:`_on_air`).
FILE_STAMP_OFFSET_S = 1.7

#: transcript statuses that mean "there is text on this row"
FINISHED_TRANSCRIPT = frozenset({"done"})

#: weights, chosen so that each rank dominates every rank below it combined:
#: preference ≤ 3 < transcript 8 < quality ≤ 20 < one metadata field 32 < time source 300
#: < source quality 700.  Duration is the exception on purpose — it is continuous, and a
#: 0.2 % difference between two tuners' encoders must not outrank an exact timestamp, so
#: its credit is scaled: ``4000 × (ratio − 1)``, capped at the merge ratio.  Everything
#: below it comes to 1287 (700 + 300 + 8×32 + 20 + 8 + 3), so a copy that loses every one
#: of those ranks — in particular the one that matters, the receiver that heard fewer
#: errored frames — has to be ≈ 32 % longer before its length carries the decision.  Losing
#: only the ranks under source quality (587) takes ≈ 15 %; a lone exact timestamp, ≈ 8 %.
W_AUDIO = 10_000.0
W_DURATION = 4_000.0  # × the capped relative excess, so 0…2000
W_SOURCE_QUALITY = 700.0
W_TIME_SOURCE = 300.0
W_METADATA = 32.0  # × the number of non-null fields, 0…8
W_QUALITY = 20.0  # × 0…1
W_TRANSCRIPT = 8.0
W_PREFERENCE = 3.0  # × 0…1

#: What one bad frame costs against one good one when scoring a copy.  Errored frames are
#: the audible defect — a bad frame is a hole or a burst of noise in the speech — so they
#: are weighed several times heavier than the mere length of the recording, and a copy that
#: heard more of the call *and* fumbled more of it does not automatically win.
BAD_FRAME_PENALTY = 5.0


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileConfig:
    """The knobs, read off :class:`app.config.Settings` once per call."""

    enabled: bool = True
    skew_s: float = 2.0
    min_overlap: float = 0.5
    fragment_s: float = 1.5
    merge_ratio: float = 1.5
    prefer_sources: tuple[str, ...] = ("trunkd", "trunk_recorder", "upload", "file")

    @classmethod
    def of(cls, settings: Settings) -> ReconcileConfig:
        return cls(
            enabled=bool(settings.reconcile_enabled),
            skew_s=float(settings.reconcile_clock_skew_seconds),
            min_overlap=float(settings.reconcile_min_overlap),
            fragment_s=float(settings.reconcile_fragment_seconds),
            merge_ratio=float(settings.reconcile_merge_ratio),
            prefer_sources=tuple(settings.reconcile_prefer_sources or ()),
        )


# --------------------------------------------------------------------------------------
# matching (§5.1 step 4, rung 2)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """The minimum of a call that matching needs — so a record can be matched before it
    is a row, and a row can be matched without loading its neighbors."""

    started_at: datetime
    ended_at: datetime
    duration_s: float
    talkgroup_ids: tuple[int, ...]
    frequency_hz: int | None
    #: ``group`` or ``unit`` (``calls.kind``).  Two copies of one transmission always agree
    #: about this, and two calls that disagree are never the same transmission whatever
    #: their timing says — so it is part of the interval rather than a caller's filter.
    kind: str = "group"
    #: **unit calls only** — ``(source radio pk, target radio pk)``.  A private call has no
    #: talkgroup and shares its traffic frequency with everything else the site granted, so
    #: the pair *is* its channel: this is what a unit call keys on in place of a tgid.
    unit_pair: tuple[int | None, int | None] = (None, None)
    #: The ``radios`` row of the radio that keyed up, for **either** kind — the same number
    #: as ``unit_pair[0]`` on a unit call, and the thing a group call has instead of a pair.
    #: Only the ``short-call`` rung reads it, and only to refuse: two sub-second key-ups a
    #: second apart on one talkgroup are two transmissions when two different radios made
    #: them.  ``None`` means "the archive cannot say", which is never evidence either way.
    source_radio: int | None = None
    #: ``calls.time_source`` — only ``file_write`` means anything here: that copy's edges
    #: sit :data:`FILE_STAMP_OFFSET_S` late, which the ``fragment inside`` rung removes
    #: before asking whether the fragment was on the air during the longer call.
    time_source: str | None = None

    @classmethod
    def of(cls, call: Call) -> Interval:
        return cls(
            started_at=call.started_at,
            ended_at=call.ended_at,
            duration_s=float(call.duration_s),
            talkgroup_ids=tuple(call.talkgroup_ids or ()),
            frequency_hz=call.frequency_hz,
            kind=call.kind or "group",
            unit_pair=(call.radio_id, call.target_radio_id),
            source_radio=_decoded_radio(call),
            time_source=call.time_source,
        )

    @classmethod
    def of_record(
        cls,
        record: Any,
        unit_pair: tuple[int | None, int | None] = (None, None),
        source_radio: int | None = None,
    ) -> Interval:
        """The same view of a :class:`app.ingest.parser.CallRecord`, so a recording can be
        matched *before* it is written — which is what tells ingest whether to count it in
        the directory's totals.

        ``unit_pair`` is the one thing a record cannot answer for itself: it carries the
        radio ids the air used and matching keys on the ``radios`` rows they resolve to, so
        the caller (:func:`find_match_for_record`) looks them up first.  Both ``None`` for a
        group call, and for a unit call whose radios the archive has never seen — which is
        the same statement as "there is no earlier copy of this to match".
        """
        tgids = tuple(record.talkgroup_ids or ())
        if not tgids and record.tgid is not None:
            tgids = (record.tgid,)
        radio = getattr(record, "radio", None)
        if radio is not None and radio.radio_id == 0:
            source_radio = None  # not decoded: see _decoded_radio
        elif source_radio is None:
            source_radio = unit_pair[0]
        return cls(
            started_at=record.started_at,
            ended_at=record.ended_at,
            duration_s=float(record.duration_s),
            talkgroup_ids=tgids,
            frequency_hz=record.frequency_hz,
            kind=getattr(record, "kind", None) or "group",
            unit_pair=unit_pair,
            source_radio=source_radio,
            time_source=getattr(record, "time_source", None),
        )


def _decoded_radio(call: Call) -> int | None:
    """``call.radio_id`` for the same-radio guard — or ``None`` when that row is radio 0.

    A recorder that did not decode the source files the call under air id 0, which has a
    ``radios`` row like any other; comparing its pk with a real radio's would read "not
    decoded" as "a different radio" and refuse a copy whose other receiver did decode it
    (961 overlapping ``fragment inside`` pairs in one week of production).
    """
    if call.radio_id is None:
        return None
    session = Session.object_session(call)
    radio = session.get(Radio, call.radio_id) if session is not None else None
    return None if radio is not None and radio.radio_id == 0 else call.radio_id


@dataclass(frozen=True)
class MatchResult:
    """Why two calls are (or are not) the same transmission."""

    matched: bool
    rule: str
    overlap_s: float = 0.0
    overlap_ratio: float = 0.0
    key: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "key": self.key,
            "overlap_s": round(self.overlap_s, 3),
            "overlap_ratio": round(self.overlap_ratio, 3),
        }


def matches(a: Interval, b: Interval, cfg: ReconcileConfig) -> MatchResult:
    """Are these two recordings the same transmission?

    **The kind** must agree before anything else: a unit-to-unit (private) call and a group
    call are different transmissions even when they overlap perfectly, and they are keyed on
    different things — a private call is on no talkgroup, so its channel is the pair of
    radios at its two ends rather than a tgid or the frequency it happened to be granted.

    **The channel** must agree next: the talkgroup sets overlap, *or* both name a
    frequency and it is the same one.  A trunkd push that arrives without a usable
    talkgroup still has its traffic frequency, and one frequency carries one transmission
    at a time.

    Then **the time slice**, by any of three rungs:

    1. **interval overlap** — the two overlap by at least ``min_overlap`` of the shorter
       recording, with the skew allowance taken back out first so that two calls which
       merely abut do not collect ``2 × skew`` of free overlap and match.  This is the
       clean case, and it is the only rung a call of 4 s or more needs.
    2. **aligned edges** — both recorders agree, within the clock allowance, about when
       the transmission began *and* when it ended.  Rung 1 cannot see a short call: an
       sdrtrunk file's start is derived from a stamp written at ``call end + 1.7 s``
       (docs/RECONCILIATION.md §5), and 1.7 s of offset on a 2.4 s call — the corpus
       median is 2.2 s — leaves only 28 % overlap, well under the floor.  Two copies of
       one transmission still agree on *both* edges to within the offset; a following
       transmission does not.  The allowance is capped at the shorter recording's own
       length, because slack longer than the call itself would let two consecutive
       key-ups collapse into one.
    3. **short-call** — both recordings are under ``2 x skew`` long, the same radio keyed
       them (when the archive can say who), they started within
       :data:`SHORT_CALL_MAX_START_DELTA_S` of each other and they last the same length to
       within :data:`SHORT_CALL_MAX_DURATION_DELTA_S`.  A recorder stamping whole seconds pushes
       two copies of one 0.65 s key-up into two intervals that never touch, which rungs 1
       and 2 both measure as "no".
    4. **fragment inside** — the shorter copy is at most ``fragment_s`` long, lies
       inside the longer one, overlaps it on the air (after the ``file_write`` stamp offset
       is taken back out) and was not keyed by a different radio: a late-granted trunkd
       fragment of a call sdrtrunk recorded whole, where the overlap ratio is under the
       floor for a reason that is not doubt.

    A shared **frequency** is the channel only when at least one side has no talkgroup: two
    calls that each name a talkgroup, and not the same one, are never one transmission.

    The one thing no rung will do is match two transmissions that merely follow each other
    on a talkgroup.
    """
    # A unit call and a group call are never the same transmission, whatever their timing
    # says: they were addressed to different things, and one of them is not on a talkgroup
    # at all.  Asked before the channel, because for a unit call the *channel* is a
    # different question with a different answer.
    if a.kind != b.kind:
        return MatchResult(False, "different call kind")

    if a.kind == "unit":
        # No talkgroup, and the traffic frequency is shared with every other grant on the
        # site — so two copies of one private call are the ones between the same two radios
        # at the same moment.  A pair with an unresolved end matches nothing: "we do not
        # know who this was" is not evidence that it is the same call as another unknown.
        source, target = a.unit_pair
        if source is None or target is None or a.unit_pair != b.unit_pair:
            return MatchResult(False, "different channel")
        key = f"unit:{source}>{target}"
    else:
        tg_a, tg_b = set(a.talkgroup_ids), set(b.talkgroup_ids)
        shared = tg_a & tg_b
        if shared:
            key = f"talkgroup:{min(shared)}"
        elif (
            a.frequency_hz is not None
            and a.frequency_hz == b.frequency_hz
            # The frequency speaks only for a copy that has no talkgroup to speak for it.
            # Two calls that each name a talkgroup, and not the same one, are two
            # transmissions whatever channel they were granted: a site reuses a traffic
            # frequency within a second of releasing it.
            and (not tg_a or not tg_b)
        ):
            key = f"frequency:{a.frequency_hz}"
        else:
            return MatchResult(False, "different channel")

    skew = timedelta(seconds=cfg.skew_s)
    start = max(a.started_at - skew, b.started_at - skew)
    end = min(a.ended_at + skew, b.ended_at + skew)
    overlap = (end - start).total_seconds()
    if overlap <= 0:
        return MatchResult(False, "no time overlap", overlap_s=overlap, key=key)

    # The overlap the skew alone would produce is not evidence of anything: two calls that
    # merely abut get 2 × skew for free, so it is taken back out before the ratio.
    real_overlap = max(0.0, overlap - 2 * cfg.skew_s)
    shorter = max(0.001, min(a.duration_s, b.duration_s))
    longer = max(a.duration_s, b.duration_s)
    ratio = real_overlap / shorter

    if ratio >= cfg.min_overlap:
        return MatchResult(True, "interval overlap", real_overlap, ratio, key)

    # Rung 2.  Both edges inside the allowance, and the allowance never exceeds the
    # shorter recording: on a 1.2 s call the slack is 1.2 s, so two 1.2 s key-ups 1.3 s
    # apart stay two calls.  The cost of that cap is that a very short call carrying the
    # full 1.7 s file-stamp offset does not reconcile at all — a miss, which costs one
    # duplicated row, rather than a merge of two real transmissions, which loses one.
    align = min(cfg.skew_s, shorter)
    if (
        abs((a.started_at - b.started_at).total_seconds()) <= align
        and abs((a.ended_at - b.ended_at).total_seconds()) <= align
    ):
        return MatchResult(True, "aligned edges", real_overlap, ratio, key)

    # Rung 3: two whole-second stamps of one sub-second key-up.  A recorder that reports
    # its call start in whole seconds (the push path's ``dateTime``) can stamp a 0.648 s
    # transmission at 04:59:32 on one receiver and 04:59:33 on the other; the two intervals
    # then do not touch, so rung 1 has nothing to measure and rung 2's allowance — capped at
    # the shorter recording, 0.648 s — is smaller than the second between them.  A recorded
    # key-up pair was exactly that.  What is left to go on is that both are far
    # too short to be two separate transmissions this close together, that they are on the
    # same channel, that they last the same length to within a rounding, and that the same
    # radio keyed both — so all four are required, and the whole rung is confined to
    # recordings under ``2 x skew``, which is the only region where whole-second rounding can
    # hide the overlap in the first place.  The start allowance is
    # :data:`SHORT_CALL_MAX_START_DELTA_S` rather than the full skew, because a second is the
    # whole of the error this rung exists to undo.
    short = 2 * cfg.skew_s
    same_radio = (
        a.source_radio is None or b.source_radio is None or a.source_radio == b.source_radio
    )
    if (
        a.duration_s < short
        and b.duration_s < short
        and same_radio
        and abs((a.started_at - b.started_at).total_seconds())
        <= min(cfg.skew_s, SHORT_CALL_MAX_START_DELTA_S)
        and abs(a.duration_s - b.duration_s) <= SHORT_CALL_MAX_DURATION_DELTA_S
    ):
        return MatchResult(True, "short-call", real_overlap, ratio, key)

    # Rung 4: a very short copy that sits inside a much longer one.  The overlap ratio is
    # measured against the fragment, so "inside" is the same statement as "most of the
    # fragment is covered", but the ratio bar is dropped because a 1.2 s trunkd fragment
    # of an 8 s sdrtrunk call is one transmission whatever the arithmetic of two offset
    # stamps says.
    #
    # "Inside" is not the skew-widened containment alone: that let a 1.2 s key-up starting
    # 0.5 s *after* an 8 s call ended count as inside it — the reply on a talkgroup folded
    # into the call it answered.  The fragment must actually have been on the air
    # during the longer call, measured with the one offset whose size and direction the
    # archive knows (a ``file_write`` copy is 1.7 s late) taken back out and no other slack,
    # and two different radios never make one transmission (rung 3's guard).
    if shorter <= cfg.fragment_s and longer > shorter and same_radio:
        inner, outer = (a, b) if a.duration_s <= b.duration_s else (b, a)
        if (
            inner.started_at >= outer.started_at - skew
            and inner.ended_at <= outer.ended_at + skew
            and _on_air_overlap_s(inner, outer) > 0
        ):
            return MatchResult(True, "fragment inside", real_overlap, ratio, key)

    return MatchResult(False, "overlap below the floor", real_overlap, ratio, key)


def _on_air(interval: Interval) -> tuple[datetime, datetime]:
    """The interval as the air had it: a ``file_write`` copy moved back by the stamp offset."""
    if interval.time_source == "file_write":
        shift = timedelta(seconds=FILE_STAMP_OFFSET_S)
        return interval.started_at - shift, interval.ended_at - shift
    return interval.started_at, interval.ended_at


def _on_air_overlap_s(a: Interval, b: Interval) -> float:
    """Seconds the two were on the air together — negative when there is a gap between them."""
    a_start, a_end = _on_air(a)
    b_start, b_end = _on_air(b)
    return (min(a_end, b_end) - max(a_start, b_start)).total_seconds()


def lock_reconcile(
    session: Session,
    *,
    talkgroup_ids: list[int] | tuple[int, ...],
    tgid: int | None,
    frequency_hz: int | None,
    unit_pair: tuple[int | None, int | None] = (None, None),
) -> None:
    """Serialize the match-then-write for every channel key this call could match on.

    The old dedupe lock keyed on ``(tgid, second)``, which is exactly what interval
    matching had to give up: two copies of one transmission start in different seconds, so
    they would take different locks and neither would see the other.  The key is therefore
    the *channel* — each talkgroup, and the frequency — with no time in it, so two calls
    that could possibly overlap always serialize.  Calls on unrelated talkgroups still
    never wait on each other, and the locks are taken in sorted order so two workers
    holding two keys each cannot deadlock.
    """
    for cls, key in _reconcile_lock_keys(
        talkgroup_ids=talkgroup_ids,
        tgid=tgid,
        frequencies=(frequency_hz,),
        unit_pair=unit_pair,
    ):
        session.execute(
            text("SELECT pg_advisory_xact_lock(:cls, :key)"), {"cls": cls, "key": key}
        )


def _reconcile_lock_keys(
    *,
    talkgroup_ids: list[int] | tuple[int, ...],
    tgid: int | None,
    frequencies: tuple[int | None, ...],
    unit_pair: tuple[int | None, int | None],
) -> list[tuple[int, int]]:
    """``(class, key)`` for every lock :func:`lock_reconcile` takes, in acquisition order."""
    ordered: list[tuple[int, int]] = []
    source, target = unit_pair
    if source is not None and target is not None:
        # A private call's channel is its pair of radios, so that is what serializes it.
        # Taken in a lock *class* of its own rather than folded into the tgid key space: the
        # two vocabularies are unrelated and a collision between them would make two calls
        # that can never match wait on each other.
        ordered.append(
            (UNIT_RECONCILE_LOCK_CLASS, (source * 2_654_435_761 + target) % 2_147_483_647)
        )
    keys = {(tg or 0) % 2_147_483_647 for tg in (talkgroup_ids or ())}
    if tgid is not None:
        keys.add(tgid % 2_147_483_647)
    for frequency in frequencies:
        if frequency is not None:
            # A frequency in Hz and a tgid are both plain integers, so the frequency keys
            # are pushed into a disjoint half of the space to keep the two from colliding.
            keys.add((frequency % 1_073_741_823) + 1_073_741_824)
    ordered.extend((RECONCILE_LOCK_CLASS, key) for key in sorted(keys))
    return ordered


def _lock_pair(session: Session, a: Call, b: Call) -> bool:
    """The channel locks for a pair ruled on by the batch pass — the same advisory locks the
    inline path takes (:func:`lock_reconcile`), over both calls' keys at once.

    The batch pass used to rule with none, so an upload of a third copy on the same channel
    could match, lock and swap against a row the pass was demoting in the same instant, and
    the archive kept two primaries for one transmission.

    **Never waits while holding.**  The pass keeps every lock of a chunk until the chunk
    commits, and an upload takes its own keys in sorted order and then row locks; waiting
    here for key B while holding key A from an earlier pair is the cycle that deadlocks
    against an upload holding B and waiting for A.  So every key is *tried*; on the first
    one that is busy this returns ``False`` having waited for nothing, and the caller
    commits (dropping all it holds) before waiting for them (:func:`_lock_pair_blocking`).
    """
    for cls, key in _pair_lock_keys(a, b):
        got = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:cls, :key)"), {"cls": cls, "key": key}
        ).scalar()
        if not got:
            return False
    return True


def _lock_pair_blocking(session: Session, a: Call, b: Call) -> None:
    """:func:`_lock_pair`'s keys, waited for — only with nothing else held."""
    for cls, key in _pair_lock_keys(a, b):
        session.execute(
            text("SELECT pg_advisory_xact_lock(:cls, :key)"), {"cls": cls, "key": key}
        )


def _pair_lock_keys(a: Call, b: Call) -> list[tuple[int, int]]:
    return _reconcile_lock_keys(
        talkgroup_ids=sorted({*(a.talkgroup_ids or ()), *(b.talkgroup_ids or ())}),
        tgid=None,
        frequencies=(a.frequency_hz, b.frequency_hz),
        unit_pair=(
            (a.radio_id, a.target_radio_id) if (a.kind or "group") == "unit" else (None, None)
        ),
    )


def _reload(session: Session, *calls: Call) -> None:
    """Re-read rows from the database over whatever the session last saw of them.

    ``lock_calls`` selects ids only, so a row loaded *before* the lock keeps the state it
    had then — and a concurrent pass may have demoted it in between.  This transaction's
    own pending changes are flushed first, so the re-read cannot discard them.
    """
    ids = [call.id for call in calls if call.id is not None]
    if ids:
        # The session is ``autoflush=False``: without this, a pending write of our own
        # would be overwritten by the re-read below.
        session.flush()
        session.scalars(
            select(Call).where(Call.id.in_(ids)).execution_options(populate_existing=True)
        ).all()


def _candidate_query(interval: Interval, system_id: int, cfg: ReconcileConfig) -> Select:
    """Everything in the same system whose interval could overlap this one.

    The SQL is the cheap half of the test — the time window and the channel — and
    :func:`matches` is the exact half, run in Python over the handful of rows this returns.
    """
    skew = timedelta(seconds=cfg.skew_s)
    if interval.kind == "unit":
        # The pair, not the talkgroup and not the frequency — and an unresolved end matches
        # nothing, which the empty ``channel`` below turns into ``false``.
        source, target = interval.unit_pair
        channel = (
            [and_(Call.radio_id == source, Call.target_radio_id == target)]
            if source is not None and target is not None
            else []
        )
    else:
        channel = (
            [Call.talkgroup_ids.overlap(list(interval.talkgroup_ids))]
            if interval.talkgroup_ids
            else []
        )
        if interval.frequency_hz is not None:
            channel.append(Call.frequency_hz == interval.frequency_hz)
    return (
        select(Call)
        .where(
            Call.system_id == system_id,
            # The cheap half of ``matches``'s first test, and it keeps a busy talkgroup's
            # rows out of a unit call's candidate set entirely.
            Call.kind == interval.kind,
            # An interval test is ``a.start <= b.end AND a.end >= b.start``, and the second
            # half bounds ``ended_at``, which is not indexed — on its own the planner would
            # sweep every call before this one.  ``MAX_CALL_SECONDS`` turns it into a bounded
            # range on ``started_at`` as well, which ``ix_calls_started_at`` answers.
            Call.started_at >= interval.started_at - skew - timedelta(seconds=MAX_CALL_SECONDS),
            Call.started_at <= interval.ended_at + skew,
            Call.ended_at >= interval.started_at - skew,
            or_(*channel) if channel else text("false"),
        )
        .order_by(Call.started_at, Call.id)
    )


def _find(
    session: Session,
    *,
    interval: Interval,
    system_id: int,
    sha256: str | None,
    file_path: str | None,
    exclude: set[int],
    cfg: ReconcileConfig,
) -> tuple[Call | None, MatchResult]:
    """The two rungs, over whichever shape the caller has.

    1. **exact** — identical ``sha256``, *within ``EXACT_HASH_MAX_SKEW_S`` of the caller's
       start*, in the same system, of the same kind and on the same channel (the talkgroup,
       frequency or radio pair :func:`matches` keys on).  The same bytes under two paths
       is the same call whatever the metadata says.
       A *missing* hash is never a match: an encrypted call pushed without audio has no
       bytes to hash, and ``sha256 IS NULL`` would otherwise make every audio-less call a
       copy of the first one.
    2. **interval** — :func:`matches` against every primary in the same system whose
       window could overlap.  Only primaries are considered: a row that already points at
       one is represented by the row it points at, which is where a merge would land.

    **A hash hit that resolves back to the caller is not a match.**  ``exclude`` holds the
    row being ruled on (and, in the batch pass, everything already resolved in this pass),
    and the hash query drops those rows — but the row it *returns* is then followed to its
    primary, and a copy of the caller (``B.duplicate_of == A.id``, the same bytes uploaded
    twice) resolves straight back to ``A``.  Returning it made :func:`reconcile_pair` rule
    on ``(A, A)``, tie, and write ``A.duplicate_of = A.id`` — tens of thousands of rows on a large archive,
    every one of them hidden from the API behind its own id (``app.repair_self_duplicates``
    is the repair).  So the primary is checked against ``exclude`` too, and an excluded one
    falls through to the interval rung exactly as if the hash had hit nothing, which is the
    guard the interval rung has always had on ``other.id``.

    **Nor is a hash hit from another day.**  Identical bytes prove the same *recording*,
    not the same transmission: a short recording with no voice in it — a squelch tail, a
    grant with nothing on it, 0.14-2 s — encodes to byte-identical MP3 bytes every time,
    so one hash spreads across hundreds of unrelated calls.  A 1.7 s unit call was once
    ruled a duplicate of a 1.7 s *group* call on another talkgroup a week earlier, rule
    ``identical sha256``; hundreds of rows were hidden that way and one hash alone covered
    over a hundred of them.  So the resolved primary's ``started_at``
    must be within ``EXACT_HASH_MAX_SKEW_S`` of this interval's start — two recorders'
    copies of one key-up differ by a second or two of clock skew, and a retransmitted file
    carries its original timestamp — and a hit outside that window is skipped exactly like
    an excluded one, leaving the interval rung (which does compare the channel and the time
    slice) to answer.
    """
    if sha256:
        # Same system and same kind before anything else: the scan is bounded, and rows
        # that can never be accepted below must not use up its budget.
        query = select(Call).where(
            Call.sha256 == sha256,
            Call.system_id == system_id,
            Call.kind == interval.kind,
        )
        if exclude:
            query = query.where(Call.id.notin_(exclude))
        if file_path is not None:
            query = query.where(Call.file_path != file_path)
        # Several rows can carry one hash (a file retransmitted more than once), and the
        # first by id may be a copy of the caller; the scan is bounded because the answer
        # is only ever "one of these resolves somewhere else", never a ranking.
        for exact in session.scalars(query.order_by(Call.id).limit(EXACT_HASH_SCAN_LIMIT)):
            primary = _primary_of(session, exact)
            if primary.id in exclude:
                continue
            # Same bytes, but from another day: a voiceless recording encodes identically
            # every time, so the hash alone is not evidence of one transmission.
            if abs((primary.started_at - interval.started_at).total_seconds()) > EXACT_HASH_MAX_SKEW_S:
                continue
            # …nor from another system, another kind of call or another channel.  Inside the
            # 60 s window a voiceless recording still hashes the same on every talkgroup (a
            # unit call and a group call a few seconds apart), so the primary must
            # stand on the channel the interval rung would ask for — ``matches`` sets ``key``
            # exactly when kind and channel agree, whatever it then says about the timing.
            if primary.system_id != system_id or (primary.kind or "group") != interval.kind:
                continue
            if not matches(interval, Interval.of(primary), cfg).key:
                continue
            return primary, MatchResult(True, "identical sha256", key=f"sha256:{sha256[:12]}")

    best: tuple[Call, MatchResult] | None = None
    for other in session.scalars(_candidate_query(interval, system_id, cfg)):
        if other.id in exclude or other.duplicate_of is not None:
            continue
        result = matches(interval, Interval.of(other), cfg)
        if not result.matched:
            continue
        # Several primaries can overlap one newcomer when a talkgroup is busy; the one
        # that shares the most time with it is the copy of *this* transmission.
        if best is None or result.overlap_s > best[1].overlap_s:
            best = (other, result)
    if best is not None:
        return best
    return None, MatchResult(False, "no candidate")


def find_match(
    session: Session,
    call: Call,
    cfg: ReconcileConfig,
    *,
    exclude: set[int] | None = None,
) -> tuple[Call | None, MatchResult]:
    """The call this row is another copy of, or ``(None, why not)``."""
    return _find(
        session,
        interval=Interval.of(call),
        system_id=call.system_id,
        sha256=call.sha256,
        file_path=call.file_path,
        exclude=(exclude or set()) | ({call.id} if call.id is not None else set()),
        cfg=cfg,
    )


def radio_pks_for_record(
    session: Session, record: Any, system_id: int
) -> tuple[int | None, int | None]:
    """The ``radios`` rows a record's two ends resolve to, before the call is written.

    Matching keys on radio *rows*, and a record carries the two numbers the air used.  This
    is the read that turns one into the other — deliberately a lookup and not an upsert:
    reconciliation runs before ``store_upload``, and minting a radio row here would create
    the directory entries the write path is responsible for.

    A radio the archive has never seen answers ``None``, which is the right answer twice
    over: there is no earlier call from a radio that does not exist yet, and no rung treats
    "unknown" as evidence.  Either end may be absent — a group call has no target at all,
    and its *source* is still worth resolving, because the ``short-call`` rung reads it.

    **The row is picked the way the write path picks it** — :func:`app.ingest.service.
    pick_radio_row`, which the store path's ``resolve_radio`` is built on.  One unit can
    have several ``radios`` rows in one system (the P25 LC opcode 9 extension rides some
    transmissions and not others, so the same radio is filed bare *and* qualified); asking
    only for "a row with this ``radio_id``" returned whichever the planner listed last, and
    a pair naming rows the stored calls do not carry matches nothing.  That is what left
    unit calls to the batch pass: on production, 14 of 20 private calls in six hours sat on
    screen as two rows for up to half an hour, while group calls — which key on a talkgroup
    and never on a radio row — reconciled inline 3,930 times out of 3,938.
    """
    from app.ingest.service import pick_radio_row  # noqa: PLC0415 - import cycle

    radio = getattr(record, "radio", None)
    source = radio.radio_id if radio is not None else None
    target = getattr(record, "target_radio_id", None)
    source_pk = (
        pick_radio_row(
            session,
            system_id,
            radio_id=source,
            wacn=getattr(radio, "wacn", None),
            home_system=getattr(radio, "home_system", None),
        )
        if source is not None
        else None
    )
    # The far end of a private call is resolved bare, exactly as ``store_upload`` resolves
    # it: the push carries a target radio id and nothing that qualifies it.
    target_pk = (
        pick_radio_row(session, system_id, radio_id=target, wacn=0, home_system=0)
        if target is not None
        else None
    )
    return (source_pk, target_pk)


def unit_pair_for_record(
    session: Session, record: Any, system_id: int
) -> tuple[int | None, int | None]:
    """The pair a *unit* call keys its channel on, or ``(None, None)``.

    Stricter than :func:`radio_pks_for_record` on purpose: a pair with an unresolved end is
    no pair at all, because :func:`matches` refuses to pair two unknown ends with each other.
    """
    source, target = radio_pks_for_record(session, record, system_id)
    return (source, target) if source is not None and target is not None else (None, None)


def find_match_for_record(
    session: Session,
    record: Any,
    system_id: int,
    cfg: ReconcileConfig,
) -> tuple[Call | None, MatchResult]:
    """The same question asked of a :class:`~app.ingest.parser.CallRecord` that has not
    been written yet — how both ingest paths learn, before the insert, that this recording
    is a second copy and must not count towards the directory's totals a second time."""
    # Resolved for both kinds: a unit call keys its *channel* on the pair, and a group call
    # still has a source radio, which the ``short-call`` rung reads.
    source_radio, target_radio = radio_pks_for_record(session, record, system_id)
    unit_pair = (
        (source_radio, target_radio)
        if getattr(record, "kind", None) == "unit"
        and source_radio is not None
        and target_radio is not None
        else (None, None)
    )
    return _find(
        session,
        interval=Interval.of_record(record, unit_pair, source_radio=source_radio),
        system_id=system_id,
        sha256=record.sha256,
        file_path=record.file_path,
        exclude=set(),
        cfg=cfg,
    )


def _primary_of(session: Session, call: Call) -> Call:
    """Follow ``duplicate_of`` to the row that is actually primary (one hop is all the
    schema ever builds, but the loop costs nothing and cannot spin: it stops at ``None``)."""
    seen: set[int] = set()
    while call.duplicate_of is not None and call.duplicate_of not in seen:
        seen.add(call.id)
        parent = session.get(Call, call.duplicate_of)
        if parent is None:
            break
        call = parent
    return call


# --------------------------------------------------------------------------------------
# scoring (docs/RECONCILIATION.md "Choosing the primary")
# --------------------------------------------------------------------------------------


@dataclass
class Score:
    """One copy's score, with every component kept so the decision is explainable."""

    call_id: int | None
    total: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)
    #: things worth saying about the score that are not themselves points, such as a
    #: duration long enough to be two calls joined rather than one recorded better
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "total": round(self.total, 2),
            "parts": {k: round(v, 2) for k, v in self.parts.items()},
            "notes": list(self.notes),
        }


def _metadata_fields(session: Session, call: Call) -> int:
    """How many of the eight things a recorder can know this row actually knows."""
    has_sources = (
        session.scalar(select(func.count()).select_from(CallSource).where(CallSource.call_id == call.id))
        if call.id is not None
        else 0
    )
    return sum(
        1
        for present in (
            call.radio_id is not None or call.radio_raw,
            bool(call.talker_alias),
            call.encrypted is not None,
            call.emergency is not None,
            call.frequency_hz is not None,
            call.site_id is not None,
            call.patch_id is not None or len(call.talkgroup_ids or ()) > 1,
            bool(has_sources),
        )
        if present
    )


def quality_score(quality: dict[str, Any] | None) -> float | None:
    """How well a receiver heard the call: ``voice_frames − 5 × bad_frames``.

    ``None`` when there is no ``voice_frames`` to score — a report that does not say how
    much voice it decoded says nothing comparable, and the copies then fall back to the
    other signals rather than being ranked on a guess.  A missing ``bad_frames`` is read as
    zero, which is what a recorder that counts only good frames means by omitting it.
    """
    if not quality:
        return None
    voice = quality.get("voice_frames")
    if not isinstance(voice, (int, float)) or isinstance(voice, bool) or not math.isfinite(voice):
        return None
    bad = quality.get("bad_frames")
    penalty = (
        float(bad)
        if isinstance(bad, (int, float)) and not isinstance(bad, bool) and math.isfinite(bad)
        else 0.0
    )
    score = float(voice) - BAD_FRAME_PENALTY * penalty
    # An unscoreable copy is `None`, never a NaN or an infinity: the callers compare these,
    # and a non-finite score makes every comparison False — which the upload path's `<=`
    # reads as "the incoming copy won" and would swap good audio for garbage.
    return score if math.isfinite(score) else None


def _source_quality(call: Call, other: Call | None) -> float:
    """1 when this copy's receiver demonstrably heard the call better, 0 otherwise.

    The recorders report what their own P25 decoder counted — how many voice frames it got
    out and how many came out errored — and that is the only signal here that describes the
    *audio* rather than the row around it.  Two tuners on one transmission can agree on
    every other component and still differ by a hole in the speech, which is exactly the
    difference a listener notices, so it ranks above them all: 700 clears an exact time
    source, a full metadata set, the encoding, a transcript and the recorder preference put
    together (587), while staying well under the duration and audio ranks, so a materially
    longer or an actually-present recording still wins.

    Deliberately a flag, not a scaled magnitude: frame counts are unbounded and scale with
    the length of the call, so "twice as many good frames" is not twice as good a copy —
    it is more often a longer one, which ``duration`` already scores.  What is being
    asserted is only "this receiver heard it better", and that is worth one fixed amount.

    Silent — no term at all — unless both copies carry a scoreable report, they disagree,
    and they do not name the same receiver.  Comparing an instance with itself says nothing
    about which of two receivers to keep, and a missing or non-finite report says nothing
    at all (:func:`quality_score` returns ``None`` for both).
    """
    if other is None:
        return 0.0
    mine = quality_score(call.source_quality)
    theirs = quality_score(other.source_quality)
    if mine is None or theirs is None or mine == theirs:
        return 0.0
    instance, other_instance = call.source_instance, other.source_instance
    if instance and other_instance and instance == other_instance:
        return 0.0
    return 1.0 if mine > theirs else 0.0


def _quality(call: Call) -> float:
    """0…1 from the encoding, and 0 when there is no audio to have an encoding."""
    if call.audio_state != "present":
        return 0.0
    bitrate = min(float(call.bitrate or 0), 64_000.0) / 64_000.0
    rate = min(float(call.sample_rate or 0), 22_050.0) / 22_050.0
    return round((bitrate + rate) / 2, 4)


def _preference(call: Call, cfg: ReconcileConfig) -> float:
    """1 for the most preferred recorder, 0 for one that is not on the list at all."""
    order = cfg.prefer_sources
    if not order or call.ingest_source not in order:
        return 0.0
    return (len(order) - order.index(call.ingest_source)) / len(order)


def score_call(
    session: Session,
    call: Call,
    other: Call | None,
    cfg: ReconcileConfig,
) -> Score:
    """Score one copy of a transmission, relative to the copy it is being compared with.

    Duration and source quality are pairwise; everything else is a property of the row
    alone.  Duration is pairwise because "more complete" is only meaningful against the
    other copy: the credit is the relative excess over the other, capped at ``merge_ratio``
    because a copy several times longer is trunkd's hang timer having merged two
    transmissions, which is worse and not better.  Source quality is pairwise because it is
    a comparison between two receivers and means nothing about one alone.
    """
    parts: dict[str, float] = {}
    notes: list[str] = []
    parts["audio"] = W_AUDIO if call.audio_state == "present" else 0.0

    duration = float(call.duration_s or 0)
    parts["duration"] = 0.0
    if other is not None:
        other_duration = float(other.duration_s or 0)
        ratio = duration / other_duration if other_duration > 0 else 1.0
        capped = W_DURATION * min(ratio - 1.0, cfg.merge_ratio - 1.0)
        if other_duration <= cfg.fragment_s < duration:
            # The other copy is a *fragment* — a late grant caught the tail of a call this
            # one recorded whole.  Being many times longer than a 1.2 s scrap is not
            # evidence of merged calls, it is the whole point, so the credit stands.
            parts["duration"] = capped
            notes.append("other_is_fragment")
        elif ratio > cfg.merge_ratio:
            # Past the merge ratio, against a copy long enough to be a call in its own
            # right, the extra length stops being evidence of a more complete recording
            # and starts being evidence of two transmissions joined by trunkd's hang
            # timer.  The credit is capped there and then withdrawn: a 24 s copy of an 8 s
            # call is not three times the call, it is two calls.
            notes.append("merged_suspect")
        elif ratio > 1.0:
            parts["duration"] = capped

    parts["source_quality"] = W_SOURCE_QUALITY * _source_quality(call, other)
    parts["time_source"] = W_TIME_SOURCE if call.time_source in EXACT_TIME_SOURCES else 0.0
    parts["metadata"] = W_METADATA * _metadata_fields(session, call)
    parts["quality"] = W_QUALITY * _quality(call)
    parts["transcript"] = W_TRANSCRIPT if call.transcript_status in FINISHED_TRANSCRIPT else 0.0
    parts["recorder"] = W_PREFERENCE * _preference(call, cfg)
    return Score(call_id=call.id, total=sum(parts.values()), parts=parts, notes=notes)


def _deciding_rule(winner: Score, loser: Score) -> str:
    """The highest-ranked component the two disagree on — the reason, in one word."""
    for name in (
        "audio",
        "duration",
        "source_quality",
        "time_source",
        "metadata",
        "quality",
        "transcript",
        "recorder",
    ):
        if abs(winner.parts.get(name, 0.0) - loser.parts.get(name, 0.0)) > 1e-9:
            return name
    return "tie"


# --------------------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------------------

#: scalar columns folded from the loser into the winner when the winner has nothing there
_MERGE_COLUMNS = (
    "radio_id",
    "radio_raw",
    "talker_alias",
    "encrypted",
    "emergency",
    "frequency_hz",
    "site_id",
    "channel",
    "decoder",
    "audio_type",
    "signal_db",
    "noise_db",
    "freq_error",
    "recorder_call_num",
    "talkgroup_alias_at_record",
)

#: keys promoted out of the loser's ``raw_meta`` into the winner's, when absent there.
#: These are the facts trunkd knows about an encrypted call that no other recorder does.
_RAW_META_PROMOTE = ("algorithm", "keyId", "key_id", "nac")


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def merge_metadata(session: Session, primary: Call, other: Call) -> list[str]:
    """Fold everything the loser knows and the winner does not into the winner.

    Only NULL/empty fields are filled — a merge never overwrites what the winning recorder
    itself reported, and never touches a human's edits.  Returns the names of the fields it
    filled, which go into ``reconcile_reason`` so a merge is auditable.
    """
    filled: list[str] = []
    for column in _MERGE_COLUMNS:
        if _empty(getattr(primary, column, None)) and not _empty(getattr(other, column, None)):
            setattr(primary, column, getattr(other, column))
            filled.append(column)
    if primary.radio_id is not None and "radio_id" in filled:
        primary.radio_resolved = True

    # Patch membership: a patch the winner did not see makes its member list the truth
    # about which talkgroups this transmission was on, which is what "calls involving
    # talkgroup X" reads.
    if primary.patch_id is None and other.patch_id is not None:
        primary.patch_id = other.patch_id
        merged_tgids = sorted({*(primary.talkgroup_ids or ()), *(other.talkgroup_ids or ())})
        if merged_tgids != sorted(primary.talkgroup_ids or ()):
            primary.talkgroup_ids = merged_tgids
            filled.append("talkgroup_ids")
        primary.call_type = "patch"
        filled.append("patch_id")
    elif not primary.talkgroup_ids and other.talkgroup_ids:
        primary.talkgroup_ids = list(other.talkgroup_ids)
        filled.append("talkgroup_ids")
    if primary.talkgroup_id is None and other.talkgroup_id is not None:
        primary.talkgroup_id = other.talkgroup_id
        filled.append("talkgroup_id")
    # The far end of a private call: one recorder may have decoded the grant and the other
    # only the traffic channel, and the pair is the only thing that identifies the call.
    if primary.target_radio_id is None and other.target_radio_id is not None:
        primary.target_radio_id = other.target_radio_id
        filled.append("target_radio_id")

    # The per-transmission radios, but only when the winner has none of its own: two
    # recorders' source lists are two readings of the same thing and interleaving them
    # would invent transmissions that never happened.
    mine = session.scalar(
        select(func.count()).select_from(CallSource).where(CallSource.call_id == primary.id)
    )
    if not mine:
        rows = list(
            session.scalars(
                select(CallSource).where(CallSource.call_id == other.id).order_by(CallSource.seq)
            )
        )
        for row in rows:
            session.add(
                CallSource(
                    call_id=primary.id,
                    radio_id=row.radio_id,
                    src=row.src,
                    pos_s=row.pos_s,
                    len_s=row.len_s,
                    emergency=row.emergency,
                    tag=row.tag,
                    seq=row.seq,
                )
            )
        if rows:
            filled.append("call_sources")
            if primary.radio_id is None and rows[0].radio_id is not None:
                primary.radio_id = rows[0].radio_id
                primary.radio_resolved = True

    # The loser's own words, verbatim, under a key nothing parses back.  A dict is built
    # rather than mutated because SQLAlchemy only notices a JSONB column that was replaced.
    raw = dict(primary.raw_meta or {})
    merged_from = dict(raw.get("merged_from") or {})
    merged_from[str(other.id)] = {
        "ingest_source": other.ingest_source,
        "file_path": other.file_path,
        "started_at": other.started_at.isoformat(),
        "duration_s": float(other.duration_s or 0),
        "time_source": other.time_source,
        "audio_state": other.audio_state,
        "raw_meta": other.raw_meta or {},
    }
    raw["merged_from"] = merged_from
    for key in _RAW_META_PROMOTE:
        value = (other.raw_meta or {}).get(key)
        if value is not None and raw.get(key) is None:
            raw[key] = value
            filled.append(f"raw_meta.{key}")
    primary.raw_meta = raw
    filled.append("raw_meta.merged_from")
    return filled


# --------------------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------------------


@dataclass
class ReconcileOutcome:
    """What reconciliation did to one pair."""

    matched: bool = False
    primary_id: int | None = None
    duplicate_id: int | None = None
    swapped: bool = False
    merged_fields: list[str] = field(default_factory=list)
    rule: str = ""
    job_id: int | None = None
    #: the audio-processing job queued for the *new* primary when a swap left it with no
    #: processed copy of its own.  ``None`` when nothing had to be queued.
    audio_job_id: int | None = None
    reason: dict[str, Any] = field(default_factory=dict)


def reconcile_pair(
    session: Session,
    newcomer: Call,
    other: Call,
    settings: Settings,
    *,
    cfg: ReconcileConfig | None = None,
    match: MatchResult | None = None,
    forced_winner: Call | None = None,
    newcomer_was_primary: bool = False,
    requeue_class: str = "live",
) -> ReconcileOutcome:
    """Rule on two copies of one transmission: pick the primary, merge, swap if needed.

    ``other`` must be a primary row.  ``newcomer`` is the copy being ruled on — freshly
    ingested, or the later of two rows in the batch pass.  A tie leaves the existing
    primary in place, which is what makes re-running the batch pass a no-op.

    ``newcomer_was_primary`` says the newcomer already stood as a primary row before this
    ruling — the batch and repair passes, where both sides of the pair were primaries and a
    live list may be showing either of them.  It decides nothing about the ruling; it only
    says whether demoting the newcomer has to be announced to live clients
    (:func:`_notify_superseded`).  At ingest it is false: the newcomer is announced *after*
    this returns, as a duplicate, and the live feed filters it out.

    ``requeue_class`` is the priority class a *swap* re-enqueues the new primary at
    (:func:`_swap`).  The live and scheduled paths leave it ``live``, which is the behavior
    this has always had; a bulk repair walking years of archive passes ``backlog`` so its
    re-transcriptions queue behind the live feed instead of in front of it.
    """
    if newcomer.id is not None and newcomer.id == other.id:
        # A row is not a copy of itself.  Nothing should ever ask — :func:`_find` refuses to
        # return the caller on either rung — but this ran for days on a live archive and wrote
        # ``duplicate_of = id`` onto tens of thousands of calls, hiding each one from every listing behind
        # its own id, so the last line of defense is here, where the write happens.  A no-op
        # outcome rather than an exception: the caller is an ingest or a batch pass, and a
        # raise here would fail an upload over a row that is already stored correctly.
        log.warning("reconcile: refusing to rule call %s a duplicate of itself", newcomer.id)
        return ReconcileOutcome(matched=False, primary_id=newcomer.id, rule="self")
    cfg = cfg or ReconcileConfig.of(settings)
    # Both call rows, in id order, before anything in this ruling writes a job row (§5.2).
    # Ordered so two passes ruling on the same pair from opposite ends cannot deadlock with
    # each other either, and taken here so the whole ruling — cancel, swap, re-enqueue —
    # runs with ``calls`` already locked.
    lock_calls(session, [newcomer.id, other.id])
    # …and the rows as they are *now*, not as the match found them: a batch pass
    # may have demoted ``other`` between the match and the lock, and ruling on the stale
    # copy would write it back to primary beside the row that displaced it.
    _reload(session, newcomer, other)
    if forced_winner is None:
        settled = _settle(session, newcomer, other, newcomer_was_primary=newcomer_was_primary)
        if isinstance(settled, ReconcileOutcome):
            return settled
        other = settled
    score_new = score_call(session, newcomer, other, cfg)
    score_old = score_call(session, other, newcomer, cfg)
    if forced_winner is not None:
        newcomer_wins = forced_winner.id == newcomer.id
        rule = "manual"
    else:
        newcomer_wins = score_new.total > score_old.total
        rule = _deciding_rule(score_new, score_old) if newcomer_wins else _deciding_rule(score_old, score_new)

    winner, loser = (newcomer, other) if newcomer_wins else (other, newcomer)
    filled = merge_metadata(session, winner, loser)

    now = datetime.now(UTC)
    reason: dict[str, Any] = {
        "rule": rule,
        "winner": "newcomer" if newcomer_wins else "existing",
        "winner_id": winner.id,
        "match": (match or MatchResult(True, "unknown")).as_dict(),
        "scores": {str(score_new.call_id): score_new.as_dict(), str(score_old.call_id): score_old.as_dict()},
        "merged_fields": filled,
        "at": now.isoformat(),
    }

    job_id = None
    audio_job_id = None
    if newcomer_wins:
        job_id, audio_job_id = _swap(session, winner, loser, settings, requeue_class=requeue_class)
    else:
        loser.duplicate_of = winner.id
        if newcomer_was_primary:
            # The batch pass: this row was a primary a moment ago, so a live list may be
            # showing it.  At ingest the flag is false and nothing is sent — the newcomer
            # was never on screen.
            _notify_superseded(session, loser, winner)
            # …and, having been a primary, it may already have been exported or queued for
            # export.  Same move as the swap makes in the other direction: the ledger
            # follows the primary so the transmission is uploaded at most once.
            from app.exporters.ledger import repoint_exports  # noqa: PLC0415 - cycle

            session.flush()
            repoint_exports(session, winner_id=winner.id, loser_id=loser.id)
        _leave_conversation(session, loser)
        if loser.transcript_status in ("pending", "queued"):
            loser.transcript_status = "skipped"
            _cancel_jobs(session, loser.id)
        # A copy is never played, so a processing pass over it would write a file nobody
        # opens.  Unconditional, unlike the transcription cancel above: the audio queue has
        # no per-row status to gate on, and in the batch pass this loser *was* a primary a
        # moment ago and may well have a pass waiting for it.
        _cancel_audio_jobs(session, loser.id)
    if job_id is not None:
        reason["requeued_job"] = job_id
    if audio_job_id is not None:
        reason["requeued_audio_job"] = audio_job_id

    for row in (winner, loser):
        row.reconciled_at = now
        # Each row gets its own copy: two ORM attributes sharing one dict is a mutation
        # on one row silently rewriting the other's audit trail.
        row.reconcile_reason = dict(reason)

    log.info(
        "reconcile: call %s and %s are one transmission (%s); primary %s by %s",
        newcomer.id,
        other.id,
        (match or MatchResult(True, "unknown")).rule,
        winner.id,
        rule,
    )
    return ReconcileOutcome(
        matched=True,
        primary_id=winner.id,
        duplicate_id=loser.id,
        swapped=newcomer_wins,
        merged_fields=filled,
        rule=rule,
        job_id=job_id,
        audio_job_id=audio_job_id,
        reason=reason,
    )


#: how many times :func:`_settle` follows a primary that keeps moving under it
_SETTLE_ATTEMPTS = 3


def _settle(
    session: Session, newcomer: Call, other: Call, *, newcomer_was_primary: bool
) -> Call | ReconcileOutcome:
    """The primary ``newcomer`` should be ruled against, once both rows are locked and fresh.

    Returns that row (``other`` itself, or the row a concurrent ruling moved its seat to),
    or an outcome when there is nothing left to rule on: the pair was already joined by
    someone else, or the newcomer — a primary in the batch and repair passes — was demoted
    underneath the caller.  At ingest the newcomer is uncommitted, so only ``other`` can
    have moved.
    """
    if newcomer_was_primary and newcomer.duplicate_of is not None:
        if newcomer.duplicate_of == other.id:
            return ReconcileOutcome(
                matched=True, primary_id=other.id, duplicate_id=newcomer.id, rule="already reconciled"
            )
        log.info("reconcile: call %s was demoted concurrently; not ruling on it", newcomer.id)
        return ReconcileOutcome(matched=False, primary_id=newcomer.duplicate_of, rule="stale")
    for _ in range(_SETTLE_ATTEMPTS):
        if other.duplicate_of is None:
            return other
        current = _primary_of(session, other)
        if current.id == newcomer.id:
            return ReconcileOutcome(
                matched=True, primary_id=newcomer.id, duplicate_id=other.id, rule="already reconciled"
            )
        lock_calls(session, [current.id])
        _reload(session, current)
        other = current
    if other.duplicate_of is None:
        return other
    log.warning("reconcile: primary of call %s kept moving; not ruling on it", newcomer.id)
    return ReconcileOutcome(matched=False, primary_id=newcomer.id, rule="stale")


def _notify_superseded(session: Session, loser: Call, winner: Call) -> None:
    """Tell the live feed that ``loser`` is no longer the primary copy (§3.8).

    A lightweight frame on purpose: the demoted row no longer satisfies the call filters
    (they exclude duplicates), so there is no call object to attach and nothing for the
    client to re-read.  The id is enough — the list drops that row, and the winner arrives
    on its own ``call.new``.
    """
    if loser.id is None or winner.id is None:  # pragma: no cover - unflushed rows
        return
    notify(
        session,
        "call.superseded",
        {
            "id": loser.id,
            "by": winner.id,
            "talkgroup_id": loser.talkgroup_id,
            "started_at": loser.started_at.isoformat() if loser.started_at else None,
        },
    )


def _leave_conversation(session: Session, call: Call) -> None:
    """Take a call out of its conversation, keeping the thread's ``call_count`` honest.

    A duplicate is not a second key-up in the exchange — it is the same one, twice — so it
    must not inflate the count the UI shows for the thread.
    """
    if call.conversation_id is None:
        return
    conv = session.get(Conversation, call.conversation_id)
    if conv is not None:
        conv.call_count = max(0, conv.call_count - 1)
        if conv.first_call_id == call.id:
            conv.first_call_id = None
    call.conversation_id = None


def _cancel_jobs(session: Session, call_id: int) -> None:
    """Stop work that is now pointless: a row that just became a duplicate.

    The call row is locked first and the caller's pending write to it flushed with it
    (:func:`app.db.lock_calls`): ``calls`` before ``transcription_jobs`` is the one order
    these two tables are ever taken in (§5.2), and the caller's
    ``loser.transcript_status = "skipped"`` is an ORM write that would otherwise land at
    commit, behind the cancel below.
    """
    lock_calls(session, call_id)
    session.execute(
        update(TranscriptionJob)
        .where(TranscriptionJob.call_id == call_id, TranscriptionJob.status == "queued")
        .values(status="cancelled", finished_at=datetime.now(UTC))
    )


def _cancel_audio_jobs(session: Session, call_id: int) -> int:
    """Cancel the queued audio-processing passes for a row that just became a copy.

    Only ``queued`` rows, the same rule the transcription cancel uses: a ``running`` job is
    mid-ffmpeg on a file this function has no business pulling out from under it, and the
    processed copy it writes is harmless — it simply sits beside a row nobody plays.

    Returns how many were cancelled, which is what makes "the copy's job is gone" testable
    without a second query.
    """
    return int(
        session.execute(
            update(AudioJob)
            .where(AudioJob.call_id == call_id, AudioJob.status == "queued")
            .values(status="cancelled", finished_at=datetime.now(UTC))
        ).rowcount
        or 0
    )


def _needs_audio_pass(call: Call) -> bool:
    """Does this row still lack a processed copy made from the recording it now points at?

    Two copies of one transmission are two different recordings, so the processed file
    never transfers with the primary seat: a row that has never been through the chain has
    ``processed_path IS NULL``, and a row whose ``file_path`` was replaced under it (the
    best-source path) has one whose name no longer matches its source.  Both need a pass.
    """
    if (call.audio_state or "present") not in ("present", "archived"):
        return False
    if not call.processed_path:
        return True
    if not call.file_path:  # pragma: no cover - a row with a processed copy and no source
        return False
    from app.audio import processed_path_for  # noqa: PLC0415 - cycle

    return Path(call.processed_path).name != processed_path_for(call.file_path).name


def _requeue_audio(
    session: Session, winner: Call, settings: Settings, *, requeue_class: str = "live"
) -> int | None:
    """Queue the processing pass for a row that has just become the primary.

    The job that ran at ingest was queued for whichever copy was primary *then*; when the
    seat moves, the row now being played has no processed audio and the row that has some
    is hidden.  Without this the new primary stays unprocessed forever — about 8 % of live
    primaries, measured, because nothing else ever revisits a call that was already ingested.

    The band is the caller's, resolved against the talkgroup exactly as the push path
    resolves an upload's (:meth:`app.ingest.flow.PriorityChoice.resolve`), so ``live`` is
    the talkgroup's own priority — what the upload would have used — and a bulk repair's
    ``backlog`` queues behind the live feed.
    """
    if not _needs_audio_pass(winner):
        return None
    from app.ingest.flow import parse_priority  # noqa: PLC0415 - cycle
    from app.ingest.service import enqueue_audio_process  # noqa: PLC0415 - cycle
    from app.models import Talkgroup  # noqa: PLC0415

    talkgroup = session.get(Talkgroup, winner.talkgroup_id) if winner.talkgroup_id else None
    priority = parse_priority(requeue_class).resolve(
        talkgroup.priority if talkgroup is not None else None
    )
    return enqueue_audio_process(session, winner, settings, priority=priority)


def _swap(
    session: Session,
    winner: Call,
    loser: Call,
    settings: Settings,
    *,
    requeue_class: str = "live",
) -> tuple[int | None, int | None]:
    """The newcomer takes the primary row: every pointer, the conversation, the queue.

    Alerts that already went out are left alone — they were true when they were sent, and
    a correction nobody asked for is noise.

    ``requeue_class`` names the band the displaced transcription comes back in
    (:mod:`app.ingest.flow`): ``live`` — the default, and every live and scheduled caller —
    keeps the historical "priority 1 when the copy it displaced had text"; ``backlog``
    resolves the talkgroup's own priority into the backlog band instead, so a repair
    promoting hundreds of thousands of 2022-2024 primaries cannot push a live call behind
    them.
    """
    session.flush()
    # Everything that pointed at the old primary now points at the new one, in one
    # statement, so a chain of three copies collapses to one primary and two duplicates.
    session.execute(
        update(Call)
        .where(Call.duplicate_of == loser.id, Call.id != winner.id)
        .values(duplicate_of=winner.id)
    )
    winner.duplicate_of = None
    loser.duplicate_of = winner.id
    # The export ledger follows the primary too, and for the sharpest reason in this
    # function: discovery looks for primaries with no ledger row, and the row it would find
    # here is a transmission somebody has already published (docs/EXPORTERS.md, "One upload
    # per transmission").
    from app.exporters.ledger import repoint_exports  # noqa: PLC0415 - cycle

    repoint_exports(session, winner_id=winner.id, loser_id=loser.id)
    # The row the live lists are showing has just stopped being the primary.  Nothing else
    # tells them: the newcomer arrives as its own ``call.new``, and a ``call.new`` for the
    # demoted row would be filtered out (the call filters exclude duplicates), so without
    # this frame the operator keeps both copies on screen.
    _notify_superseded(session, loser, winner)

    # The conversation membership follows the primary.  Three cases, and the middle one is
    # the only move: whichever thread ends up representing this transmission must contain
    # it exactly once.
    conversation_id = loser.conversation_id
    if winner.conversation_id is not None:
        # The winner already sits in a thread of its own — the batch pass reconciling two
        # rows that were each a primary.  Nothing moves; the loser just leaves its own,
        # and its thread's count comes down by the one transmission it never added.
        _leave_conversation(session, loser)
    elif conversation_id is not None:
        conv = session.get(Conversation, conversation_id)
        # A move, not a departure: the thread still holds the same number of
        # transmissions, so ``call_count`` is left alone.  Its bounds are widened rather
        # than recomputed — the loser is still part of the same exchange, it is simply no
        # longer the row that represents it.
        loser.conversation_id = None
        winner.conversation_id = conversation_id
        if conv is not None:
            conv.started_at = min(conv.started_at, winner.started_at)
            conv.ended_at = max(conv.ended_at, winner.ended_at)
            if conv.first_call_id == loser.id:
                conv.first_call_id = winner.id
    else:
        from app.ingest.service import assign_conversation  # noqa: PLC0415 - cycle

        assign_conversation(session, winner, settings.conversation_gap_seconds)

    # Transcription.  Audio is never copied between copies — two tuners produce two
    # different recordings — so the displaced transcript does not transfer; the new
    # primary is queued ahead of the backlog instead, because the archive briefly has a
    # primary with no text where it used to have one.
    job_id = None
    had_transcript = loser.transcript_status == "done"
    if winner.audio_state == "present" and winner.transcript_status in ("pending", "skipped", "empty"):
        from app.ingest.service import enqueue_transcription  # noqa: PLC0415 - cycle
        from app.models import Talkgroup  # noqa: PLC0415

        talkgroup = session.get(Talkgroup, winner.talkgroup_id) if winner.talkgroup_id else None
        # The talkgroup's own policy still decides *whether* to transcribe — a swap must
        # not start transcribing a channel nobody asked for, or a squelch tail.  Only the
        # place in the queue is overridden, and only when the copy this one displaced had
        # text: the archive would otherwise show a primary with none where it had one.
        priority = 1 if had_transcript else None
        if requeue_class != "live":
            from app.ingest.flow import parse_priority  # noqa: PLC0415 - cycle

            # A named class, resolved against the talkgroup exactly as the push path
            # resolves an upload's: the band is the caller's decision, the base is still
            # the talkgroup's own.
            priority = parse_priority(requeue_class).resolve(
                talkgroup.priority if talkgroup is not None else None
            )
        job_id = enqueue_transcription(session, winner, talkgroup, settings, priority=priority)

    # Audio processing, for the same reason and with the same asymmetry: the pass that ran
    # at ingest was queued for the row that was primary then, and the processed file it
    # wrote belongs to the loser's recording.  The winner is the row that will be played
    # from here, so it gets the pass it never had.
    audio_job_id = _requeue_audio(session, winner, settings, requeue_class=requeue_class)

    if loser.transcript_status in ("pending", "queued"):
        loser.transcript_status = "skipped"
        _cancel_jobs(session, loser.id)
    # The displaced row is a copy now; a queued pass over it would spend a worker writing a
    # file no listener can reach.
    _cancel_audio_jobs(session, loser.id)
    return job_id, audio_job_id


# --------------------------------------------------------------------------------------
# the ingest hook (§5.1 step 4)
# --------------------------------------------------------------------------------------


def reconcile_call(
    session: Session,
    call: Call,
    settings: Settings,
    *,
    match_id: int | None = None,
    match: MatchResult | None = None,
) -> ReconcileOutcome:
    """Reconcile one freshly written call against the archive.

    Both ingest paths call this with the row already inserted (and, when they looked one up
    before the insert to decide whether to count it in the directory's totals, with the
    match they found).  Returns what happened; the caller only needs
    :attr:`ReconcileOutcome.matched` to answer the recorder.
    """
    cfg = ReconcileConfig.of(settings)
    if not cfg.enabled:  # pragma: no cover - the disabled path never reaches here
        return ReconcileOutcome()
    if match_id is not None:
        other = session.get(Call, match_id)
    else:
        other, match = find_match(session, call, cfg)
    if other is None:
        return ReconcileOutcome()
    other = _primary_of(session, other)
    if other.id == call.id:  # pragma: no cover - defensive
        return ReconcileOutcome()
    return reconcile_pair(session, call, other, settings, cfg=cfg, match=match)


# --------------------------------------------------------------------------------------
# the push path (§5.1 step 0)
# --------------------------------------------------------------------------------------


@dataclass
class UploadDecision:
    """What the endpoint has to know *before* it writes the pushed call.

    ``reject`` is only ever true with ``RECONCILE_ENABLED=false``, where the old dedupe
    still answers ``duplicate call rejected`` and stores nothing.  With reconciliation on,
    a matched push is written like any other and ``matched_id`` names the copy it will be
    compared with — which is also what says the directory's counters must not move again.
    """

    matched_id: int | None = None
    reject: bool = False
    match: MatchResult | None = None


def reconcile_lookup(session: Session, record: Any, settings: Settings) -> UploadDecision:
    """Take the channel locks and find the copy this push duplicates, if any."""
    from app.ingest.service import find_duplicate, lock_dedupe  # noqa: PLC0415 - cycle

    cfg = ReconcileConfig.of(settings)
    if not cfg.enabled:
        lock_dedupe(session, record.tgid, record.started_at)
        duplicate_of = find_duplicate(session, record, settings)
        return UploadDecision(matched_id=duplicate_of, reject=duplicate_of is not None)

    from app.ingest.service import resolve_system_id  # noqa: PLC0415 - cycle

    wacn = record.radio.wacn if record.radio and record.radio.wacn else None
    # This runs *before* ``store_upload``, so it is the first thing that could mint a
    # ``System`` from a label the recorder never sent — and for a while it was: the write
    # path was fixed and an unlabeled push still created its system here, one step
    # earlier.  Raises ``UnknownSystem``, which the endpoint answers as a bad push.
    system_id = resolve_system_id(
        session,
        record.system,
        protocol=record.decoder,
        wacn=wacn,
        fill_protocol=not getattr(record, "decoder_from_form", False),
    )
    # The locks are taken after the system is resolved, not before: a unit call's channel
    # key is its pair of ``radios`` rows, and those are per-system.  Resolving the system
    # takes no lock and writes nothing (it raises rather than minting one), so nothing has
    # happened yet that the lock exists to serialize.
    lock_reconcile(
        session,
        talkgroup_ids=record.talkgroup_ids,
        tgid=record.tgid,
        frequency_hz=record.frequency_hz,
        unit_pair=(
            unit_pair_for_record(session, record, system_id)
            if getattr(record, "kind", None) == "unit"
            else (None, None)
        ),
    )
    matched, match = find_match_for_record(session, record, system_id, cfg)
    return UploadDecision(
        matched_id=matched.id if matched is not None else None, match=match
    )


def reconcile_upload(
    session: Session, call: Call, decision: UploadDecision, settings: Settings
) -> ReconcileOutcome:
    """Rule on a pushed call against the copy :func:`reconcile_lookup` found."""
    if decision.matched_id is None:
        return ReconcileOutcome()
    other = session.get(Call, decision.matched_id)
    if other is None:  # pragma: no cover - it was there a moment ago
        return ReconcileOutcome()
    return reconcile_pair(
        session, call, _primary_of(session, other), settings, match=decision.match
    )


# --------------------------------------------------------------------------------------
# the batch pass (§5.1 step 4, backlog)
# --------------------------------------------------------------------------------------


@dataclass
class ReconcileReport:
    """One batch pass, in the shape the CLI prints and the log line renders."""

    scanned: int = 0
    pairs: int = 0
    swaps: int = 0
    merges: int = 0
    commits: int = 0
    elapsed_s: float = 0.0
    dry_run: bool = False
    since: datetime | None = None
    system: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "pairs": self.pairs,
            "swaps": self.swaps,
            "merges": self.merges,
            "commits": self.commits,
            "elapsed_s": round(self.elapsed_s, 3),
            "dry_run": self.dry_run,
            "since": self.since.isoformat() if self.since else None,
            "system": self.system,
        }


def _arrived_after(a: Call, b: Call) -> bool:
    """Did ``a`` reach the archive after ``b``?  Ingest time, with the id as the tie-break."""
    if a.ingested_at is not None and b.ingested_at is not None and a.ingested_at != b.ingested_at:
        return a.ingested_at > b.ingested_at
    return (a.id or 0) > (b.id or 0)


def last_pass_at(session: Session) -> datetime | None:
    """The watermark the scheduled pass keeps in the ``settings`` table."""
    row = session.get(Setting, LAST_PASS_KEY)
    if row is None or not row.value:
        return None
    try:
        return datetime.fromisoformat(str(row.value))
    except ValueError:  # pragma: no cover - a hand-edited row
        return None


def _set_last_pass(session: Session, when: datetime) -> None:
    row = session.get(Setting, LAST_PASS_KEY)
    if row is None:
        row = Setting(key=LAST_PASS_KEY, description="Reconciliation batch pass watermark")
        session.add(row)
    row.value = when.isoformat()


def reconcile_batch(
    session: Session,
    settings: Settings,
    *,
    since: datetime | None = None,
    system: str | None = None,
    dry_run: bool = False,
    limit: int = 100_000,
    advance_watermark: bool = False,
    commit_every: int | None = None,
) -> ReconcileReport:
    """Reconcile a window of the archive — the backlog arriving after the live feed.

    Idempotent by construction: only primaries are examined and only primaries are
    matched, so the second run of a pass finds each pair already resolved (the loser is no
    longer a primary) and changes nothing.

    **Committed in chunks.**  The window can be 100,000 calls, and walking all of them in
    one transaction held every row the pass had rewritten, a ``KEY SHARE`` lock on every
    radio, site and talkgroup those rows point at, and — because :func:`reconcile_pair`
    reassigns conversations — the per-talkgroup ``pg_advisory_xact_lock`` that live ingest
    needs, from the first candidate to the last.  On 2026-09-03 an upload waited 122 s
    behind exactly that.  The pass therefore commits every
    ``settings.reconcile_batch_commit_every`` candidates: the locks are transaction-scoped,
    so each commit drops all of them and the next chunk re-acquires only what it touches,
    which bounds how long anything can wait on this pass to the length of one chunk.
    Idempotency is what makes that safe — a crash between two chunks leaves the pairs
    already resolved resolved, and the next pass picks up the rest.

    The candidates are read as a list of ids up front rather than streamed, because a
    ``COMMIT`` in the middle of iterating a result set is not something to rely on.

    The settings table is layered over ``settings`` first, as ingest does: the scheduled
    pass and ``keyup reconcile`` both hand in the process settings, and a swap re-queues
    the new primary's audio pass, which must see the Admin page's ``audio_process_enabled``
    rather than the environment's default of on (and ``reconcile_enabled`` likewise).
    """
    from app.runtime_settings import effective_settings  # noqa: PLC0415 - keep imports light

    settings = effective_settings(session, base=settings)
    cfg = ReconcileConfig.of(settings)
    report = ReconcileReport(dry_run=dry_run, since=since, system=system)
    if not cfg.enabled:
        log.info("reconcile: disabled (RECONCILE_ENABLED=false)")
        return report

    started = datetime.now(UTC)
    clock = time.monotonic()
    chunk_size = int(commit_every if commit_every is not None else
                     getattr(settings, "reconcile_batch_commit_every", 200) or 200)
    chunk_size = max(1, chunk_size)

    query = select(Call.id).where(Call.duplicate_of.is_(None))
    if since is not None:
        query = query.where(Call.ingested_at >= since)
    if system is not None:
        from app.models import System  # noqa: PLC0415 - only the batch pass needs it

        query = query.where(
            Call.system_id.in_(select(System.id).where(func.lower(System.name) == system.lower()))
        )
    query = query.order_by(Call.started_at, Call.id).limit(limit)
    candidates = list(session.scalars(query))

    resolved: set[int] = set()
    for start in range(0, len(candidates), chunk_size):
        chunk = candidates[start : start + chunk_size]
        for call_id in chunk:
            report.scanned += 1
            if call_id in resolved:
                continue
            call = session.get(Call, call_id)
            if call is None or call.duplicate_of is not None:
                continue
            other, match = find_match(session, call, cfg, exclude=resolved)
            if other is None:
                continue
            report.pairs += 1
            if dry_run:
                log.info(
                    "reconcile(dry-run): call %s matches %s (%s)", call.id, other.id, match.rule
                )
                resolved.add(call.id)
                continue
            # The same channel locks the inline path holds from match to write, so a live
            # upload on this channel cannot rule against either row while this pass does.
            # The match above ran without them, so it is only trusted if both
            # rows are still primaries once the locks are held.
            if not _lock_pair(session, call, other):
                # Busy: drop everything this chunk holds before waiting (see _lock_pair).
                session.commit()
                report.commits += 1
                _lock_pair_blocking(session, call, other)
            _reload(session, call, other)
            if call.duplicate_of is not None or other.duplicate_of is not None:
                report.pairs -= 1  # someone else ruled on it first; the next pass sees why
                resolved.add(call.id)
                continue
            # Which of the two is the "newcomer" is not arbitrary: a tie leaves the
            # *existing* primary in place, so the row that reached the archive later has to
            # be the one asking for the seat, exactly as it would have been at ingest.
            newcomer, existing = (
                (call, other) if _arrived_after(call, other) else (other, call)
            )
            outcome = reconcile_pair(
                session,
                newcomer,
                existing,
                settings,
                cfg=cfg,
                match=match,
                # Both sides were primaries until this ruling, so whichever loses has to be
                # taken off the live lists that are showing it.
                newcomer_was_primary=True,
            )
            report.swaps += int(outcome.swapped)
            report.merges += int(bool(outcome.merged_fields))
            resolved.add(outcome.duplicate_id or call.id)
        if not dry_run:
            session.commit()
            report.commits += 1

    if advance_watermark and not dry_run:
        _set_last_pass(session, started)
        session.commit()
        report.commits += 1
    report.elapsed_s = time.monotonic() - clock
    log.info(
        "reconcile pass: scanned=%d pairs=%d swaps=%d merges=%d commits=%d in %.1fs%s",
        report.scanned,
        report.pairs,
        report.swaps,
        report.merges,
        report.commits,
        report.elapsed_s,
        " (dry run)" if dry_run else "",
    )
    return report


# --------------------------------------------------------------------------------------
# by hand (POST /calls/{id}/make-primary)
# --------------------------------------------------------------------------------------


def make_primary(session: Session, call: Call, settings: Settings) -> ReconcileOutcome:
    """Force this copy to be the primary of its pair — the operator overruling the score.

    Raises :class:`ValueError` when the call is not a duplicate of anything, because there
    is then nothing to swap and silently doing nothing would read as success.
    """
    if call.duplicate_of is None:
        raise ValueError("call is already the primary copy")
    other = session.get(Call, call.duplicate_of)
    if other is None:  # pragma: no cover - the FK is ON DELETE SET NULL
        raise ValueError("the primary this call points at is gone")
    return reconcile_pair(
        session,
        call,
        other,
        settings,
        match=MatchResult(True, "manual"),
        forced_winner=call,
    )


def duplicates_of(session: Session, call: Call, settings: Settings) -> list[dict[str, Any]]:
    """The other copies of one transmission, scored, for the detail page's "Other copies"."""
    cfg = ReconcileConfig.of(settings)
    others = list(
        session.scalars(
            select(Call).where(Call.duplicate_of == call.id).order_by(Call.started_at, Call.id)
        )
    )
    return [
        {
            "id": other.id,
            "ingest_source": other.ingest_source,
            "duration_s": float(other.duration_s or 0),
            "audio_state": other.audio_state,
            "started_at": other.started_at,
            "time_source": other.time_source,
            "transcript_status": other.transcript_status,
            "score": round(score_call(session, other, call, cfg).total, 2),
        }
        for other in others
    ]
