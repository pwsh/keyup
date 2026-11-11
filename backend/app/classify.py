"""Rules-based talkgroup classification — the operator's mapping, written down.

Production has 1,840 talkgroups on one system, of which about forty carry traffic
anybody reads.  Exactly one of them had a ``category`` and none had an ``agency_code``,
which is why ``?agency=`` matched nothing and a CAD integration matching incidents to
radio traffic had only time to go on: the agency and unit terms have nothing to match
(docs/TALKGROUPS.md).  Filling those two columns in by hand is forty edits that
nobody can review, cannot be repeated on a re-import, and are lost the next time a
recorder renames a channel.

So the mapping lives in a file instead: an ordered list of ``alias regex → {category,
service_tag, agency_code}`` (``keyup classify-talkgroups --rules FILE``; the format is
``examples/classification.example.csv``), replayed by
:func:`plan_talkgroups`.  Three properties make it safe to run against a live archive:

**First match wins, and only the first.**  Rules are read in file order and the first
whose pattern is found in the alias decides.  A talkgroup no pattern matches is not
touched at all — it is not classified as ``other``, because "nobody has said" and
"somebody said it is miscellaneous" are different facts and the UI draws them
differently.

**Fill-only by default.**  A column that already holds a value is left alone and reported
as ``kept``.  The operator, the RadioReference sync and the CSV importer all write these
same columns, and a classifier that overwrote them would silently undo somebody's
correction on every run.  ``overwrite=True`` is the explicit opposite and says so in the
report.

**Nothing is written without ``apply``.**  :func:`plan_talkgroups` only reads; it returns
the changes it *would* make, which is what the CLI prints as a table.  The same plan is
then handed to :func:`apply_changes`, so what was reviewed is what is written.

:func:`plan_radios` is the same idea one join further out.  A radio has no alias to match,
but it has a history: ``radio_affiliations`` records which talkgroups it has been heard
on and how often.  A radio whose traffic is overwhelmingly on Central City PD's channels
is a Central City PD radio, and that is the only evidence this archive has.  A tie is left
alone rather than broken arbitrarily — a radio that works two agencies equally is a real
thing (a shared supervisor, a mutual-aid unit), and guessing at it would put a wrong
``agency_code`` in front of every ``?agency=`` and ``?unit=`` query.

There is no agreed ``unit_id`` prefix convention, so nothing here
derives an agency from a unit id; if one is agreed later it belongs beside
:func:`plan_radios` as a second, higher-confidence source.
"""

from __future__ import annotations

import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    TALKGROUP_CATEGORIES,
    Agency,
    Radio,
    RadioAffiliation,
    System,
    Talkgroup,
    canonical_service_tag,
)

log = logging.getLogger("app.classify")

#: The talkgroup columns a rule may set.  Deliberately short: these three are what
#: ``?agency=`` and the UI read, and a rule that could set ``priority`` or
#: ``transcribe`` would be a policy engine rather than a classifier.
FIELDS = ("category", "service_tag", "agency_code")


class RulesError(ValueError):
    """A rules file that cannot be trusted — reported with the line that is wrong."""


@dataclass(frozen=True, slots=True)
class Rule:
    """One ``alias regex → columns`` line, compiled and validated."""

    #: the pattern as written, which is what the report and the ``--json`` output name
    source: str
    pattern: re.Pattern[str]
    category: str | None
    service_tag: str | None
    agency_code: str | None
    note: str
    #: 1-based line in the rules file, so an error names something a person can find
    line: int

    @property
    def sets(self) -> dict[str, str]:
        """The columns this rule actually assigns; a blank cell assigns nothing."""
        values = {
            "category": self.category,
            "service_tag": self.service_tag,
            "agency_code": self.agency_code,
        }
        return {key: value for key, value in values.items() if value}


def load_rules(path: str | Path) -> list[Rule]:
    """Read and validate a rules file.  Raises :class:`RulesError` on anything unusable.

    Validation is strict and happens here rather than at write time on purpose: a
    category the UI cannot draw or a service tag RadioReference does not know would be
    written to hundreds of rows before anybody noticed, and the file is small enough to
    check in full every run.  ``agency_code`` is checked for *shape* only — whether the
    code exists is a database question, asked once in :func:`plan_talkgroups`.

    Lines beginning with ``#`` are comments, so the file can explain itself.  There is no
    default file: the rules are a deployment's own mapping of its own channels.
    """
    path = Path(path)
    if not path.is_file():
        raise RulesError(f"no rules file at {path}")

    rules: list[Rule] = []
    with path.open(newline="") as handle:
        # Strip comments before the CSV reader sees them: a `#` line is not a row, and
        # DictReader would otherwise hand back a record whose every field is None.
        numbered = [(n, line) for n, line in enumerate(handle, start=1) if not line.lstrip().startswith("#")]
        if not numbered:
            raise RulesError(f"{path} has no rules")
        reader = csv.DictReader(text for _, text in numbered)
        # DictReader consumed the header, so the nth data row is numbered[n + 1].
        for offset, row in enumerate(reader):
            line = numbered[offset + 1][0] if offset + 1 < len(numbered) else numbered[-1][0]
            source = (row.get("pattern") or "").strip()
            if not source:
                continue
            try:
                pattern = re.compile(source, re.IGNORECASE)
            except re.error as exc:
                raise RulesError(f"{path}:{line}: {source!r} is not a regex ({exc})") from exc

            category = (row.get("category") or "").strip().lower() or None
            if category is not None and category not in TALKGROUP_CATEGORIES:
                raise RulesError(
                    f"{path}:{line}: {category!r} is not a talkgroup category "
                    f"(one of {', '.join(TALKGROUP_CATEGORIES)})"
                )
            raw_tag = (row.get("service_tag") or "").strip()
            service_tag = canonical_service_tag(raw_tag)
            if raw_tag and service_tag is None:
                raise RulesError(f"{path}:{line}: {raw_tag!r} is not a RadioReference service tag")
            agency_code = (row.get("agency_code") or "").strip().upper() or None

            rule = Rule(
                source=source,
                pattern=pattern,
                category=category,
                service_tag=service_tag,
                agency_code=agency_code,
                note=(row.get("note") or "").strip(),
                line=line,
            )
            if not rule.sets:
                raise RulesError(f"{path}:{line}: {source!r} sets no columns")
            rules.append(rule)

    if not rules:
        raise RulesError(f"{path} has no rules")
    return rules


def match(rules: list[Rule], alias: str | None) -> Rule | None:
    """The first rule whose pattern is found in ``alias``, or ``None``."""
    text = (alias or "").strip()
    if not text:
        return None
    for rule in rules:
        if rule.pattern.search(text):
            return rule
    return None


# --------------------------------------------------------------------------------------
# talkgroups
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Change:
    """What one talkgroup would gain, and what it already had that was left alone."""

    talkgroup_id: int
    tgid: int
    alias: str | None
    rule: str
    #: column → new value
    sets: dict[str, str] = field(default_factory=dict)
    #: column → the operator's existing value, not overwritten (fill-only)
    kept: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "talkgroup_id": self.talkgroup_id,
            "tgid": self.tgid,
            "alias": self.alias,
            "rule": self.rule,
            "sets": dict(self.sets),
            "kept": dict(self.kept),
        }


def _system_id(session: Session, name: str | None) -> int | None:
    if not name:
        return None
    system_id = session.scalar(select(System.id).where(func.upper(System.name) == name.strip().upper()))
    if system_id is None:
        raise ValueError(f"no system named {name!r}")
    return system_id


def plan_talkgroups(
    session: Session,
    rules: list[Rule],
    *,
    system: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Work out what the rules would do.  Reads only; nothing is written.

    The returned report carries the changes themselves (``changes``), the talkgroups no
    rule matched (``unmatched``, capped in the CLI's table but complete here) and — the
    reason this is a separate step — ``unknown_agencies``: agency codes a rule names that
    the ``agencies`` table does not have.  Writing one would fail on the foreign key
    partway through a batch, so the plan refuses instead and says which code is wrong.
    """
    system_id = _system_id(session, system)
    known_agencies = {code for (code,) in session.execute(select(Agency.code))}
    wanted = {rule.agency_code for rule in rules if rule.agency_code}
    unknown = sorted(wanted - known_agencies)

    stmt = select(Talkgroup).order_by(Talkgroup.tgid)
    if system_id is not None:
        stmt = stmt.where(Talkgroup.system_id == system_id)

    changes: list[Change] = []
    unmatched: list[dict[str, Any]] = []
    matched = 0
    for tg in session.scalars(stmt):
        rule = match(rules, tg.alias)
        if rule is None:
            unmatched.append({"talkgroup_id": tg.id, "tgid": tg.tgid, "alias": tg.alias})
            continue
        matched += 1
        change = Change(talkgroup_id=tg.id, tgid=tg.tgid, alias=tg.alias, rule=rule.source)
        for column, value in rule.sets.items():
            current = getattr(tg, column, None)
            if current and not overwrite:
                if current != value:
                    change.kept[column] = current
                continue
            if current == value:
                continue
            change.sets[column] = value
        if change.sets or change.kept:
            changes.append(change)

    return {
        "rules": len(rules),
        "system": system,
        "overwrite": overwrite,
        "talkgroups_matched": matched,
        "talkgroups_unmatched": len(unmatched),
        "talkgroups_changed": sum(1 for c in changes if c.sets),
        "columns_set": sum(len(c.sets) for c in changes),
        "columns_kept": sum(len(c.kept) for c in changes),
        "unknown_agencies": unknown,
        "changes": changes,
        "unmatched": unmatched,
    }


def apply_changes(session: Session, changes: list[Change]) -> int:
    """Write the planned column values.  Returns the number of talkgroups updated.

    One UPDATE per talkgroup rather than a bulk statement: at forty rows the round trips
    are free, and a per-row statement is what makes a partial failure name the row.
    """
    written = 0
    for change in changes:
        if not change.sets:
            continue
        session.execute(
            update(Talkgroup).where(Talkgroup.id == change.talkgroup_id).values(**change.sets)
        )
        written += 1
    return written


# --------------------------------------------------------------------------------------
# radios
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RadioChange:
    """One radio's inferred agency, with the evidence that inferred it."""

    radio_id_fk: int
    radio_id: int
    label: str | None
    agency_code: str
    #: calls on that agency's channels, and on every agency's, so the margin is visible
    calls: int
    total: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "radio_id_fk": self.radio_id_fk,
            "radio_id": self.radio_id,
            "label": self.label,
            "agency_code": self.agency_code,
            "calls": self.calls,
            "total": self.total,
        }


def plan_radios(
    session: Session, *, system: str | None = None, overwrite: bool = False
) -> dict[str, Any]:
    """Infer ``radios.agency_code`` from where each radio has actually been heard.

    The evidence is ``radio_affiliations.call_count`` rolled up by the agency of the
    talkgroup, and the rule is a *strict* majority of one agency over every other: a tie
    is skipped and counted, never broken.  Radios that have only ever been heard on
    unclassified channels contribute nothing and are simply absent from the plan — which
    is why this is worth running *after* the talkgroup pass and not before.
    """
    system_id = _system_id(session, system)

    stmt = (
        select(
            RadioAffiliation.radio_id_fk,
            Talkgroup.agency_code,
            func.sum(RadioAffiliation.call_count),
        )
        .join(Talkgroup, Talkgroup.id == RadioAffiliation.talkgroup_id_fk)
        .where(Talkgroup.agency_code.is_not(None))
        .group_by(RadioAffiliation.radio_id_fk, Talkgroup.agency_code)
    )
    if system_id is not None:
        stmt = stmt.where(Talkgroup.system_id == system_id)

    tally: dict[int, dict[str, int]] = defaultdict(dict)
    for radio_pk, code, count in session.execute(stmt):
        tally[int(radio_pk)][str(code)] = int(count or 0)
    if not tally:
        return {
            "radios_considered": 0,
            "radios_changed": 0,
            "radios_tied": 0,
            "radios_kept": 0,
            "changes": [],
        }

    radios = {
        row.id: row
        for row in session.scalars(select(Radio).where(Radio.id.in_(sorted(tally))))
        if system_id is None or row.system_id == system_id
    }

    changes: list[RadioChange] = []
    tied = kept = 0
    for radio_pk, counts in sorted(tally.items()):
        radio = radios.get(radio_pk)
        if radio is None:
            continue
        if radio.agency_code and not overwrite:
            kept += 1
            continue
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        best_code, best_count = ranked[0]
        if len(ranked) > 1 and ranked[1][1] == best_count:
            tied += 1
            continue
        if radio.agency_code == best_code:
            continue
        changes.append(
            RadioChange(
                radio_id_fk=radio_pk,
                radio_id=int(radio.radio_id),
                label=radio.unit_id or radio.alias,
                agency_code=best_code,
                calls=best_count,
                total=sum(counts.values()),
            )
        )

    return {
        "radios_considered": len(radios),
        "radios_changed": len(changes),
        "radios_tied": tied,
        "radios_kept": kept,
        "changes": changes,
    }


def apply_radio_changes(session: Session, changes: list[RadioChange]) -> int:
    """Write the inferred ``radios.agency_code`` values.  Returns rows updated."""
    for change in changes:
        session.execute(
            update(Radio).where(Radio.id == change.radio_id_fk).values(agency_code=change.agency_code)
        )
    return len(changes)


__all__ = [
    "FIELDS",
    "Change",
    "RadioChange",
    "Rule",
    "RulesError",
    "apply_changes",
    "apply_radio_changes",
    "load_rules",
    "match",
    "plan_radios",
    "plan_talkgroups",
]
