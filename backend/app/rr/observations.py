"""``POST /api/recorder/observations`` → the three audit tables (docs/CHANNEL_AUDIT.md).

One document is one *window*: what the recorder saw of one site over ``window_s`` seconds.
It is stored verbatim in ``recorder_observations`` and then folded into ``site_frequencies``
and ``adjacent_sites``, where counts accumulate across windows and ``first_seen_at`` only
ever moves backwards.  Nothing here decides whether a frequency is interesting — that is
:mod:`app.rr.audit`'s job, and it works off these rows.

Role mapping, which is the whole normalization:

=========================  ====================================  ==================
document key               role                                  source
=========================  ====================================  ==================
``control``                ``primary_control``                   ``observed``
``secondary_control[]``    ``alternate_control``                 ``observed``
``traffic[]``              ``voice``                             ``observed``
``configured_control[]``   ``primary_control``                   ``configured``
=========================  ====================================  ==================

A document with no ``site.label`` is stored and not normalized: every audit row hangs off
a site, and inventing one from the RFSS/site numbers would put a site in the directory
that no call ever names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.ingest.service import resolve_system_id, upsert_site
from app.models import AdjacentSite, RecorderObservation, Site, SiteFrequency, System
from app.rr.message_kinds import MessageKindFinding, record_message_kinds

log = logging.getLogger("app.rr.observations")

__all__ = ["ObservationResult", "last_observed_at", "record_observation"]

#: strongest first — when one window names a frequency twice (a control channel that also
#: granted, say) the strongest role is the one that is stored
ROLE_RANK = ("primary_control", "alternate_control", "voice", "unknown")


@dataclass
class ObservationResult:
    """What one document did, which is also the endpoint's response body."""

    observation_id: int
    system_id: int | None = None
    site_id: int | None = None
    frequencies_upserted: int = 0
    frequencies_new: int = 0
    adjacent_upserted: int = 0
    adjacent_new: int = 0
    #: set when the document could not be normalized (no site label)
    note: str | None = None
    new_frequencies_hz: list[int] = field(default_factory=list)
    #: message kinds this window saw for the first time, or whose exemplar changed — the
    #: endpoint delivers them (:func:`app.rr.message_kinds.raise_message_kind_alerts`);
    #: the activity rows are already written, in this transaction.
    message_kind_findings: list[MessageKindFinding] = field(default_factory=list)


def _aware(value: datetime | str | None) -> datetime | None:
    """An instant from the document — ISO-8601 text or a datetime — as aware UTC.

    The document is kept as JSON (that is what ``recorder_observations.payload`` stores),
    so every timestamp in it arrives as a string; a caller handing over real datetimes
    works too.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fill(row: object, **values: Any) -> None:
    """Set each attribute only where the row has nothing — the air never overwrites."""
    for name, value in values.items():
        if value is not None and getattr(row, name, None) is None:
            setattr(row, name, value)


def _upsert_frequency(
    session: Session,
    *,
    site_id: int,
    frequency_hz: int,
    role: str,
    source: str,
    channel_ref: str | None,
    first_seen_at: datetime | None,
    last_seen_at: datetime | None,
    observations: int,
    grants: int,
    meta: dict[str, Any] | None,
) -> bool:
    """Upsert one ``site_frequencies`` row; returns True when it is new.

    ``least``/``greatest`` rather than a plain assignment because windows can arrive out
    of order (a recorder that was offline posts its backlog), and a first sighting must not
    move forward because a later request carried an older window.
    """
    stmt = (
        pg_insert(SiteFrequency)
        .values(
            site_id=site_id,
            frequency_hz=frequency_hz,
            role=role,
            source=source,
            channel_ref=channel_ref,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            observation_count=observations,
            grant_count=grants,
            meta=meta,
        )
        .on_conflict_do_update(
            constraint="uq_site_frequencies_site_freq_source",
            set_={
                "role": text("excluded.role"),
                "channel_ref": func.coalesce(
                    text("excluded.channel_ref"), SiteFrequency.channel_ref
                ),
                "first_seen_at": func.least(
                    SiteFrequency.first_seen_at, text("excluded.first_seen_at")
                ),
                "last_seen_at": func.greatest(
                    SiteFrequency.last_seen_at, text("excluded.last_seen_at")
                ),
                "observation_count": SiteFrequency.observation_count
                + text("excluded.observation_count"),
                "grant_count": SiteFrequency.grant_count + text("excluded.grant_count"),
                "meta": func.coalesce(text("excluded.meta"), SiteFrequency.meta),
                "updated_at": func.now(),
            },
        )
        .returning(text("(xmax = 0)"))
    )
    return bool(session.execute(stmt).scalar_one())


def _upsert_adjacent(
    session: Session,
    *,
    site_id: int,
    entry: dict[str, Any],
    seen_at: datetime,
) -> bool:
    system_id_p25 = _int(entry.get("system_id"))
    rfss = _int(entry.get("rfss"))
    site_number = _int(entry.get("site"))
    if system_id_p25 is None or rfss is None or site_number is None:
        log.info("observations: adjacent site without a full identity, skipped: %s", entry)
        return False
    stmt = (
        pg_insert(AdjacentSite)
        .values(
            site_id=site_id,
            system_id_p25=system_id_p25,
            rfss=rfss,
            site_number=site_number,
            lra=_int(entry.get("lra")),
            control_frequency_hz=_int(entry.get("control_frequency_hz")),
            channel_ref=entry.get("channel"),
            flags=entry.get("flags"),
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            observation_count=1,
        )
        .on_conflict_do_update(
            constraint="uq_adjacent_sites_identity",
            set_={
                "lra": func.coalesce(text("excluded.lra"), AdjacentSite.lra),
                "control_frequency_hz": func.coalesce(
                    text("excluded.control_frequency_hz"), AdjacentSite.control_frequency_hz
                ),
                "channel_ref": func.coalesce(text("excluded.channel_ref"), AdjacentSite.channel_ref),
                "flags": func.coalesce(text("excluded.flags"), AdjacentSite.flags),
                "first_seen_at": func.least(AdjacentSite.first_seen_at, text("excluded.first_seen_at")),
                "last_seen_at": func.greatest(AdjacentSite.last_seen_at, text("excluded.last_seen_at")),
                "observation_count": AdjacentSite.observation_count + 1,
                "updated_at": func.now(),
            },
        )
        .returning(text("(xmax = 0)"))
    )
    return bool(session.execute(stmt).scalar_one())


def _collect(document: dict[str, Any], observed_at: datetime) -> dict[tuple[int, str], dict[str, Any]]:
    """Flatten one document into ``{(frequency_hz, source): row}``, strongest role wins."""
    rows: dict[tuple[int, str], dict[str, Any]] = {}

    def add(
        frequency_hz: int | None,
        *,
        role: str,
        source: str,
        channel_ref: str | None = None,
        first_seen_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        grants: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not frequency_hz:
            return
        key = (int(frequency_hz), source)
        existing = rows.get(key)
        candidate = {
            "frequency_hz": int(frequency_hz),
            "role": role,
            "source": source,
            "channel_ref": channel_ref,
            "first_seen_at": first_seen_at or observed_at,
            "last_seen_at": last_seen_at or observed_at,
            "observations": 1,
            "grants": grants,
            "meta": meta,
        }
        if existing is None:
            rows[key] = candidate
            return
        # Same frequency twice in one window: keep the strongest role, add the grants, and
        # keep the widest interval.  Counting the window twice would inflate every rate.
        if ROLE_RANK.index(role) < ROLE_RANK.index(existing["role"]):
            existing["role"] = role
            existing["channel_ref"] = channel_ref or existing["channel_ref"]
            existing["meta"] = meta or existing["meta"]
        existing["grants"] += grants
        existing["first_seen_at"] = min(existing["first_seen_at"], candidate["first_seen_at"])
        existing["last_seen_at"] = max(existing["last_seen_at"], candidate["last_seen_at"])

    control = document.get("control") or {}
    if isinstance(control, dict):
        add(
            _int(control.get("frequency_hz")),
            role="primary_control",
            source="observed",
            channel_ref=control.get("channel"),
            meta={"tsbk_rate": control.get("tsbk_rate")} if control.get("tsbk_rate") else None,
        )

    for entry in document.get("secondary_control") or ():
        if isinstance(entry, dict):
            add(
                _int(entry.get("frequency_hz")),
                role="alternate_control",
                source="observed",
                channel_ref=entry.get("channel"),
                meta={"service_class": entry.get("service_class")} if entry.get("service_class") else None,
            )

    for entry in document.get("traffic") or ():
        if isinstance(entry, dict):
            add(
                _int(entry.get("frequency_hz")),
                role="voice",
                source="observed",
                channel_ref=entry.get("channel"),
                first_seen_at=_aware(entry.get("first_seen")),
                last_seen_at=_aware(entry.get("last_seen")),
                grants=_int(entry.get("grants")) or 0,
                meta={"calls": entry.get("calls")} if entry.get("calls") is not None else None,
            )

    for value in document.get("configured_control") or ():
        add(_int(value), role="primary_control", source="configured")

    return rows


def record_observation(
    session: Session, document: dict[str, Any], *, recorder_default: str = "unknown"
) -> ObservationResult:
    """Store one observation document and fold it into the audit tables.

    ``document`` is plain JSON — the shape :class:`app.schemas.RecorderObservationIn`
    validates, dumped with ``mode="json"`` — because it is stored verbatim.

    The caller commits.  Everything the document says about the *identity* of the system
    and the site (WACN, system id, NAC, RFSS, site number, LRA) is written only where the
    directory has nothing: the air is authoritative about what it hears, not about what an
    operator has already named.
    """
    system_doc = document.get("system") or {}
    site_doc = document.get("site") or {}
    observed_at = _aware(document.get("observed_at")) or datetime.now(UTC)
    recorder = str(document.get("recorder") or recorder_default)

    # ``label`` is required by the schema but not required to be *non-empty*, and an empty
    # one must not become a ``System("")`` row: unlabeled attaches to the sole system or
    # raises ``UnknownSystem``, which the endpoint answers as a bad document.
    system_id = resolve_system_id(
        session, str(system_doc["label"]), protocol=None, wacn=_int(system_doc.get("wacn"))
    )
    system = session.get(System, system_id)
    _fill(
        system,
        wacn=_int(system_doc.get("wacn")),
        system_id=_int(system_doc.get("system_id")),
        nac=_int(system_doc.get("nac")),
    )

    site_label = (site_doc.get("label") or "").strip() or None
    site_id = upsert_site(session, system_id, site_label)
    if site_id is not None:
        site = session.get(Site, site_id)
        _fill(
            site,
            site_number=_int(site_doc.get("site")),
            rfss=_int(site_doc.get("rfss")),
            lra=_int(site_doc.get("lra")),
            nac=_int(system_doc.get("nac")),
        )

    # ``recorder`` is the software ("trunkd"); ``instance`` is the *machine*.  Only the
    # second tells two boxes watching one site apart, and an older recorder sends none —
    # which is stored as NULL rather than guessed at from the token's label, because a
    # wrong instance is worse than an absent one for every filter that reads it.
    instance = (str(document.get("instance") or "").strip()) or None

    observation = RecorderObservation(
        system_id=system_id,
        site_id=site_id,
        recorder=recorder,
        recorder_version=document.get("recorder_version"),
        instance=instance,
        observed_at=observed_at,
        window_s=_int(document.get("window_s")),
        payload=document,
    )
    session.add(observation)
    session.flush()

    result = ObservationResult(observation_id=observation.id, system_id=system_id, site_id=site_id)
    if site_id is None:
        result.note = "no site label: the document was stored but not normalized"
        log.info("observations: %s posted a document with no site label", recorder)
        return result

    for row in _collect(document, observed_at).values():
        inserted = _upsert_frequency(
            session,
            site_id=site_id,
            frequency_hz=row["frequency_hz"],
            role=row["role"],
            source=row["source"],
            channel_ref=row["channel_ref"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            observations=row["observations"],
            grants=row["grants"],
            meta=row["meta"],
        )
        result.frequencies_upserted += 1
        if inserted:
            result.frequencies_new += 1
            if row["source"] == "observed":
                result.new_frequencies_hz.append(row["frequency_hz"])

    # The control-channel message census (docs/UNKNOWN-DATA-PLAN.md phase 5).  Folded here
    # rather than in the endpoint so the memory of what this site has ever transmitted
    # commits with the window that carried it, and so a replay rebuilds it too.
    result.message_kind_findings = record_message_kinds(
        session, site_id=site_id, payload=document, observed_at=observed_at
    )

    for entry in document.get("adjacent_sites") or ():
        if not isinstance(entry, dict):
            continue
        if _upsert_adjacent(session, site_id=site_id, entry=entry, seen_at=observed_at):
            result.adjacent_new += 1
        result.adjacent_upserted += 1

    log.info(
        "observations: %s site=%s %d frequencies (%d new), %d adjacent (%d new)",
        recorder,
        site_label,
        result.frequencies_upserted,
        result.frequencies_new,
        result.adjacent_upserted,
        result.adjacent_new,
    )
    return result


def last_observed_at(session: Session, site_id: int) -> datetime | None:
    """When the recorder last reported on this site."""
    return session.scalar(
        select(func.max(RecorderObservation.observed_at)).where(RecorderObservation.site_id == site_id)
    )
