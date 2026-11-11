"""Free the calls reconciliation hid behind a transmission they were never part of.

The third of the duplicate repairs (:mod:`app.repair_self_duplicates`,
:mod:`app.repair_hash_duplicates`), and the same shape of thing: an operator command, run by
hand, that re-examines rows the archive already swallowed and returns the wrongly hidden
ones to it.

What happened
-------------
Before the duplicate rules were tightened, three of
:func:`app.ingest.reconcile.matches`'s answers were too generous:

``fragment inside``
    "inside" was the skew-widened containment alone, so a 1.2 s key-up starting half a
    second *after* an 8 s call ended counted as inside it — the reply on a talkgroup folded
    into the call it answered.  Nothing asked whether the two were ever on the air
    together, nor whether the same radio keyed them.  On production **17,350 of 19,681**
    such rulings from one week did not overlap on the air at all.
the frequency key
    a shared traffic frequency made two calls one channel even when each named a
    talkgroup, and not the same one — a site re-grants a frequency within a second of
    releasing it.
``identical sha256``
    the exact rung did not require the same system, kind or channel: voiceless audio
    encodes to identical bytes on every talkgroup.

Each wrong ruling left the separate transmission hidden behind ``duplicate_of``, out of its
conversation, its pending transcription cancelled to ``skipped`` — and
:func:`~app.ingest.reconcile.merge_metadata` may have filled the primary's empty fields
from it.

The repair
----------
:func:`repair_fragment_duplicates` walks the duplicates whose stored ruling was one of
those three and asks the **fixed** rules again (:func:`_judge`).  Only a pair the new rules
refuse is touched.  For each one, under the channel locks and ``lock_calls``:

* ``duplicate_of`` and ``reconciled_at`` are cleared and ``reconcile_reason`` replaced with
  a ``repair:fragment-duplicate`` record that keeps the bogus ruling under ``previous``;
* the row is offered to reconciliation again against its *other* neighbours
  (:func:`~app.ingest.reconcile.find_match`, the old primary excluded) — a row that really
  is a copy of some other call is ruled against that call with the fixed rules;
* a row that ends up a primary goes back into its conversation, its wrongly-skipped
  transcription is queued at ``--requeue-priority`` (the backlog band by default, so the
  live feed is never pushed back), and it gets an audio pass when it never had one and
  audio processing is on — the Admin page's switch as well as ``AUDIO_PROCESS_ENABLED``;
* the export ledger is only ever *released*, never filled: a row inside the live export
  window (``EXPORT_MAX_AGE_MINUTES``) loses the ``already exported as call …`` skip the
  ruling gave it — and the primary loses the one naming it — so discovery re-judges both
  with every gate it normally applies.  A row outside the window is left exactly as it is,
  and with no age bound configured it is fenced with a ``skipped`` row, because the
  services must never receive last week's traffic because of a repair;
* a ``call.updated`` is sent for it when it started in the last
  :data:`NOTIFY_RECENT_HOURS` (delivered at the batch's commit, so in batches of at most
  ``batch``), and one activity event summarizes the run.

What :func:`~app.ingest.reconcile.merge_metadata` filled on a primary is **counted, not
undone**: whether the primary's own recorder would have reported the same value is not
something the archive can answer.  ``merge_side_effects`` in the report says how many
primaries had fields filled from a row now separated from them, per field, and how many of
those still carry exactly the separated row's value.

Chains
------
Old swaps left some duplicates pointing at a row that is itself a duplicate
(``dup → dup → … → primary``).  Such a row is judged against the *final* primary at the end
of its chain (:func:`_follow`, at most :data:`MAX_CHAIN_HOPS` hops; a cycle or a longer
chain is logged and left alone).  Refused, it is freed like any other, under row locks on
every row of the chain; accepted, its ``duplicate_of`` is re-pointed straight at the final
primary so the chain collapses.  The dry run judges chains the same way, so its refusals
are what an apply would repair.

Committed every ``batch`` pairs, resumable and idempotent: a repaired row no longer carries
one of the three rulings, and a row re-matched elsewhere carries a ruling the fixed rules
made, which they accept on the next run.  A row that changed between its judgement and its
locks — most often because another row of the same pass moved its primary — is tried once
more at the end of the pass before it is counted as ``skipped_changed_underneath``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import lock_calls, notify
from app.ingest.reconcile import (
    EXACT_HASH_MAX_SKEW_S,
    Interval,
    MatchResult,
    ReconcileConfig,
    _arrived_after,
    _primary_of,
    _reconcile_lock_keys,
    _reload,
    _requeue_audio,
    find_match,
    matches,
    reconcile_pair,
)
from app.models import CALL_EXPORT_SERVICES, Call, CallExport, Talkgroup
from app.repair_self_duplicates import FRESH_TRANSCRIPT_STATUS, _has_transcript
from app.runtime_settings import effective_settings

log = logging.getLogger("app.repair_fragment_duplicates")

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_REQUEUE_PRIORITY",
    "REPAIRED_RULES",
    "FragmentRepairReport",
    "repair_fragment_duplicates",
]

#: Pairs per committed transaction — the size the sibling repairs use, for their reason.
DEFAULT_BATCH_SIZE = 500

#: The band a freed row's transcription is queued in: the talkgroup's own priority pushed
#: into the backlog band, so tens of thousands of re-transcriptions queue behind live traffic.
DEFAULT_REQUEUE_PRIORITY = "backlog"

#: The stored ``reconcile_reason.match.rule`` values the fixed rules have to re-judge.
REPAIRED_RULES = ("fragment inside", "identical sha256")

#: …and every ruling made on a frequency key, whatever rung carried it: the frequency is
#: the channel only when one side has no talkgroup now.
FREQUENCY_KEY_PREFIX = "frequency:"

#: The ``reconcile_reason.rule`` a repaired row carries.
REPAIR_RULE = "repair:fragment-duplicate"

#: The ledger reason a freed row outside the export window is fenced with, when the
#: operator configured no age bound and discovery would otherwise publish it.
FENCED_REASON = "restored by repair outside the export window"

#: The prefix of the skip the ruling left on the ledger (``DUPLICATE_UPLOAD_REASON``).
_DUPLICATE_SKIP_PREFIX = "already exported as call "

#: A freed row that started this recently gets a ``call.updated`` frame (see _repair_one).
NOTIFY_RECENT_HOURS = 24

#: How many ``duplicate_of`` hops are followed from a candidate to its final primary; a
#: longer chain is logged and skipped rather than trusted.
MAX_CHAIN_HOPS = 10

Progress = Callable[[dict[str, Any]], None]


@dataclass
class FragmentRepairReport:
    """One repair pass, in the shape the CLI prints."""

    examined: int = 0
    refused_by_new_rules: int = 0
    repaired: int = 0
    rematched_elsewhere: int = 0
    transcription_queued: int = 0
    transcription_queued_seconds: float = 0.0
    audio_queued: int = 0
    conversations_reattached: int = 0
    kept_by_original_partner: int = 0
    skipped_changed_underneath: int = 0
    #: candidates whose primary was itself a duplicate, judged against the chain's end
    chains_followed: int = 0
    #: …of those, the ones re-pointed straight at the final primary (would be, in a dry run)
    chains_collapsed: int = 0
    #: …and the ones left alone: a cycle, or more than MAX_CHAIN_HOPS hops
    chains_unresolved: int = 0
    exports_released: int = 0
    exports_fenced: int = 0
    notified: int = 0
    commits: int = 0
    dry_run: bool = False
    since: datetime | None = None
    until: datetime | None = None
    requeue_priority: str = DEFAULT_REQUEUE_PRIORITY
    #: per stored ruling: ``{"examined": n, "refused": m}``
    by_rule: dict[str, Counter] = field(default_factory=dict)
    merged_primaries: set[int] = field(default_factory=set)
    merged_fields: Counter = field(default_factory=Counter)
    merged_still_equal: Counter = field(default_factory=Counter)

    def count(self, label: str, key: str) -> None:
        self.by_rule.setdefault(label, Counter())[key] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "refused_by_new_rules": self.refused_by_new_rules,
            "repaired": self.repaired,
            "rematched_elsewhere": self.rematched_elsewhere,
            "transcription_queued": self.transcription_queued,
            "transcription_queued_seconds": round(self.transcription_queued_seconds, 1),
            "audio_queued": self.audio_queued,
            "conversations_reattached": self.conversations_reattached,
            "kept_by_original_partner": self.kept_by_original_partner,
            "skipped_changed_underneath": self.skipped_changed_underneath,
            "chains_followed": self.chains_followed,
            "chains_collapsed": self.chains_collapsed,
            "chains_unresolved": self.chains_unresolved,
            "exports_released": self.exports_released,
            "exports_fenced": self.exports_fenced,
            "notified": self.notified,
            "by_rule": {
                label: {"examined": c["examined"], "refused": c["refused"]}
                for label, c in sorted(self.by_rule.items())
            },
            "merge_side_effects": {
                "primaries_with_filled_fields": len(self.merged_primaries),
                "by_field": dict(sorted(self.merged_fields.items())),
                "still_equal_to_separated_row": dict(sorted(self.merged_still_equal.items())),
            },
            "commits": self.commits,
            "dry_run": self.dry_run,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "requeue_priority": self.requeue_priority,
        }


# --------------------------------------------------------------------------------------
# selection and judgement
# --------------------------------------------------------------------------------------


def _candidates(cursor: int, batch: int, since: datetime | None, until: datetime | None):  # noqa: ANN202 - a Select of Call
    """The duplicates whose ruling the fixed rules have to answer again, by id.

    Self-duplicates (``duplicate_of = id``) belong to :mod:`app.repair_self_duplicates` and
    are left out.  ``reconcile_reason`` is JSONB, read with the same ``->'match'->>…`` path
    the far-hash repair uses.
    """
    match = Call.reconcile_reason["match"]
    query = select(Call).where(
        Call.id > cursor,
        Call.duplicate_of.isnot(None),
        Call.duplicate_of != Call.id,
        or_(
            match["rule"].astext.in_(REPAIRED_RULES),
            match["key"].astext.like(f"{FREQUENCY_KEY_PREFIX}%"),
        ),
    )
    if since is not None:
        query = query.where(Call.started_at >= since)
    if until is not None:
        query = query.where(Call.started_at < until)
    return query.order_by(Call.id).limit(batch)


def _is_candidate(call: Call) -> bool:
    """:func:`_candidates`' filter on a row already in hand (the end-of-pass retry)."""
    if call.duplicate_of is None or call.duplicate_of == call.id:
        return False
    match = (call.reconcile_reason or {}).get("match") or {}
    return str(match.get("rule")) in REPAIRED_RULES or str(match.get("key") or "").startswith(
        FREQUENCY_KEY_PREFIX
    )


def _label(call: Call) -> str:
    match = (call.reconcile_reason or {}).get("match") or {}
    rule = str(match.get("rule") or "?")
    if str(match.get("key") or "").startswith(FREQUENCY_KEY_PREFIX):
        return f"{rule} [frequency key]"
    return rule


def _merged_into(dup: Call, primary: Call) -> list[str]:
    """The fields the ruling that hid ``dup`` filled on ``primary`` (empty when it filled
    them on some other row, or nothing)."""
    reason = dup.reconcile_reason or {}
    if reason.get("winner_id") != primary.id:
        return []
    return [str(f) for f in (reason.get("merged_fields") or []) if f != "raw_meta.merged_from"]


def _before_merge(primary: Call, dup: Call) -> Interval:
    """``primary`` as matching saw it *before* the ruling filled it from ``dup``.

    A merged primary carries the duplicate's radio, talkgroups or frequency, and asking the
    fixed rules about the pair with those in place would have the duplicate agree with its
    own copied values — a cross-talkgroup hash hit whose primary had no talkgroup of its
    own "shares" the duplicate's afterwards.  A field is taken back out only when the ruling
    says it filled it *and* the primary still holds exactly the duplicate's value.
    """
    interval = Interval.of(primary)
    merged = set(_merged_into(dup, primary))
    if not merged:
        return interval
    changes: dict[str, Any] = {}
    if (merged & {"radio_id", "call_sources"}) and primary.radio_id == dup.radio_id:
        changes["source_radio"] = None
        changes["unit_pair"] = (None, interval.unit_pair[1])
    if "target_radio_id" in merged and primary.target_radio_id == dup.target_radio_id:
        pair = changes.get("unit_pair", interval.unit_pair)
        changes["unit_pair"] = (pair[0], None)
    if (
        "talkgroup_ids" in merged
        and "patch_id" not in merged
        and sorted(primary.talkgroup_ids or ()) == sorted(dup.talkgroup_ids or ())
    ):
        changes["talkgroup_ids"] = ()
    if "frequency_hz" in merged and primary.frequency_hz == dup.frequency_hz:
        changes["frequency_hz"] = None
    return replace(interval, **changes) if changes else interval


def _judge(dup: Call, other: Call, cfg: ReconcileConfig) -> MatchResult:
    """Would the fixed reconciliation rule these two one transmission?

    Either rung: :func:`~app.ingest.reconcile.matches` on the intervals, or the exact rung
    as :func:`~app.ingest.reconcile._find` now applies it — an identical ``sha256`` counts
    only in the same system, of the same kind, on the same channel and within
    :data:`~app.ingest.reconcile.EXACT_HASH_MAX_SKEW_S` of each other.
    """
    result = matches(Interval.of(dup), _before_merge(other, dup), cfg)
    if result.matched:
        return result
    if (
        dup.sha256
        and dup.sha256 == other.sha256
        and dup.system_id == other.system_id
        and (dup.kind or "group") == (other.kind or "group")
        and abs((dup.started_at - other.started_at).total_seconds()) <= EXACT_HASH_MAX_SKEW_S
        and result.key
    ):
        return MatchResult(True, "identical sha256", key=f"sha256:{dup.sha256[:12]}")
    return result


@dataclass
class _Chain:
    """``dup`` → ``links[0]`` → … → ``primary``: the rows between a candidate and the row that
    is actually primary.  ``links`` is empty for the ordinary one-hop case."""

    links: list[Call]
    primary: Call

    @property
    def rows(self) -> list[Call]:
        return [*self.links, self.primary]

    @property
    def ids(self) -> set[int]:
        return {row.id for row in self.rows}


def _follow(session: Session, dup: Call) -> _Chain | str:
    """Follow ``dup.duplicate_of`` to the row that is primary.

    Old swaps left some duplicates pointing at another duplicate, and a ruling can only be
    re-judged — or a row freed — against a primary.  Returns the chain, or why it could not
    be followed: ``"cycle"`` (a row reached twice, a self-duplicate on the way included) or
    ``"too deep"`` (more than :data:`MAX_CHAIN_HOPS` hops).
    """
    links: list[Call] = []
    seen = {dup.id}
    current = dup
    for _ in range(MAX_CHAIN_HOPS):
        target = current.duplicate_of
        if target in seen:
            return "cycle"
        parent = session.get(Call, target)
        if parent is None:  # pragma: no cover - the FK is ON DELETE SET NULL
            return "dangling"
        if parent.duplicate_of is None:
            return _Chain(links, parent)
        seen.add(parent.id)
        links.append(parent)
        current = parent
    return "too deep"


def _original_partner(session: Session, dup: Call, chain: _Chain) -> Call | None:
    """The row the stored ruling actually compared ``dup`` with, when that is not the
    primary it resolves to now — a later swap re-points every duplicate at the new winner
    (:func:`~app.ingest.reconcile._swap`), so the ruling may have been about another row of
    the same group: a sibling duplicate of the primary, or of a row on ``dup``'s chain, or
    one of the chain's own links."""
    reason = dup.reconcile_reason or {}
    ids = {int(k) for k in (reason.get("scores") or {}) if str(k).isdigit()}
    if isinstance(reason.get("winner_id"), int):
        ids.add(int(reason["winner_id"]))
    ids.discard(dup.id)
    ids.discard(chain.primary.id)
    group = chain.ids
    for partner_id in sorted(ids):
        partner = session.get(Call, partner_id)
        if partner is not None and partner.duplicate_of in group:
            return partner
    return None


#: What the fixed rules say about a candidate (see :func:`_verdict`).
MATCHES, KEPT_BY_PARTNER, REFUSED = "matches", "kept by original partner", "refused"


def _verdict(session: Session, dup: Call, chain: _Chain, cfg: ReconcileConfig) -> str:
    """The fixed rules on ``dup`` against the final primary of its chain — or, failing that,
    against the row its stored ruling was actually about."""
    if _judge(dup, chain.primary, cfg).matched:
        return MATCHES
    partner = _original_partner(session, dup, chain)
    if partner is not None and _judge(dup, partner, cfg).matched:
        return KEPT_BY_PARTNER
    return REFUSED


def _note_merge(report: FragmentRepairReport, dup: Call, primary: Call) -> None:
    fields = _merged_into(dup, primary)
    if not fields:
        return
    report.merged_primaries.add(primary.id)
    for name in fields:
        report.merged_fields[name] += 1
        if not name.startswith("raw_meta.") and name != "call_sources":
            mine, theirs = getattr(primary, name, None), getattr(dup, name, None)
            if theirs is not None and mine == theirs:
                report.merged_still_equal[name] += 1


# --------------------------------------------------------------------------------------
# restoring one row
# --------------------------------------------------------------------------------------


def _would_queue(session: Session, call: Call, settings: Settings) -> bool:
    """The dry-run answer to "would the requeue put this row in the transcription queue"."""
    if call.transcript_status != "skipped" or (call.audio_state or "present") != "present":
        return False
    if _has_transcript(session, call.id):
        return False
    talkgroup = session.get(Talkgroup, call.talkgroup_id) if call.talkgroup_id else None
    if talkgroup is not None and not talkgroup.transcribe:
        return False
    minimum = float(settings.transcribe_min_seconds)
    if talkgroup is not None and talkgroup.transcribe_min_seconds is not None:
        minimum = float(talkgroup.transcribe_min_seconds)
    return float(call.duration_s) >= minimum


def _requeue_transcription(
    session: Session, call: Call, settings: Settings, requeue_priority: str
) -> int | None:
    """Queue a wrongly-skipped transcription at ``requeue_priority``, resolved against the
    talkgroup exactly as the push path resolves an upload's priority.  The talkgroup's own
    policy still decides *whether* (``enqueue_transcription``)."""
    if call.transcript_status != "skipped":
        return None
    if (call.audio_state or "present") != "present" or _has_transcript(session, call.id):
        return None
    from app.ingest.flow import parse_priority  # noqa: PLC0415 - cycle
    from app.ingest.service import enqueue_transcription  # noqa: PLC0415 - cycle

    talkgroup = session.get(Talkgroup, call.talkgroup_id) if call.talkgroup_id else None
    priority = parse_priority(requeue_priority).resolve(
        talkgroup.priority if talkgroup is not None else None
    )
    call.transcript_status = FRESH_TRANSCRIPT_STATUS
    return enqueue_transcription(session, call, talkgroup, settings, priority=priority)


def _export_window_start(settings: Settings, now: datetime) -> datetime | None:
    minutes = int(getattr(settings, "export_max_age_minutes", 0) or 0)
    return now - timedelta(minutes=minutes) if minutes > 0 else None


def _restore_exports(
    session: Session, dup: Call, primaries: list[Call], settings: Settings, now: datetime
) -> tuple[int, int]:
    """Undo what the ruling did to the ledger — for the live window only.

    Returns ``(released, fenced)``.  Inside the window the duplicate skip the ruling left
    (``already exported as call …`` on the freed row, and the one naming the freed row on
    the primary — on any row of its chain, when it had one) is deleted, so discovery
    re-judges the call with every gate it applies to any other.  Outside it nothing is
    released; with ``EXPORT_MAX_AGE_MINUTES = 0`` discovery has no age bound of its own, so
    a freed row it could still reach is given a ``skipped`` row instead — a repair must
    never publish old traffic.
    """
    from app.exporters.gate import watermark  # noqa: PLC0415 - cycle

    window = _export_window_start(settings, now)
    released = fenced = 0
    if window is not None and dup.started_at >= window:
        released += int(
            session.execute(
                delete(CallExport).where(
                    CallExport.call_id == dup.id,
                    CallExport.status == "skipped",
                    CallExport.skip_reason.like(f"{_DUPLICATE_SKIP_PREFIX}%"),
                )
            ).rowcount
            or 0
        )
    for primary in primaries:
        if window is None or primary.started_at < window:
            continue
        released += int(
            session.execute(
                delete(CallExport).where(
                    CallExport.call_id == primary.id,
                    CallExport.status == "skipped",
                    CallExport.skip_reason == f"{_DUPLICATE_SKIP_PREFIX}{dup.id}",
                )
            ).rowcount
            or 0
        )
    if window is None:
        for service in CALL_EXPORT_SERVICES:
            mark = watermark(session, service)
            if mark is None or dup.started_at <= mark:
                continue
            inserted = session.execute(
                insert(CallExport)
                .values(
                    call_id=dup.id,
                    service=service,
                    status="skipped",
                    skip_reason=FENCED_REASON,
                    next_attempt_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_call_exports_call_service")
                .returning(CallExport.id)
            ).first()
            fenced += int(inserted is not None)
    return released, fenced


def _lock_keys(dup: Call, others: list[Call]) -> list[tuple[int, int]]:
    """The channel locks for ``dup`` and every row it is ruled on against, in the order
    :func:`app.ingest.reconcile.lock_reconcile` takes them — for a single other row exactly
    :func:`app.ingest.reconcile._pair_lock_keys`."""
    rows = [dup, *others]
    return _reconcile_lock_keys(
        talkgroup_ids=sorted({tg for row in rows for tg in (row.talkgroup_ids or ())}),
        tgid=None,
        frequencies=tuple(row.frequency_hz for row in rows),
        unit_pair=(
            (dup.radio_id, dup.target_radio_id) if (dup.kind or "group") == "unit" else (None, None)
        ),
    )


def _take_locks(
    session: Session, dup: Call, others: list[Call], report: FragmentRepairReport
) -> None:
    """The channel locks, never waited for while holding others (see
    :func:`app.ingest.reconcile._lock_pair`): busy means commit first, then wait."""
    keys = _lock_keys(dup, others)
    for cls, key in keys:
        got = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:cls, :key)"), {"cls": cls, "key": key}
        ).scalar()
        if not got:
            break
    else:
        return
    session.commit()
    report.commits += 1
    for cls, key in keys:
        session.execute(text("SELECT pg_advisory_xact_lock(:cls, :key)"), {"cls": cls, "key": key})


def _lock_chain(
    session: Session, dup: Call, chain: _Chain, report: FragmentRepairReport
) -> bool:
    """Lock ``dup`` and every row of its chain — channel locks, then the row locks in
    ``lock_calls`` order — re-read them, and say whether the chain is still the one the
    verdict was reached on."""
    _take_locks(session, dup, chain.rows, report)
    lock_calls(session, [dup.id, *chain.ids])
    _reload(session, dup, *chain.rows)
    pointers = zip([dup, *chain.links], chain.rows, strict=True)
    return chain.primary.duplicate_of is None and all(
        row.duplicate_of == target.id for row, target in pointers
    )


def _collapse(session: Session, dup: Call, chain: _Chain, now: datetime) -> None:
    """Re-point ``dup`` straight at the final primary its chain resolves to, which the fixed
    rules accept it as a copy of.  The ruling is kept; the chain it hung from is recorded."""
    via = [row.id for row in chain.links]
    reason = dict(dup.reconcile_reason or {})
    reason["chain_collapsed"] = {"via": via, "primary": chain.primary.id, "at": now.isoformat()}
    dup.reconcile_reason = reason
    dup.duplicate_of = chain.primary.id
    session.flush()
    log.info(
        "repair-fragments: call %s collapsed: duplicate_of %s -> %s (chain via %s)",
        dup.id,
        via[0],
        chain.primary.id,
        via,
    )


def _repair_one(
    session: Session,
    dup: Call,
    chain: _Chain,
    settings: Settings,
    cfg: ReconcileConfig,
    report: FragmentRepairReport,
    *,
    requeue_priority: str,
    now: datetime,
) -> None:
    """Free ``dup`` from its chain and let the fixed rules place it.  The caller holds the
    locks (:func:`_lock_chain`)."""
    from app.ingest.service import assign_conversation  # noqa: PLC0415 - cycle

    primary = chain.primary

    previous = dup.reconcile_reason
    dup.duplicate_of = None
    dup.reconciled_at = None
    dup.reconcile_reason = {
        "rule": REPAIR_RULE,
        "note": (
            f"ruled a duplicate of call {primary.id} on a rule the tightened check refuses: the two "
            "were not on the air together, were keyed by different radios, or share only "
            "a traffic frequency or voiceless bytes across two talkgroups"
        ),
        "primary_was": primary.id,
        **({"chain_via": [row.id for row in chain.links]} if chain.links else {}),
        "at": now.isoformat(),
        "previous": previous,
    }
    report.repaired += 1
    released, fenced = _restore_exports(session, dup, chain.rows, settings, now)
    report.exports_released += released
    report.exports_fenced += fenced
    session.flush()

    # Is it a copy of something else?  The old primary (and its chain) is excluded: the fixed
    # rules just refused it, and its merged fields would otherwise let it agree with its own
    # copy.
    group = chain.ids
    other, match = find_match(session, dup, cfg, exclude=group)
    if other is not None:
        other = _primary_of(session, other)
    if other is not None and other.id not in group | {dup.id}:
        _take_locks(session, dup, [other], report)
        _reload(session, dup, other)
        if dup.duplicate_of is None and other.duplicate_of is None:
            newcomer, existing = (dup, other) if _arrived_after(dup, other) else (other, dup)
            outcome = reconcile_pair(
                session,
                newcomer,
                existing,
                settings,
                cfg=cfg,
                match=match,
                newcomer_was_primary=True,
                requeue_class=requeue_priority,
            )
            if outcome.matched and outcome.rule not in ("stale",):
                report.rematched_elsewhere += 1
                if outcome.job_id is not None:
                    report.transcription_queued += 1
                    winner = session.get(Call, outcome.primary_id)
                    report.transcription_queued_seconds += float(winner.duration_s or 0)
                report.audio_queued += int(outcome.audio_job_id is not None)

    if dup.duplicate_of is None:
        if (
            dup.conversation_id is None
            and dup.talkgroup_id is not None
            and assign_conversation(session, dup, settings.conversation_gap_seconds) is not None
        ):
            report.conversations_reattached += 1
        if _requeue_transcription(session, dup, settings, requeue_priority) is not None:
            report.transcription_queued += 1
            report.transcription_queued_seconds += float(dup.duration_s or 0)
        if bool(getattr(settings, "audio_process_enabled", True)):
            report.audio_queued += int(
                _requeue_audio(session, dup, settings, requeue_class=requeue_priority) is not None
            )
    # Only a row an open page could be showing: the live lists and the default archive view
    # are the last day.  A backfill of months would otherwise send a frame per row, each
    # one a query per connected filter; the run's activity event covers the rest.
    if dup.started_at >= now - timedelta(hours=NOTIFY_RECENT_HOURS):
        notify(session, "call.updated", {"id": dup.id})
        report.notified += 1


# --------------------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------------------


def _process(
    session: Session,
    dup: Call,
    settings: Settings,
    cfg: ReconcileConfig,
    report: FragmentRepairReport,
    *,
    dry_run: bool,
    requeue_priority: str,
    now: datetime,
    last_try: bool,
) -> bool:
    """Judge one candidate and act on the verdict.  ``False`` — only when not ``last_try``
    — means its chain changed between the verdict and the locks and nothing was counted or
    written: the caller tries it again at the end of the pass."""
    chain = _follow(session, dup)
    if isinstance(chain, str):
        report.chains_unresolved += 1
        log.warning(
            "repair-fragments: call %s: duplicate_of chain from %s not followed (%s, limit %s "
            "hops); skipped",
            dup.id,
            dup.duplicate_of,
            chain,
            MAX_CHAIN_HOPS,
        )
        return True
    verdict = _verdict(session, dup, chain, cfg)
    if verdict == KEPT_BY_PARTNER or (verdict == MATCHES and not chain.links):
        report.chains_followed += int(bool(chain.links))
        report.kept_by_original_partner += int(verdict == KEPT_BY_PARTNER)
        return True
    if not dry_run and not _lock_chain(session, dup, chain, report):
        if not last_try:
            return False
        report.skipped_changed_underneath += 1
        locked = False
    else:
        locked = not dry_run
    report.chains_followed += int(bool(chain.links))

    if verdict == MATCHES:  # a chain whose end the fixed rules accept: collapse it
        if locked:
            _collapse(session, dup, chain, now)
        if locked or dry_run:
            report.chains_collapsed += 1
        return True

    label = _label(dup)
    report.refused_by_new_rules += 1
    report.count(label, "refused")
    for row in chain.rows:
        _note_merge(report, dup, row)
    if dry_run:
        group = chain.ids
        other, _ = find_match(session, dup, cfg, exclude=group)
        if other is not None and _primary_of(session, other).id not in group | {dup.id}:
            report.rematched_elsewhere += 1
        elif _would_queue(session, dup, settings):
            report.transcription_queued += 1
            report.transcription_queued_seconds += float(dup.duration_s or 0)
    elif locked:
        _repair_one(
            session,
            dup,
            chain,
            settings,
            cfg,
            report,
            requeue_priority=requeue_priority,
            now=now,
        )
    return True


def repair_fragment_duplicates(
    session: Session,
    settings: Settings,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    dry_run: bool = False,
    batch: int = DEFAULT_BATCH_SIZE,
    requeue_priority: str = DEFAULT_REQUEUE_PRIORITY,
    progress: Progress | None = None,
) -> FragmentRepairReport:
    """Re-judge every ``fragment inside`` / ``identical sha256`` / frequency-key duplicate
    with the fixed rules and free the ones they refuse.

    ``since``/``until`` bound the *duplicate's* ``started_at`` (``until`` exclusive).
    ``dry_run`` reads and counts and writes nothing: the refusals, the rows a re-match
    would link elsewhere (the same :func:`~app.ingest.reconcile.find_match` a real run
    asks, over the archive as it stands), and the transcriptions the requeue would queue.
    """
    from app.ingest.flow import parse_priority  # noqa: PLC0415 - cycle

    parse_priority(requeue_priority)  # a typo fails before the first row, not mid-run
    # The Admin page's switches, not only the environment: the CLI hands in the process
    # settings, where ``AUDIO_PROCESS_ENABLED`` is on by default, while the operator had the
    # stage switched off in the settings table.  The 2026-09-23 history repair queued
    # 450,962 audio jobs that way, every one cancelled by the worker a moment later.  The
    # same layering ingest and the upload route do (:func:`app.ingest.service.ingest_file`).
    settings = effective_settings(session, base=settings)
    cfg = ReconcileConfig.of(settings)
    report = FragmentRepairReport(
        dry_run=dry_run, since=since, until=until, requeue_priority=requeue_priority
    )
    batch = max(1, int(batch))
    cursor = 0
    retry: list[int] = []

    def judge(dup: Call, now: datetime, *, last_try: bool) -> bool:
        return _process(
            session,
            dup,
            settings,
            cfg,
            report,
            dry_run=dry_run,
            requeue_priority=requeue_priority,
            now=now,
            last_try=last_try,
        )

    def finish_batch() -> None:
        if dry_run:
            session.rollback()
        else:
            session.commit()
            report.commits += 1
        if progress is not None:
            progress({"cursor": cursor, **_progress_counts(report)})

    while True:
        rows = list(session.scalars(_candidates(cursor, batch, since, until)))
        if not rows:
            break
        # A repaired row leaves the set, but a kept one does not: the cursor is what walks it.
        cursor = rows[-1].id
        now = datetime.now(UTC)
        for dup in rows:
            if dup.duplicate_of is None:  # freed by an earlier pair of this batch
                continue
            report.examined += 1
            report.count(_label(dup), "examined")
            if not judge(dup, now, last_try=False):
                retry.append(dup.id)
        finish_batch()

    # Rows whose chain moved between verdict and locks — usually because another row of
    # this pass was freed, re-matched or swapped — get one more try against the archive as
    # the pass left it.  Only an apply defers (a dry run takes no locks).
    if retry:
        session.expire_all()  # every chain row is read afresh, not as this pass cached it
    for start in range(0, len(retry), batch):
        now = datetime.now(UTC)
        for call_id in retry[start : start + batch]:
            dup = session.get(Call, call_id)
            if dup is None:  # pragma: no cover - deleted underneath
                continue
            _reload(session, dup)
            if _is_candidate(dup):
                judge(dup, now, last_try=True)
        finish_batch()

    if not dry_run and (report.repaired or report.chains_collapsed):
        from app.activity import record  # noqa: PLC0415 - cycle

        record(
            session,
            source="system",
            level="info",
            kind="reconcile.repair_fragments",
            message=(
                f"reconcile repair: {report.repaired} calls wrongly hidden as duplicates "
                f"restored ({report.rematched_elsewhere} re-matched to another call, "
                f"{report.transcription_queued} transcriptions queued); "
                f"{report.chains_collapsed} duplicate chains collapsed"
            ),
            meta={k: v for k, v in report.as_dict().items() if k != "merge_side_effects"},
        )
        session.commit()
        report.commits += 1

    log.info(
        "repair-fragments%s: examined %s, refused %s, repaired %s, re-matched %s, queued %s, "
        "chains followed %s, collapsed %s, unresolved %s, skipped %s",
        " (dry-run)" if dry_run else "",
        report.examined,
        report.refused_by_new_rules,
        report.repaired,
        report.rematched_elsewhere,
        report.transcription_queued,
        report.chains_followed,
        report.chains_collapsed,
        report.chains_unresolved,
        report.skipped_changed_underneath,
    )
    return report


def _progress_counts(report: FragmentRepairReport) -> dict[str, Any]:
    return {
        "examined": report.examined,
        "refused": report.refused_by_new_rules,
        "repaired": report.repaired,
        "rematched": report.rematched_elsewhere,
        "queued": report.transcription_queued,
        "collapsed": report.chains_collapsed,
    }
