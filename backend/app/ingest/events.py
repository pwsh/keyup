"""``POST /api/recorder/events`` → ``radio_events`` and the directory (docs/RADIO_EVENTS.md).

The call archive answers "what was said".  These rows answer "who was *there*": a radio
that turns on registers with the site and affiliates to a talkgroup on the control channel,
minutes before it keys up — and a radio that never keys up at all appears nowhere else.
docs/NON-VOICE-DATA.md §4 measured the traffic at roughly 600 events an hour on the author's
control channel, three orders of magnitude below the TSBK firehose, which is why this is a
table and the histogram is not.

Three things happen to every event:

1. it is **stored**, once — ``uq_radio_events_identity`` is
   ``(site, at, kind, opcode, radio_id, talkgroup)``, so the spooled batch a recorder
   retries after a network failure costs nothing the second time;
2. it is **folded into ``radios``** — ``upsert_radio`` with ``count=False`` (a registration
   is not a transmission and must never move ``call_count``), plus ``last_seen_at`` and,
   for a registration or de-registration, ``last_registered_at`` / ``last_deregistered_at``.
   ``wacn`` / ``home_system_id`` are *filled* from a fully qualified message where the row
   has the 0 sentinel, never overwritten;
3. an accepted **``group_affiliation``** is folded into ``radio_affiliations`` with
   ``source='affiliation'`` — which is the change of meaning the column exists to make
   explicit.  ``call_count`` is untouched: nobody spoke.

Nothing here rejects a batch.  A malformed event is counted and reported; the other 999
are stored.  A recorder that cannot tell whether its batch landed will resend it, and
resending must be safe, so the ingest is idempotent before it is strict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.ingest.service import (
    resolve_radio,
    resolve_system_id,
    upsert_site,
    upsert_talkgroup,
)
from app.models import RADIO_EVENT_KINDS, Radio, RadioAffiliation, RadioEvent, Site, System

log = logging.getLogger("app.ingest.events")

__all__ = ["EventsResult", "record_events"]

#: the vocabulary the wire may use, as a set for the membership test
KINDS = frozenset(RADIO_EVENT_KINDS)

#: what a recorder may call a kind that the archive spells differently.  Kept deliberately
#: small: the point of ``other`` is that an unrecognized kind is *stored*, not guessed at.
KIND_ALIASES = {
    "registration": "unit_registration",
    "deregistration": "unit_deregistration",
    "unit_reg": "unit_registration",
    "unit_dereg": "unit_deregistration",
    "registration_command": "unit_registration_command",
    "affiliation": "group_affiliation",
    "affiliation_query": "group_affiliation_query",
    "group_affiliation_response": "group_affiliation",
    "location_reg": "location_registration",
    "data_grant": "sndcp_grant",
    "data_page": "sndcp_page",
    "unit_to_unit": "private_call",
    "unit_call": "private_call",
    "private_call_request": "private_call",
}

#: results that count as "yes".  A P25 response says ACCEPT / DENY / REFUSED / FAIL; a
#: recorder may spell the first one a few ways, and only the affirmative one writes an
#: affiliation row — a *denied* affiliation is a fact about the site, not about presence.
ACCEPTING_RESULTS = frozenset({"accept", "accepted", "ok", "granted", "grant", "success"})

#: how many parse failures come back to the recorder; the rest are only counted
MAX_REPORTED_ERRORS = 5


@dataclass
class EventsResult:
    """What one batch did — also the endpoint's response body."""

    stored: int = 0
    duplicates: int = 0
    rejected: int = 0
    system_id: int | None = None
    site_id: int | None = None
    radios_touched: int = 0
    affiliations_touched: int = 0
    errors: list[str] = field(default_factory=list)

    def reject(self, why: str) -> None:
        self.rejected += 1
        if len(self.errors) < MAX_REPORTED_ERRORS:
            self.errors.append(why)


def _aware(value: datetime | str | None) -> datetime | None:
    """An instant from the document — ISO-8601 text or a datetime — as aware UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_kind(raw: Any) -> str:
    """The stored ``kind`` for whatever the recorder called it.

    Unknown is ``other``, never an error: docs/NON-VOICE-DATA.md §7 is explicit that the
    rare opcodes were never observed long enough to model, so the first one to appear must
    land in the table rather than be argued with.
    """
    name = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if name in KINDS:
        return name
    return KIND_ALIASES.get(name, "other")


def _touch_presence(
    session: Session, radio_pk: int, *, at: datetime, registered: bool, deregistered: bool
) -> None:
    """Move ``last_seen_at`` and, for a (de)registration, the presence stamps.

    ``greatest`` rather than an assignment because a recorder posting a spooled backlog
    delivers older events after newer ones, and presence must not walk backwards.
    """
    values: dict[str, Any] = {
        "last_seen_at": func.greatest(func.coalesce(Radio.last_seen_at, at), at),
        "first_seen_at": func.least(func.coalesce(Radio.first_seen_at, at), at),
    }
    if registered:
        values["last_registered_at"] = func.greatest(func.coalesce(Radio.last_registered_at, at), at)
    if deregistered:
        values["last_deregistered_at"] = func.greatest(func.coalesce(Radio.last_deregistered_at, at), at)
    session.execute(update(Radio).where(Radio.id == radio_pk).values(**values))


def _touch_affiliation(session: Session, radio_pk: int, talkgroup_pk: int, at: datetime) -> None:
    """Record "this radio is affiliated to that talkgroup", from the control channel.

    ``call_count`` is not incremented and ``last_heard_at`` is not moved — nobody spoke.
    ``source`` is raised to ``affiliation`` and never lowered again: it is the stronger
    fact, and a later call on the same pair does not make it less true.
    """
    stmt = pg_insert(RadioAffiliation).values(
        radio_id_fk=radio_pk,
        talkgroup_id_fk=talkgroup_pk,
        first_heard_at=None,
        last_heard_at=None,
        call_count=0,
        source="affiliation",
        last_affiliated_at=at,
    )
    session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_radio_affiliations_radio_talkgroup",
            set_={
                "source": text("'affiliation'"),
                "last_affiliated_at": func.greatest(
                    func.coalesce(RadioAffiliation.last_affiliated_at, text("excluded.last_affiliated_at")),
                    text("excluded.last_affiliated_at"),
                ),
            },
        )
    )


def record_events(  # noqa: PLR0912, PLR0915 - three phases, read top to bottom
    session: Session,
    document: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> EventsResult:
    """Store one batch of radio events and fold it into the directory.

    ``document`` is the shape :class:`app.schemas.RecorderEventsIn` validates, dumped with
    ``mode="json"``.  The caller commits.  It runs in three phases — parse, directory,
    everything hanging off the directory — because the whole batch is one transaction and
    the rows it takes have to be taken in the §5.2b order (:mod:`app.db`), which is only
    knowable once the batch has been read.  Like the observation path, everything the batch
    says about the *identity* of the system and the site is written only where the
    directory has nothing.
    """
    settings = settings or get_settings()
    result = EventsResult()

    system_doc = document.get("system") or {}
    site_doc = document.get("site") or {}
    label = str(system_doc.get("label") or "").strip()
    if not label:
        result.reject("the batch names no system")
        result.rejected = len(document.get("events") or ()) or 1
        return result

    system_id = resolve_system_id(session, label, protocol=None, wacn=_int(system_doc.get("wacn")))
    system = session.get(System, system_id)
    for name, value in (
        ("wacn", _int(system_doc.get("wacn"))),
        ("system_id", _int(system_doc.get("system_id"))),
        ("nac", _int(system_doc.get("nac"))),
    ):
        if value is not None and getattr(system, name, None) is None:
            setattr(system, name, value)

    site_label = (site_doc.get("label") or "").strip() or None
    site_id = upsert_site(session, system_id, site_label)
    if site_id is not None:
        site = session.get(Site, site_id)
        for name, value in (
            ("site_number", _int(site_doc.get("site"))),
            ("rfss", _int(site_doc.get("rfss"))),
            ("lra", _int(site_doc.get("lra"))),
            ("nac", _int(system_doc.get("nac"))),
        ):
            if value is not None and getattr(site, name, None) is None:
                setattr(site, name, value)

    result.system_id = system_id
    result.site_id = site_id

    # Stamped on every row of the batch: the batch names the instance once, exactly as it
    # names the system and the site once.  NULL from a recorder that does not send one.
    instance = (str(document.get("instance") or "").strip()) or None

    radios_seen: set[int] = set()
    affiliations: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------------------
    # Phase 1 — parse.  Nothing is written yet: the batch is read end to end so the
    # directory rows it needs are known before any of them is taken.  §5.2b (app/db.py)
    # is the reason: a batch used to upsert a radio, then a talkgroup, then the next
    # radio, in whatever order the control channel happened to print them, while
    # ``POST /api/call-upload`` took the talkgroup first — two orders, one deadlock a
    # shift, and two batches from two recorders could do it to each other too.
    # ------------------------------------------------------------------------------
    work: list[dict[str, Any]] = []
    for index, raw in enumerate(document.get("events") or ()):
        if not isinstance(raw, dict):
            result.reject(f"event {index}: not an object")
            continue
        at = _aware(raw.get("at"))
        if at is None:
            result.reject(f"event {index}: unusable timestamp {raw.get('at')!r}")
            continue

        kind = normalize_kind(raw.get("kind"))
        opcode = _int(raw.get("opcode")) or 0
        mfid = _int(raw.get("mfid")) or 0
        radio_id = _int(raw.get("radio_id"))
        radio_wacn = _int(raw.get("radio_wacn")) or 0
        radio_system = _int(raw.get("radio_system")) or 0
        tgid = _int(raw.get("talkgroup"))
        result_text = (str(raw.get("result")).strip().lower() if raw.get("result") else None) or None
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else None

        # A recorder that decoded a kind the archive has never seen still says what opcode
        # it was; keeping it in ``meta`` means the row can be re-classified later without
        # asking the recorder to hear the site again.
        if kind == "other" and raw.get("kind") and str(raw.get("kind")) not in KINDS:
            meta = {**(meta or {}), "kind_reported": str(raw.get("kind"))}

        work.append(
            {
                "raw": raw,
                "at": at,
                "kind": kind,
                "opcode": opcode,
                "mfid": mfid,
                "radio_id": radio_id,
                "radio_wacn": radio_wacn,
                "radio_system": radio_system,
                "tgid": tgid,
                "result_text": result_text,
                "meta": meta,
            }
        )

    # ------------------------------------------------------------------------------
    # Phase 2 — the directory, in the one order every writer uses: talkgroups first, in
    # ascending ``tgid``, then radios, in ascending ``(radio_id, wacn, home_system)``.
    #
    # An event's statements are unchanged and so are their arguments; only the order in
    # which they are issued moves.  Each *sighting* still gets its own upsert — the
    # first/last-seen fold is a least()/greatest(), so replaying them grouped by key is
    # the same arithmetic in a different order — which is why the counters below and the
    # rows themselves come out identical to the one-pass version.
    # ------------------------------------------------------------------------------
    talkgroup_work: dict[int, list[dict[str, Any]]] = {}
    radio_work: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for item in work:
        if item["tgid"] is not None:
            talkgroup_work.setdefault(item["tgid"], []).append(item)
        if item["radio_id"] is not None:
            key = (item["radio_id"], item["radio_wacn"], item["radio_system"])
            radio_work.setdefault(key, []).append(item)

    talkgroup_pks: dict[int, int] = {}
    for tgid in sorted(talkgroup_work):
        for item in talkgroup_work[tgid]:
            talkgroup_pks[tgid] = upsert_talkgroup(
                session, system_id, tgid, alias=None, seen_at=item["at"], count=False,
                settings=settings,
            ).id

    radio_pks: dict[tuple[int, int, int], int] = {}
    for key in sorted(radio_work):
        for item in radio_work[key]:
            # ``count=False``: presence is not a transmission.  The directory's
            # ``call_count`` is the number of times a unit was *heard*, and an affiliation
            # storm at shift change must not inflate it.  ``resolve_radio`` rather than
            # ``upsert_radio`` because half the messages about one unit carry its WACN and
            # half do not, and the two halves must land on one row.
            radio_pk = resolve_radio(
                session,
                system_id,
                radio_id=key[0],
                wacn=key[1],
                home_system=key[2],
                seen_at=item["at"],
                count=False,
            )
            radio_pks[key] = radio_pk
            _touch_presence(
                session,
                radio_pk,
                at=item["at"],
                registered=item["kind"] in ("unit_registration", "location_registration"),
                deregistered=item["kind"] == "unit_deregistration",
            )
            radios_seen.add(radio_pk)

    # ------------------------------------------------------------------------------
    # Phase 3 — everything that hangs off the directory: the affiliation rows (a third
    # table, and last in the §5.2b order, in ascending ``(talkgroup, radio)``), then the
    # event rows themselves in the order the batch sent them, so ``stored``/``duplicates``
    # and the reported errors read exactly as before.
    # ------------------------------------------------------------------------------
    pending: list[tuple[int, int, datetime]] = []
    for item in work:
        radio_pk = (
            radio_pks.get((item["radio_id"], item["radio_wacn"], item["radio_system"]))
            if item["radio_id"] is not None
            else None
        )
        talkgroup_pk = talkgroup_pks.get(item["tgid"]) if item["tgid"] is not None else None
        item["radio_pk"] = radio_pk
        item["talkgroup_pk"] = talkgroup_pk
        result_text = item["result_text"]
        accepted = result_text is None or result_text in ACCEPTING_RESULTS
        if (
            item["kind"] == "group_affiliation"
            and accepted
            and radio_pk is not None
            and talkgroup_pk is not None
        ):
            pending.append((talkgroup_pk, radio_pk, item["at"]))
            affiliations.add((radio_pk, talkgroup_pk))

    for talkgroup_pk, radio_pk, at in sorted(pending, key=lambda row: (row[0], row[1], row[2])):
        _touch_affiliation(session, radio_pk, talkgroup_pk, at)

    for item in work:
        raw = item["raw"]
        stored = session.execute(
            pg_insert(RadioEvent)
            .values(
                at=item["at"],
                system_id=system_id,
                site_id=site_id,
                kind=item["kind"],
                opcode=item["opcode"],
                mfid=item["mfid"],
                radio_pk=item["radio_pk"],
                radio_id=item["radio_id"],
                radio_wacn=item["radio_wacn"],
                radio_system=item["radio_system"],
                target_radio_id=_int(raw.get("target_radio_id")),
                talkgroup_tgid=item["tgid"],
                talkgroup_pk=item["talkgroup_pk"],
                result=item["result_text"],
                frequency_hz=_int(raw.get("frequency_hz")),
                instance=instance,
                meta=item["meta"],
            )
            # A bare ON CONFLICT DO NOTHING: ``uq_radio_events_identity`` is an
            # *expression* index (three of its six parts are COALESCE'd), which
            # ``index_elements`` cannot name.  It is the only unique constraint on the
            # table, so there is nothing else this could swallow.
            .on_conflict_do_nothing()
            .returning(RadioEvent.id)
        ).scalar_one_or_none()
        if stored is None:
            result.duplicates += 1
        else:
            result.stored += 1

    result.radios_touched = len(radios_seen)
    result.affiliations_touched = len(affiliations)
    return result
