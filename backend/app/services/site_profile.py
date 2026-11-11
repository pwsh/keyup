"""Everything known — and everything *not* known — about one tower site, in one document.

``GET /api/sites/{id}/profile``.  The Sites page has always been able to show the handful
of columns the ``sites`` row carries; the rest of what a site announces about itself has
been arriving in ``recorder_observations.payload`` for months and was readable nowhere:
the service bitmap, the band plan, the secondary control channels, the neighbors, the
packet-data activity.  This module folds the stored windows back into the shape a reader
wants.

Two rules the whole module turns on:

*Merge across instances, newest first.*  A site can be watched by more than one recorder,
and they see different things — one box has the band plan, the other is the one currently
locked to the control channel.  So the newest stored window **per instance** is taken and
the fields are filled from the newest window that has a non-empty answer, rather than from
"the newest window" alone, which would blank half the document every time a second box
posted a thinner one.

*An absence is a fact.*  ``services`` is ``None``, never ``[]``, when no window carried the
bitmap, and :func:`_unknowns` turns each such hole into a sentence.  A panel that renders
only what is known makes a site with one decoded field look as complete as one with twenty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.models import Call, RecorderObservation, Site, SiteMessageKind, System
from app.schemas import (
    SiteProfileAdjacent,
    SiteProfileBand,
    SiteProfileChannel,
    SiteProfileControl,
    SiteProfileDataChannel,
    SiteProfileIdentity,
    SiteProfileMessageKinds,
    SiteProfileObserver,
    SiteProfileOut,
    SiteProfileServices,
    SiteProfileTraffic,
)

# --------------------------------------------------------------------------------------
# the services column on the site list
# --------------------------------------------------------------------------------------


def latest_services(db: Session, site_ids: list[int]) -> dict[int, list[str]]:
    """``{site_id: available services}`` — one ``DISTINCT ON`` for the whole listing.

    The newest window *that carries the bitmap* wins, not the newest window: the recorder
    only sends ``services`` when it has decoded a SYS_SVC_BCAST in that five minutes, so
    taking the newest row outright would show the column empty on a quiet site that
    announced its services ten minutes ago.  ``jsonb_typeof`` rather than ``IS NOT NULL``
    because a JSON ``null`` in the document is not a SQL NULL.
    """
    if not site_ids:
        return {}
    services = RecorderObservation.payload["services"]
    stmt = (
        select(RecorderObservation.site_id, services.label("services"))
        .where(
            RecorderObservation.site_id.in_(site_ids),
            func.jsonb_typeof(services) == "object",
        )
        .ext(distinct_on(RecorderObservation.site_id))
        .order_by(RecorderObservation.site_id, RecorderObservation.observed_at.desc())
    )
    found: dict[int, list[str]] = {}
    for site_id, payload in db.execute(stmt):
        names = _strings(payload.get("available")) if isinstance(payload, dict) else []
        if site_id is not None:
            found[site_id] = names
    return found


# --------------------------------------------------------------------------------------
# reading a payload without trusting it
# --------------------------------------------------------------------------------------


def _strings(value: Any) -> list[str]:
    """A JSON list read as names, skipping anything that is not one."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float)) and str(item)]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _channel(entry: Any) -> SiteProfileChannel | None:
    if not isinstance(entry, dict):
        return None
    return SiteProfileChannel(
        frequency_hz=_int(entry.get("frequency_hz")),
        channel=(str(entry["channel"]) if entry.get("channel") is not None else None),
        tsbk_rate=_float(entry.get("tsbk_rate")),
        service_class=_strings(entry.get("service_class")),
    )


# --------------------------------------------------------------------------------------
# the merge
# --------------------------------------------------------------------------------------


def _windows(db: Session, site_id: int) -> list[tuple[str | None, datetime, dict[str, Any]]]:
    """The newest stored window per recorder instance, newest window first."""
    stmt = (
        select(
            RecorderObservation.instance,
            RecorderObservation.observed_at,
            RecorderObservation.payload,
        )
        .where(RecorderObservation.site_id == site_id)
        .ext(distinct_on(RecorderObservation.instance))
        .order_by(RecorderObservation.instance, RecorderObservation.observed_at.desc())
    )
    rows = [
        (instance, observed_at, payload)
        for instance, observed_at, payload in db.execute(stmt)
        if isinstance(payload, dict)
    ]
    rows.sort(key=lambda row: row[1], reverse=True)
    return rows


class _Merged:
    """The newest non-empty answer for each payload key, across the windows."""

    def __init__(self, windows: list[tuple[str | None, datetime, dict[str, Any]]]) -> None:
        self._payloads = [payload for _instance, _at, payload in windows]

    def value(self, *path: str) -> Any:
        """The first non-null value at ``path``, walking the windows newest first."""
        for payload in self._payloads:
            cursor: Any = payload
            for key in path:
                cursor = cursor.get(key) if isinstance(cursor, dict) else None
            if cursor is not None and cursor != [] and cursor != {}:
                return cursor
        return None

    def mapping(self, *path: str) -> dict[str, Any]:
        value = self.value(*path)
        return value if isinstance(value, dict) else {}

    def sequence(self, *path: str) -> list[Any]:
        value = self.value(*path)
        return value if isinstance(value, list) else []


# --------------------------------------------------------------------------------------
# the sections
# --------------------------------------------------------------------------------------


def _identity(site: Site, system: System | None, merged: _Merged) -> SiteProfileIdentity:
    """The stored row wins over the payload: the row *is* the archive's record of the air.

    The payload fills the two the row has no column for — the system's label as the
    recorder names it, and the WACN — and stands in for anything a merge or a hand edit
    has left null.
    """
    nac = site.nac if site.nac is not None else _int(merged.value("system", "nac"))
    if nac is None and system is not None:
        nac = system.nac
    return SiteProfileIdentity(
        system_id=site.system_id,
        system_name=system.name if system is not None else None,
        system_label=(lambda label: str(label) if label else None)(merged.value("system", "label")),
        wacn=(
            system.wacn
            if system is not None and system.wacn is not None
            else _int(merged.value("system", "wacn"))
        ),
        p25_system_id=(
            system.system_id
            if system is not None and system.system_id is not None
            else _int(merged.value("system", "system_id"))
        ),
        nac=nac,
        nac_hex=None if nac is None else f"0x{nac:X}",
        rfss=site.rfss if site.rfss is not None else _int(merged.value("site", "rfss")),
        site_number=(
            site.site_number if site.site_number is not None else _int(merged.value("site", "site"))
        ),
        lra=site.lra if site.lra is not None else _int(merged.value("site", "lra")),
        rr_site_id=site.rr_site_id,
        rr_synced_at=site.rr_synced_at,
    )


def _control(merged: _Merged) -> SiteProfileControl:
    control = merged.mapping("control")
    secondary = [_channel(entry) for entry in merged.sequence("secondary_control")]
    return SiteProfileControl(
        frequency_hz=_int(control.get("frequency_hz")),
        channel=(str(control["channel"]) if control.get("channel") is not None else None),
        tsbk_rate=_float(control.get("tsbk_rate")),
        secondary_control=[entry for entry in secondary if entry is not None],
        configured_control=[
            value
            for value in (_int(entry) for entry in merged.sequence("configured_control"))
            if value is not None
        ],
    )


def _services(merged: _Merged) -> SiteProfileServices:
    """The bitmap, or a document of nulls saying nobody has decoded one here."""
    services = merged.mapping("services")
    if not services:
        return SiteProfileServices()
    available = _strings(services.get("available"))
    supported = _strings(services.get("supported"))
    offered = set(available)
    return SiteProfileServices(
        available=available,
        supported=supported,
        available_raw=(str(services["available_raw"]) if services.get("available_raw") else None),
        supported_raw=(str(services["supported_raw"]) if services.get("supported_raw") else None),
        request_priority=_int(services.get("request_priority")),
        supported_not_available=[name for name in supported if name not in offered],
    )


def _bands(merged: _Merged) -> list[SiteProfileBand]:
    bands = []
    for entry in merged.sequence("bands"):
        if not isinstance(entry, dict):
            continue
        tdma = entry.get("tdma")
        bands.append(
            SiteProfileBand(
                id=_int(entry.get("id")),
                base_hz=_int(entry.get("base_hz")),
                spacing_hz=_int(entry.get("spacing_hz")),
                offset_hz=_int(entry.get("offset_hz")),
                bandwidth_hz=_int(entry.get("bandwidth_hz")),
                tdma=None if tdma is None else bool(tdma),
            )
        )
    bands.sort(key=lambda band: (band.id is None, band.id or 0))
    return bands


def _traffic(merged: _Merged) -> list[SiteProfileTraffic]:
    rows = []
    for entry in merged.sequence("traffic"):
        if not isinstance(entry, dict):
            continue
        rows.append(
            SiteProfileTraffic(
                frequency_hz=_int(entry.get("frequency_hz")),
                channel=(str(entry["channel"]) if entry.get("channel") is not None else None),
                grants=_int(entry.get("grants")) or 0,
                calls=_int(entry.get("calls")) or 0,
                first_seen=_dt(entry.get("first_seen")),
                last_seen=_dt(entry.get("last_seen")),
            )
        )
    rows.sort(key=lambda row: (-row.grants, -row.calls, row.frequency_hz or 0))
    return rows


@dataclass
class _Announced:
    """One neighbour as the control channel announced it, in the shape the RR matcher reads.

    :func:`app.rr.audit._match_rr_site` resolves a stored ``adjacent_sites`` row; here the
    same announcement is coming out of an observation payload instead.  Rather than copy
    the matching (which knows about colliding ``(rfss, site)`` pairs on this network and
    breaks them by control frequency), the payload entry is given the four attribute names
    the matcher reads.
    """

    rfss: int | None
    site_number: int | None
    control_frequency_hz: int | None
    system_id_p25: int | None


def _adjacent(db: Session, site: Site, merged: _Merged) -> list[SiteProfileAdjacent]:
    """The announced neighbours, each given the name of the site it is known to be.

    Two sources, and the difference between them is the point.  ``known`` means *Keyup*
    knows the tower — a site row, something this archive has heard — and that resolution is
    :func:`app.rr.audit._known_sites`', so the name here and the name the channel audit
    prints for the same neighbour are the same name, decided in one place.  When no Keyup
    site matches, the RadioReference cache is asked: it documents all 35 towers of this
    system, including the ones no recorder here will ever hear, and a neighbour called
    "West Co Simulcast" is worth more to a reader than "RFSS 20 site 20" even though Keyup
    does not know it.  ``source`` says which answered, and only a nameless neighbour —
    neither source — is reported as unnamed.
    """
    from app.rr.audit import _known_sites, _match_rr_site, _rr_sites, resolve_known_site

    index = _known_sites(db)
    rr_rows = _rr_sites(db, site.system_id)
    rows = []
    for entry in merged.sequence("adjacent_sites"):
        if not isinstance(entry, dict):
            continue
        system_id = _int(entry.get("system_id"))
        rfss = _int(entry.get("rfss"))
        site_number = _int(entry.get("site"))
        control_hz = _int(entry.get("control_frequency_hz"))
        known = resolve_known_site(index, system_id, rfss, site_number)
        name = known.name if known is not None else None
        source = "keyup" if name else None
        if name is None and rr_rows:
            announced = _Announced(
                rfss=rfss,
                site_number=site_number,
                control_frequency_hz=control_hz,
                system_id_p25=system_id,
            )
            rr_site, _match = _match_rr_site(announced, rr_rows)
            if rr_site is not None and rr_site.name:
                name, source = rr_site.name, "radioreference"
        flags = entry.get("flags")
        rows.append(
            SiteProfileAdjacent(
                system_id=system_id,
                rfss=rfss,
                site_number=site_number,
                lra=_int(entry.get("lra")),
                control_frequency_hz=control_hz,
                channel=(str(entry["channel"]) if entry.get("channel") is not None else None),
                flags=flags if isinstance(flags, dict) else {},
                name=name,
                source=source,
                known=known is not None,
            )
        )
    rows.sort(key=lambda row: (row.rfss or 0, row.site_number or 0))
    return rows


def _data_channel(merged: _Merged) -> SiteProfileDataChannel | None:
    data = merged.mapping("data_channel")
    if not data:
        return None
    return SiteProfileDataChannel(
        grants=_int(data.get("grants")) or 0,
        pages=_int(data.get("pages")) or 0,
        pdus=_int(data.get("pdus")) or 0,
        channels=[
            value for value in (_int(entry) for entry in (data.get("channels") or [])) if value is not None
        ],
    )


#: An unnamed kind counted no more often than this is a decode artefact, not a message.
#: One occurrence of a ``mfid`` seen nowhere else, carrying a CRC residual its own parity
#: forbids, is a false pass — trunkd's registry classifies a whole page of them
#: (docs/IDENTIFIERS.md §6) — and a neighbour site heard at the edge of reception produces
#: hundreds.  Counting them as "kinds nobody has named" buries the one that matters.
NOISE_MAX_COUNT = 2


def _message_kinds(db: Session, site_id: int) -> SiteProfileMessageKinds:
    """Named versus unnamed, and how much of the unnamed is only noise.

    ``known`` is the flag on the row — set by the built-in P25 table at insert, by
    ``keyup sites name-message-kinds``, or by an operator — and everything else is still
    news, minus the single-decode rubbish :data:`NOISE_MAX_COUNT` separates out.
    """
    rare = SiteMessageKind.count <= NOISE_MAX_COUNT
    known, unknown, noise = db.execute(
        select(
            func.count().filter(SiteMessageKind.known),
            func.count().filter(~SiteMessageKind.known),
            func.count().filter(~SiteMessageKind.known, rare),
        ).where(SiteMessageKind.site_id == site_id)
    ).one()
    return SiteProfileMessageKinds(
        known=int(known), unknown=int(unknown), noise=int(noise), total_seen=int(known) + int(unknown)
    )


# --------------------------------------------------------------------------------------
# what is *not* known
# --------------------------------------------------------------------------------------


def _unknowns(
    profile_site: Site,
    identity: SiteProfileIdentity,
    control: SiteProfileControl,
    services: SiteProfileServices,
    bands: list[SiteProfileBand],
    adjacent: list[SiteProfileAdjacent],
    kinds: SiteProfileMessageKinds,
    windows: int,
) -> list[str]:
    """One short sentence per hole, in the order a reader would ask about them."""
    gaps: list[str] = []
    if windows == 0:
        gaps.append("No recorder has ever posted an observation window for this site.")
        return gaps

    missing = [
        label
        for label, value in (
            ("its NAC", identity.nac),
            ("its RFSS", identity.rfss),
            ("its site number", identity.site_number),
            ("its location registration area", identity.lra),
            ("the system's WACN", identity.wacn),
            ("the P25 system id", identity.p25_system_id),
        )
        if value is None
    ]
    if missing:
        gaps.append(f"Never announced (or never decoded): {', '.join(missing)}.")

    if services.available is None:
        gaps.append(
            "No service bitmap: the site has not sent a SYS_SVC_BCAST in any stored window, "
            "so which services it offers is unknown."
        )
    elif services.supported_not_available:
        gaps.append(
            f"{len(services.supported_not_available)} service(s) the site supports but is not "
            "currently offering."
        )

    if control.frequency_hz is None:
        gaps.append("No control-channel frequency in any stored window.")
    unresolved = sum(1 for entry in control.secondary_control if entry.frequency_hz is None)
    if unresolved:
        gaps.append(
            f"{unresolved} secondary control channel(s) announced as a channel number whose band "
            "plan has not been heard, so the frequency cannot be computed."
        )
    if not control.configured_control:
        gaps.append("No recorder reports a configured control-channel list for this site.")

    if not bands:
        gaps.append("No IDEN band plan heard, so channel numbers cannot be turned into frequencies.")

    unnamed = [entry for entry in adjacent if entry.name is None]
    if unnamed:
        named = ", ".join(
            f"RFSS {entry.rfss if entry.rfss is not None else '?'} "
            f"site {entry.site_number if entry.site_number is not None else '?'}"
            for entry in unnamed[:4]
        )
        gaps.append(
            f"{len(unnamed)} announced neighbour(s) match no site Keyup or RadioReference "
            f"knows: {named}."
        )
    elif not adjacent:
        gaps.append("No adjacent sites announced in any stored window.")

    recurring = kinds.unknown - kinds.noise
    if recurring:
        gaps.append(f"{recurring} recurring control-channel message kind(s) nobody has named.")

    if profile_site.rr_site_id is None:
        gaps.append("Not mapped to a RadioReference site, so nothing here can be checked against it.")

    return gaps


# --------------------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------------------


def build(db: Session, site: Site) -> SiteProfileOut:
    """The whole profile for one site."""
    windows = _windows(db, site.id)
    merged = _Merged(windows)
    system = db.get(System, site.system_id)

    identity = _identity(site, system, merged)
    control = _control(merged)
    services = _services(merged)
    bands = _bands(merged)
    adjacent = _adjacent(db, site, merged)
    kinds = _message_kinds(db, site.id)
    call_count = int(
        db.scalar(select(func.count()).select_from(Call).where(Call.site_id == site.id)) or 0
    )

    return SiteProfileOut(
        site_id=site.id,
        site_name=site.name,
        description=site.description,
        notes=site.notes,
        call_count=call_count,
        observed_by=[
            SiteProfileObserver(
                instance=instance,
                observed_at=observed_at,
                tsbk_rate=_float((payload.get("control") or {}).get("tsbk_rate"))
                if isinstance(payload.get("control"), dict)
                else None,
                control_frequency_hz=_int((payload.get("control") or {}).get("frequency_hz"))
                if isinstance(payload.get("control"), dict)
                else None,
            )
            for instance, observed_at, payload in windows
            # A window from a binary too old to send one has no instance to name, and a
            # profile that lists `null` among its observers is naming a recorder that does
            # not exist.  It still contributes its fields to the merge above — as the
            # oldest fallback, which is what it is.
            if instance
        ],
        identity=identity,
        control=control,
        services=services,
        bands=bands,
        traffic=_traffic(merged),
        adjacent_sites=adjacent,
        data_channel=_data_channel(merged),
        lsd_nonzero=_int(merged.value("lsd_nonzero")),
        message_kinds=kinds,
        unknowns=_unknowns(site, identity, control, services, bands, adjacent, kinds, len(windows)),
    )
