"""Import a directory of aliases from sdrtrunk's ``default.xml`` or from a CSV.

sdrtrunk's playlist is the recorder's own name list, so importing it fills in every
talkgroup alias, group and color at once instead of waiting for each talkgroup to be
heard.  Only ``protocol="APCO25"`` identifiers are read — the same playlist can carry
DMR, LTR and NBFM entries whose numbers mean something else entirely.

The playlist shape (docs/ARCHITECTURE.md 4.2)::

    <playlist version="4">
      <alias name="EXAMPLE LAW 1" list="Example System" group="Law" color="-16776961">
        <id xsi:type="talkgroup" value="1601" protocol="APCO25"/>
        <id xsi:type="radio" value="9200001" protocol="APCO25"/>
      </alias>
    </playlist>

The CSV shape is ``tgid,alias,group,category,service_tag,agency_code,color`` with a header
row.  ``group`` may also be spelled ``group_name`` and ``service_tag`` may also be spelled
``tag`` — the first is what ``GET /talkgroups/export`` emits and the second is what
trunk-recorder calls it — so the export → edit → import round trip is lossless and a
directory exported from a recorder imports without being rewritten first
(docs/TALKGROUPS.md).

Two cells are checked rather than trusted, and a bad one is *counted and dropped* rather
than failing the file, because one typo in a thousand-row directory should not cost the
other 999: ``service_tag`` against RadioReference's vocabulary
(:data:`app.models.RR_SERVICE_TAGS`), and ``agency_code`` against the ``agencies`` table.
The second matters more than it looks — that column is a foreign key, so an unrecognized
code would not land wrong, it would abort the whole import at whichever row held it.

The XML is parsed with ``defusedxml``, because the playlist is an upload: an operator
points the importer at a file, and a file that arrived from somewhere else must not be
able to spend this process' memory on entity expansion.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree
from sqlalchemy import literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Agency, Radio, System, Talkgroup, canonical_service_tag

log = logging.getLogger("app.ingest.playlist")

XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
#: the only protocol whose talkgroup and radio numbers match what this archive records
PROTOCOL = "APCO25"


@dataclass
class AliasEntry:
    """One playlist alias, flattened."""

    name: str
    group: str | None = None
    category: str | None = None
    service_tag: str | None = None
    agency_code: str | None = None
    color: str | None = None
    #: the two per-talkgroup export checkboxes.  ``None`` means the column was absent from
    #: the CSV and the existing value is left alone — an import must not silently retick
    #: boxes an operator cleared just because the file predates the columns.
    export_broadcastify: bool | None = None
    export_bcfy_calls: bool | None = None
    export_openmhz: bool | None = None
    tgids: list[int] = field(default_factory=list)
    radio_ids: list[int] = field(default_factory=list)


@dataclass
class ImportSummary:
    source: str
    aliases: int = 0
    talkgroups_created: int = 0
    talkgroups_updated: int = 0
    radios_created: int = 0
    radios_updated: int = 0
    skipped_non_apco25: int = 0
    #: rows whose ``service_tag``/``tag`` cell named no RadioReference tag; the rest of
    #: the row was still imported
    skipped_service_tags: int = 0
    #: rows whose ``agency_code`` cell named no row in ``agencies``; same treatment, and
    #: for a harder reason — the column is a foreign key, so writing one would not merely
    #: be wrong, it would abort the whole import at whichever row happened to hold the typo
    skipped_agency_codes: int = 0


def _optional_bool(raw: str | None) -> bool | None:
    """``true``/``false`` from a CSV cell; ``None`` for an absent or unreadable one.

    ``None`` rather than a default is the point: a directory exported before these columns
    existed has no cell here, and an import of it must leave the flags as they are.
    """
    text = (raw or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def _sdrtrunk_color(raw: str | None) -> str | None:
    """sdrtrunk stores a signed 32-bit ARGB int (``-16776961``); return ``#rrggbb``."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("#"):
        return raw
    try:
        value = int(raw)
    except ValueError:
        return raw or None
    if value == 0:
        return None
    return f"#{value & 0xFFFFFF:06x}"


def parse_playlist(path: str | Path) -> tuple[list[AliasEntry], int]:
    """Read ``default.xml`` into alias entries.  Returns ``(entries, skipped)``.

    Parsed with ``defusedxml``: this document arrives over ``POST /talkgroups/import``, and
    stdlib ``ElementTree`` still honours the entity definitions that turn a two-kilobyte
    file into gigabytes of expanded text (M3).
    """
    root = ElementTree.parse(str(path)).getroot()
    entries: list[AliasEntry] = []
    skipped = 0

    for alias in root.iter("alias"):
        name = (alias.get("name") or "").strip()
        if not name:
            continue
        entry = AliasEntry(
            name=name,
            group=(alias.get("group") or "").strip() or None,
            color=_sdrtrunk_color(alias.get("color")),
        )
        for ident in alias.iter("id"):
            kind = (ident.get(XSI_TYPE) or ident.get("type") or "").lower()
            if (ident.get("protocol") or "").upper() != PROTOCOL:
                skipped += 1
                continue
            value = (ident.get("value") or "").strip()
            if not value.isdigit():
                continue
            if "talkgroup" in kind:
                entry.tgids.append(int(value))
            elif "radio" in kind:
                entry.radio_ids.append(int(value))
        if entry.tgids or entry.radio_ids:
            entries.append(entry)
    return entries, skipped


def parse_csv(path: str | Path) -> tuple[list[AliasEntry], int]:
    """Read ``tgid,alias,group,category,service_tag,agency_code,color`` and the two
    ``export_*`` checkboxes.

    Returns ``(entries, bad_service_tags)`` — see the module docstring for why an
    unrecognized service tag is counted rather than raised.
    """
    entries: list[AliasEntry] = []
    bad_tags = 0
    with Path(path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            raw_tgid = (row.get("tgid") or "").strip()
            if not raw_tgid.isdigit():
                continue
            # ``tag`` is trunk-recorder's name for the same column, so a directory exported
            # from a recorder imports without being rewritten first.
            raw_tag = (row.get("service_tag") or row.get("tag") or "").strip()
            service_tag = canonical_service_tag(raw_tag)
            if raw_tag and service_tag is None:
                bad_tags += 1
                log.warning("csv import: %r is not a RadioReference service tag; ignored", raw_tag)
            entries.append(
                AliasEntry(
                    name=(row.get("alias") or "").strip(),
                    # ``group_name`` is what ``GET /talkgroups/export`` calls this column
                    # and ``group`` is what an sdrtrunk-shaped CSV calls it.  Both are
                    # accepted, because the export → edit → import round trip is the
                    # documented way to classify a directory and a round trip that
                    # silently blanks a column is worse than one that refuses.
                    group=(row.get("group") or row.get("group_name") or "").strip() or None,
                    category=(row.get("category") or "").strip() or None,
                    service_tag=service_tag,
                    agency_code=(row.get("agency_code") or "").strip() or None,
                    color=_sdrtrunk_color(row.get("color")),
                    export_broadcastify=_optional_bool(row.get("export_broadcastify")),
                    export_bcfy_calls=_optional_bool(row.get("export_bcfy_calls")),
                    export_openmhz=_optional_bool(row.get("export_openmhz")),
                    tgids=[int(raw_tgid)],
                )
            )
    return entries, bad_tags


def _inserted_flag():  # noqa: ANN201
    """``xmax = 0`` on a RETURNING row means the upsert inserted rather than updated."""
    return literal_column("(xmax = 0)").label("inserted")


def resolve_system(session: Session, name: str | None) -> int:
    """Pick the system to import into.

    With a name, create it if need be.  Without one, use the only system there is —
    guessing between several would silently mis-assign every alias.
    """
    if name:
        # DO NOTHING, not ``DO UPDATE SET name = name``: ``systems.name`` is the unique key
        # itself, and an ON CONFLICT SET that names a key column takes ``FOR UPDATE`` on the
        # row, which conflicts with the ``FOR KEY SHARE`` every in-flight call insert holds
        # on its system (see the lock note atop ingest/service.py).  The follow-up SELECT is
        # race-safe: DO NOTHING waits out a concurrent inserter and the unique constraint
        # guarantees the row exists by the time it runs.
        stmt = (
            pg_insert(System)
            .values(name=name)
            .on_conflict_do_nothing(index_elements=[System.name])
            .returning(System.id)
        )
        system_id = session.execute(stmt).scalar_one_or_none()
        if system_id is not None:
            return system_id
        return session.execute(select(System.id).where(System.name == name)).scalar_one()
    ids = session.execute(select(System.id).order_by(System.id)).scalars().all()
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise ValueError("no systems yet — ingest a recording first or pass --system NAME")
    raise ValueError(f"{len(ids)} systems exist — pass --system NAME to say which")


def import_entries(
    session: Session,
    entries: list[AliasEntry],
    *,
    system_id: int,
    source: str = "playlist",
    skipped: int = 0,
    skipped_service_tags: int = 0,
) -> ImportSummary:
    """Apply alias entries to ``talkgroups`` and ``radios``.

    Unlike ingest, the importer *does* overwrite aliases: the playlist is the recorder
    operator's own naming, and importing it is an explicit act.  A talkgroup that has
    never been heard is created with ``call_count`` 0 so it shows up in the directory.
    """
    summary = ImportSummary(
        source=source,
        aliases=len(entries),
        skipped_non_apco25=skipped,
        skipped_service_tags=skipped_service_tags,
    )

    # ``talkgroups.agency_code`` is a foreign key, so an unrecognized code is not a cell
    # that lands wrong — it is a ForeignKeyViolation that aborts the whole import at
    # whichever row happens to carry the typo, losing the other 999.  One query up front
    # turns that into the same "counted and dropped" the service tags already get.
    known_agencies = {
        code for (code,) in session.execute(select(Agency.code)) if code is not None
    }

    for entry in entries:
        if entry.agency_code and entry.agency_code not in known_agencies:
            log.warning(
                "import: %r is not an agency code in this deployment; ignored", entry.agency_code
            )
            entry.agency_code = None
            summary.skipped_agency_codes += 1
        values: dict[str, object] = {"alias": entry.name or None}
        if entry.group is not None:
            values["group_name"] = entry.group
        if entry.category is not None:
            values["category"] = entry.category
        if entry.service_tag is not None:
            values["service_tag"] = entry.service_tag
        if entry.agency_code is not None:
            values["agency_code"] = entry.agency_code
        if entry.color is not None:
            values["color"] = entry.color
        if entry.export_broadcastify is not None:
            values["export_broadcastify"] = entry.export_broadcastify
        if entry.export_bcfy_calls is not None:
            values["export_bcfy_calls"] = entry.export_bcfy_calls
        if entry.export_openmhz is not None:
            values["export_openmhz"] = entry.export_openmhz

        # Ascending, and talkgroups before radios: §5.2b in app/db.py.  The importer runs
        # alone against the directory, but the rule costs nothing to keep here.
        for tgid in sorted(entry.tgids):
            stmt = (
                pg_insert(Talkgroup)
                .values(system_id=system_id, tgid=tgid, call_count=0, **values)
                .on_conflict_do_update(constraint="uq_talkgroups_system_tgid", set_=values)
                .returning(Talkgroup.id, _inserted_flag())
            )
            _, inserted = session.execute(stmt).one()
            if inserted:
                summary.talkgroups_created += 1
            else:
                summary.talkgroups_updated += 1

        for radio_id in sorted(entry.radio_ids):
            # Update every form of the radio on this system (plain and fully qualified),
            # then create the plain row when the playlist named one we have never heard.
            touched = session.execute(
                update(Radio)
                .where(Radio.system_id == system_id, Radio.radio_id == radio_id)
                .values(alias=entry.name or None)
            ).rowcount
            if touched:
                summary.radios_updated += touched
            else:
                session.execute(
                    pg_insert(Radio)
                    .values(
                        system_id=system_id,
                        radio_id=radio_id,
                        wacn=0,
                        home_system_id=0,
                        alias=entry.name or None,
                        kind="unknown",
                        call_count=0,
                    )
                    .on_conflict_do_nothing(constraint="uq_radios_system_radio_wacn_home")
                )
                summary.radios_created += 1

    return summary


def import_file(
    session: Session, path: str | Path, *, system: str | None = None
) -> ImportSummary:
    """Import ``default.xml`` or a CSV, chosen by suffix."""
    path = Path(path)
    system_id = resolve_system(session, system)
    if path.suffix.lower() == ".xml":
        entries, skipped = parse_playlist(path)
        return import_entries(
            session, entries, system_id=system_id, source=str(path), skipped=skipped
        )
    entries, bad_tags = parse_csv(path)
    return import_entries(
        session, entries, system_id=system_id, source=str(path), skipped_service_tags=bad_tags
    )


__all__ = [
    "AliasEntry",
    "ImportSummary",
    "import_entries",
    "import_file",
    "parse_csv",
    "parse_playlist",
    "resolve_system",
]
