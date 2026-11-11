"""RadioReference → ``site_frequencies`` with ``source='radioreference'``.

RR is somebody else's server and a Premium subscription the operator pays for, so this is
deliberately unhurried: one sync per system per ``RR_SYNC_HOURS``, three SOAP calls per
sync (plus at most one shared ``getTag`` read for the whole run), and a manual trigger
(``POST /api/admin/radioreference/sync``) that is the only way to go faster.

Matching an RR site to a Keyup site is the one genuinely ambiguous step.  In order:

1. ``sites.rr_site_id`` — set by an operator, and always right;
2. the site number the air announced (``sites.site_number``) against RR's ``siteNumber``;
3. the NAC, when both sides have one and it is unique within the system;
4. the description/name, compared case- and punctuation-insensitively.

Every fallback logs which rule matched, and an RR site that matches nothing — or matches
more than one Keyup site — logs that too and is skipped rather than guessed at.  Sites are
never *created* from RR: a site in the directory is one the recorder has actually heard.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    SERVICE_TAG_TO_CATEGORY,
    RrSite,
    Site,
    SiteFrequency,
    System,
    Talkgroup,
    canonical_service_tag,
)
from app.rr.client import RadioReferenceClient, RadioReferenceError, credentials_present, make_client

log = logging.getLogger("app.rr.sync")

__all__ = [
    "RrSyncSummary",
    "TagTable",
    "service_tag_of",
    "sync_due",
    "sync_system",
    "systems_to_sync",
]

#: RR's ``use`` column → the role vocabulary in ``site_frequencies``
USE_TO_ROLE = {"d": "primary_control", "a": "alternate_control"}

_PUNCT = re.compile(r"[^a-z0-9]+")


@dataclass
class RrSyncSummary:
    """One system's sync, as the admin endpoint and the log line report it."""

    system_id: int
    system_name: str
    rr_system_id: int | None = None
    sites_seen: int = 0
    sites_matched: int = 0
    rr_sites_cached: int = 0
    frequencies: int = 0
    frequencies_removed: int = 0
    talkgroups_named: int = 0
    talkgroups_tagged: int = 0
    skipped: str | None = None
    unmatched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id,
            "system": self.system_name,
            "rr_system_id": self.rr_system_id,
            "sites_seen": self.sites_seen,
            "sites_matched": self.sites_matched,
            "rr_sites_cached": self.rr_sites_cached,
            "frequencies": self.frequencies,
            "frequencies_removed": self.frequencies_removed,
            "talkgroups_named": self.talkgroups_named,
            "talkgroups_tagged": self.talkgroups_tagged,
            "skipped": self.skipped,
            "unmatched": self.unmatched,
        }


def _norm(value: Any) -> str:
    return _PUNCT.sub(" ", str(value or "").lower()).strip()


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def hz(freq: Any) -> int | None:
    """RR gives megahertz as a double; the archive stores hertz as an integer."""
    try:
        value = float(freq)
    except (TypeError, ValueError):
        return None
    return round(value * 1_000_000) if value > 0 else None


def role_of(use: Any) -> str:
    """``'d'`` → primary control, ``'a'`` → alternate control, anything else → voice."""
    return USE_TO_ROLE.get(str(use or "").strip().lower(), "voice")


def _nac_values(value: Any) -> set[int]:
    """RR writes a P25 NAC in hex, as the database page shows it (``\"034\"``
    = 52).  Parse it as hex; accepting a decimal reading too would collide distinct
    NACs (hex ``\"052\"`` = 82 vs ``\"034\"`` = 52 both look like decimal 52)."""
    import contextlib

    out: set[int] = set()
    text = str(value or "").strip()
    if not text:
        return out
    with contextlib.suppress(ValueError):
        out.add(int(text, 16))
    return out


def match_site(rr_site: dict[str, Any], candidates: list[Site]) -> tuple[Site | None, str]:
    """Find the Keyup site an RR site describes.  Returns ``(site, how)``."""
    rr_site_id = _int(rr_site.get("siteId"))
    if rr_site_id is not None:
        exact = [s for s in candidates if s.rr_site_id == rr_site_id]
        if len(exact) == 1:
            return exact[0], "rr_site_id"

    # NAC first.  On this L3Harris network the NAC is *per site* (one site 0x293,
    # every neighbor a different value), so it is the strongest identifier the air
    # carries.  It is only ambiguous — and so skipped — on a system that really does
    # share one NAC across sites, where more than one candidate would match.  RR may
    # encode it in decimal or hex, so both readings are accepted.
    nacs = _nac_values(rr_site.get("nac"))
    if nacs:
        by_nac = [s for s in candidates if s.nac in nacs]
        if len(by_nac) == 1:
            return by_nac[0], "nac"
        if len(by_nac) > 1:
            return None, "ambiguous nac"

    rfss = _int(rr_site.get("rfss"))
    number = _int(rr_site.get("siteNumber"))
    if rfss is not None and number is not None:
        by_pair = [
            s for s in candidates if s.rfss == rfss and s.site_number == number
        ]
        if len(by_pair) == 1:
            return by_pair[0], "rfss+site_number"
        if len(by_pair) > 1:
            return None, "ambiguous rfss+site_number"

    if number is not None:
        # A bare site number, but never across a NAC or RFSS the other side disagrees
        # with — that would be a different tower that merely shares the number (this
        # network numbers every site "1").
        by_number = [
            s
            for s in candidates
            if s.site_number == number
            and not (nacs and s.nac is not None and s.nac not in nacs)
            and not (rfss is not None and s.rfss is not None and s.rfss != rfss)
        ]
        if len(by_number) == 1:
            return by_number[0], "site_number"
        if len(by_number) > 1:
            return None, "ambiguous site_number"
        if any(site.site_number is not None for site in candidates):
            return None, f"site number {number} is not in the directory"

    descr = _norm(rr_site.get("siteDescr"))
    if descr:
        by_name = [
            s
            for s in candidates
            if _norm(s.name) == descr
            or (_norm(s.name) and _norm(s.name) in descr)
            or _norm(s.description) == descr
        ]
        if len(by_name) == 1:
            return by_name[0], "description"
        if len(by_name) > 1:
            return None, "ambiguous description"

    return None, "no match"


def _store_site_frequencies(
    session: Session, site: Site, rr_site: dict[str, Any], now: datetime
) -> tuple[int, int]:
    """Replace this site's ``radioreference`` rows with what RR says today."""
    seen: set[int] = set()
    for entry in rr_site.get("siteFreqs") or ():
        if not isinstance(entry, dict):
            continue
        frequency_hz = hz(entry.get("freq"))
        if frequency_hz is None:
            continue
        seen.add(frequency_hz)
        lcn = entry.get("lcn")
        stmt = (
            pg_insert(SiteFrequency)
            .values(
                site_id=site.id,
                frequency_hz=frequency_hz,
                role=role_of(entry.get("use")),
                source="radioreference",
                channel_ref=str(lcn) if lcn not in (None, "") else None,
                first_seen_at=now,
                last_seen_at=now,
                observation_count=0,
                grant_count=0,
                meta={
                    "use": entry.get("use") or None,
                    "lcn": lcn,
                    "color_code": entry.get("colorCode"),
                    "chan_id": entry.get("chanId"),
                    "rr_site_id": _int(rr_site.get("siteId")),
                },
            )
            .on_conflict_do_update(
                constraint="uq_site_frequencies_site_freq_source",
                set_={
                    "role": text("excluded.role"),
                    "channel_ref": text("excluded.channel_ref"),
                    # first_seen_at is when RR first documented it; last_seen_at is the last
                    # sync that still found it.  Neither says anything about the air.
                    "first_seen_at": func.least(SiteFrequency.first_seen_at, text("excluded.first_seen_at")),
                    "last_seen_at": text("excluded.last_seen_at"),
                    "meta": text("excluded.meta"),
                    "updated_at": func.now(),
                },
            )
        )
        session.execute(stmt)

    # A frequency RR has dropped must stop counting as "documented", or it would sit in the
    # missing list for ever.
    stale = delete(SiteFrequency).where(
        SiteFrequency.site_id == site.id,
        SiteFrequency.source == "radioreference",
        SiteFrequency.frequency_hz.notin_(seen or {0}),
    )
    removed = session.execute(stale).rowcount or 0
    site.rr_site_id = _int(rr_site.get("siteId")) or site.rr_site_id
    site.rr_synced_at = now
    if site.nac is None:
        site.nac = _int(rr_site.get("nac"))
    if site.rfss is None:
        site.rfss = _int(rr_site.get("rfss"))
    return len(seen), removed


def _control_frequency_hz(rr_site: dict[str, Any]) -> int | None:
    """The site's control channel: the ``siteFreq`` RR marks primary (``use=='d'``), and
    failing that the first frequency it lists — the same value the neighbor announcement
    carries, so it is what an adjacent-site lookup keys on."""
    freqs = [f for f in (rr_site.get("siteFreqs") or ()) if isinstance(f, dict)]
    for entry in freqs:
        if str(entry.get("use") or "").strip().lower() == "d":
            control = hz(entry.get("freq"))
            if control is not None:
                return control
    for entry in freqs:
        control = hz(entry.get("freq"))
        if control is not None:
            return control
    return None


def _rr_county(rr_site: dict[str, Any]) -> str | None:
    """RR names the county under one of a few keys depending on the record; None if absent."""
    for key in ("ctName", "county", "countyName", "ctid_name"):
        value = str(rr_site.get(key) or "").strip()
        if value:
            return value
    return None


def _one_nac(value: Any) -> int | None:
    """A single NAC from RR's hex text, matching :func:`match_site`'s reading."""
    nacs = _nac_values(value)
    return next(iter(nacs)) if len(nacs) == 1 else None


def _json_safe(value: Any) -> Any:
    """Make an RR structure JSON-serializable for a JSONB column: zeep returns
    frequencies as ``Decimal``, which psycopg's JSON encoder rejects."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _cache_rr_site(session: Session, system: System, rr_site: dict[str, Any], now: datetime) -> None:
    """Upsert one RR site into ``rr_sites`` — every site, matched to a Keyup site or not.

    This is purely a reference cache: it never touches ``sites`` or ``site_frequencies``.
    The channel audit resolves the neighbors a control channel announces against it.
    """
    rr_site_id = _int(rr_site.get("siteId"))
    values = {
        "system_id": system.id,
        "rr_site_id": rr_site_id,
        "rr_system_id": _int(rr_site.get("systemId") or rr_site.get("sysid")),
        "site_number": _int(rr_site.get("siteNumber")),
        "rfss": _int(rr_site.get("rfss")),
        "nac": _one_nac(rr_site.get("nac")),
        "name": (str(rr_site.get("siteDescr") or "").strip() or None),
        "county": _rr_county(rr_site),
        "control_frequency_hz": _control_frequency_hz(rr_site),
        "frequencies": _json_safe(rr_site.get("siteFreqs") or []),
        "synced_at": now,
    }
    # Two partial unique indexes: RR's site id when it carries one, the (rfss, site_number)
    # pair when it does not.  ``index_where`` picks the matching one as the conflict target.
    updates = {k: text(f"excluded.{k}") for k in values if k != "system_id"}
    updates["updated_at"] = func.now()
    if rr_site_id is not None:
        stmt = pg_insert(RrSite).values(**values).on_conflict_do_update(
            index_elements=["system_id", "rr_site_id"],
            index_where=text("rr_site_id IS NOT NULL"),
            set_=updates,
        )
    else:
        stmt = pg_insert(RrSite).values(**values).on_conflict_do_update(
            index_elements=["system_id", "rfss", "site_number"],
            index_where=text("rr_site_id IS NULL"),
            set_=updates,
        )
    session.execute(stmt)


class TagTable:
    """RR's ``tagId`` → ``tagDescr`` table, fetched at most once per sync run.

    ``getTag(0, authInfo)`` returns *every* frequency and talkgroup tag definition — the
    same forty-odd rows whatever system is being synced — while a single system's
    talkgroup list can carry thousands of references into it.  So the table is fetched
    lazily (a run whose talkgroups all carry their own ``tagDescr`` never asks for it at
    all) and then reused for every system of the run; ids are never hardcoded, because
    they are RR's to renumber.

    A client without a ``tags`` method, or an RR that refuses the call, leaves the table
    empty rather than failing the sync: the tag names are a bonus on top of the site
    frequencies that are the point, and the per-talkgroup ``tagDescr`` usually answers
    anyway.
    """

    def __init__(self, client: RadioReferenceClient) -> None:
        self._client = client
        self._names: dict[int, str] | None = None

    def _load(self) -> dict[int, str]:
        names: dict[int, str] = {}
        getter = getattr(self._client, "tags", None)
        if getter is None:
            return names
        try:
            rows = getter() or []
        except RadioReferenceError as exc:
            log.warning("rr sync: tag table unavailable: %s", exc)
            return names
        for row in rows:
            if not isinstance(row, dict):
                continue
            tag_id = _int(row.get("tagId") or row.get("tagid") or row.get("id"))
            name = str(row.get("tagDescr") or row.get("tagDescription") or "").strip()
            if tag_id is not None and name:
                names[tag_id] = name
        return names

    def name_of(self, tag_id: Any) -> str | None:
        """The tag name RR files under ``tag_id``, or None when it names none."""
        key = _int(tag_id)
        if key is None:
            return None
        if self._names is None:
            self._names = self._load()
        return self._names.get(key)


def _tag_entries(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``tags`` array of one RR talkgroup record, tolerating a bare single tag."""
    tags = entry.get("tags") or entry.get("tag") or ()
    if isinstance(tags, dict):
        tags = [tags]
    return [t for t in tags if isinstance(t, dict)]


def service_tag_of(entry: dict[str, Any], tags: TagTable) -> str | None:
    """The RR service tag of one talkgroup record, by name.

    Each ``tags`` entry carries ``tagDescr`` on most RR records, so that is read first and
    the id→name table is only consulted for the ones that carry a bare ``tagId``.  A
    talkgroup can carry several tags; the first one RR's own vocabulary recognizes wins,
    and only when none is recognized is an unknown name returned — verbatim, so an RR that
    grows a tag records what it actually said instead of silently dropping it.
    """
    names = [
        name
        for name in (
            str(tag.get("tagDescr") or "").strip() or tags.name_of(tag.get("tagId"))
            for tag in _tag_entries(entry)
        )
        if name
    ]
    for name in names:
        canonical = canonical_service_tag(name)
        if canonical is not None:
            return canonical
    return names[0] if names else None


def _name_talkgroups(
    session: Session, system: System, talkgroups: list[dict[str, Any]], tags: TagTable
) -> tuple[int, int]:
    """Fill in aliases, descriptions and service tags the directory does not have.

    Never an overwrite: an operator who renamed or reclassified a channel outranks RR, and
    the alias the recorder put on the call is history either way.  ``category`` follows the
    service tag only when *it* is blank as well, so an operator who filed a channel under
    ``interop`` keeps that bucket even when RR later says "Law Tac" — and an RR tag this
    build has never heard of is still recorded in ``service_tag``, leaving ``category``
    alone rather than guessing.

    Returns ``(named, tagged)`` — rows whose text was filled in, and rows that gained a
    service tag.
    """
    named = 0
    tagged = 0
    by_tgid = {
        row.tgid: row
        for row in session.scalars(select(Talkgroup).where(Talkgroup.system_id == system.id))
    }
    for entry in talkgroups:
        if not isinstance(entry, dict):
            continue
        tgid = _int(entry.get("tgDec") or entry.get("tgid") or entry.get("dec"))
        row = by_tgid.get(tgid) if tgid is not None else None
        if row is None:
            continue
        alias = (entry.get("tgAlpha") or entry.get("alphaTag") or "").strip() or None
        description = (entry.get("tgDescr") or entry.get("description") or "").strip() or None
        touched = False
        if alias and not (row.alias or "").strip():
            row.alias = alias
            touched = True
        if description and not (row.description or "").strip():
            row.description = description
            touched = True
        service_tag = service_tag_of(entry, tags)
        if service_tag and not (row.service_tag or "").strip():
            row.service_tag = service_tag
            tagged += 1
            category = SERVICE_TAG_TO_CATEGORY.get(service_tag)
            if category and not (row.category or "").strip():
                row.category = category
        named += 1 if touched else 0
    return named, tagged


def sync_system(
    session: Session,
    system: System,
    client: RadioReferenceClient,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    with_talkgroups: bool = True,
    tags: TagTable | None = None,
) -> RrSyncSummary:
    """Pull one system's sites (and optionally talkgroup names) from RadioReference.

    ``tags`` is the run's shared RR tag table; a caller that syncs one system on its own
    can leave it out and get a table of its own.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    summary = RrSyncSummary(
        system_id=system.id, system_name=system.name, rr_system_id=system.rr_system_id
    )
    if system.rr_system_id is None:
        summary.skipped = "no rr_system_id"
        return summary

    details = client.trs_details(system.rr_system_id) or {}
    if system.wacn is None:
        system.wacn = _int(details.get("wacn"))
    if system.system_id is None:
        system.system_id = _int(details.get("sysid") or details.get("systemId"))
    if system.nac is None:
        system.nac = _int(details.get("nac"))

    candidates = list(session.scalars(select(Site).where(Site.system_id == system.id)))
    for rr_site in client.trs_sites(system.rr_system_id) or []:
        if not isinstance(rr_site, dict):
            continue
        summary.sites_seen += 1
        # Retain every site RR lists — a reference cache the audit resolves neighbors
        # against — before the discarding step below keeps only the one local tower.
        _cache_rr_site(session, system, rr_site, now)
        summary.rr_sites_cached += 1
        site, how = match_site(rr_site, candidates)
        label = str(rr_site.get("siteDescr") or rr_site.get("siteNumber") or rr_site.get("siteId"))
        if site is None:
            summary.unmatched.append(label)
            log.info(
                "rr sync: %s site %r does not map to a Keyup site (%s); skipped",
                system.name,
                label,
                how,
            )
            continue
        if how != "rr_site_id":
            log.info("rr sync: %s site %r matched %r by %s", system.name, label, site.name, how)
        stored, removed = _store_site_frequencies(session, site, rr_site, now)
        summary.sites_matched += 1
        summary.frequencies += stored
        summary.frequencies_removed += removed

    if with_talkgroups:
        try:
            summary.talkgroups_named, summary.talkgroups_tagged = _name_talkgroups(
                session,
                system,
                client.trs_talkgroups(system.rr_system_id) or [],
                tags if tags is not None else TagTable(client),
            )
        except RadioReferenceError as exc:  # the sites are the point; names are a bonus
            log.warning("rr sync: %s talkgroup names unavailable: %s", system.name, exc)

    system.rr_synced_at = now
    log.info("rr sync: %s", summary.as_dict())
    return summary


def systems_to_sync(
    session: Session, *, settings: Settings | None = None, force: bool = False, now: datetime | None = None
) -> list[System]:
    """Systems with an RR id whose last sync is older than ``RR_SYNC_HOURS``.

    Applies ``RR_SYSTEMS`` (name → RR id) to any system that has no id of its own, so the
    mapping can live in the environment *or* in the row.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    mapping = {str(k).strip(): int(v) for k, v in (settings.rr_systems or {}).items()}
    systems = list(session.scalars(select(System).order_by(System.name)))
    for system in systems:
        if system.rr_system_id is None and system.name in mapping:
            system.rr_system_id = mapping[system.name]

    cutoff = now - timedelta(hours=max(0, settings.rr_sync_hours))
    due = []
    for system in systems:
        if system.rr_system_id is None:
            continue
        last = system.rr_synced_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if force or last is None or last <= cutoff:
            due.append(system)
    return due


def sync_due(
    session: Session,
    *,
    settings: Settings | None = None,
    client: RadioReferenceClient | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> list[RrSyncSummary]:
    """Sync every system that is due.  A no-op when RR is not configured."""
    settings = settings or get_settings()
    if client is None and not credentials_present(settings):
        return []
    due = systems_to_sync(session, settings=settings, force=force, now=now)
    if not due:
        return []
    client = client or make_client(settings)
    if client is None:  # pragma: no cover - credentials_present already said otherwise
        return []
    summaries = []
    # One tag table for the whole run: ``getTag`` answers for every system at once.
    tags = TagTable(client)
    for system in due:
        try:
            summaries.append(
                sync_system(session, system, client, settings=settings, now=now, tags=tags)
            )
        except RadioReferenceError as exc:
            log.warning("rr sync: %s failed: %s", system.name, exc)
            summaries.append(
                RrSyncSummary(
                    system_id=system.id,
                    system_name=system.name,
                    rr_system_id=system.rr_system_id,
                    skipped=str(exc),
                )
            )
    return summaries
