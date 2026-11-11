"""Reference data a deployment loads into its own database.

``keyup seed-agencies`` reads an agency CSV with the four columns an agency list needs —
``code,name,short_name,kind`` (``examples/agencies.example.csv``) — usually exported from
the dispatch center's CAD system, so the codes here match the ones it uses.  Talkgroups
and radios point at ``agencies.code``, which is what makes "which channels would a Central
City PD call be on?" answerable.  Nothing is seeded unless a CSV is named: the file is the
deployment's, not the package's (``--csv`` or ``KEYUP_SEED_AGENCIES``).

The table is not only the seeded rows: an operator can add agencies the list has no code
for (``agencies.source = 'local'``).  So the seed is deliberately not a mirror — see
:func:`seed_agencies` for the three rules that keep a re-seed from undoing somebody's work.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agency

#: The provenance :func:`seed_agencies` writes and refreshes (``app.models.AGENCY_SOURCES``).
SEED_SOURCE = "seed"


def seed_agencies(session: Session, *, path: str | Path, force: bool = False) -> int:
    """Insert or refresh the seeded agency rows.  Returns the number of CSV rows read.

    Three rules, because the table now holds rows the CSV does not know about:

    1. **A row the CSV does not name is never touched, and never deleted.**  That covers
       every ``local`` agency, and it is why the seed does no ``DELETE`` at all — a
       talkgroup or a radio may be pointing at the row, and the agency vanishing would
       leave a dangling code the API then refuses to save.
    2. **A ``local`` row whose code collides with a CSV code keeps its own values.**  The
       operator typed that code deliberately; if the seeded list later issues the same one, the
       right fix is a person looking at both, not a silent overwrite.  ``force`` does not
       override this — it would change the row's provenance, not just its name.
    3. **A ``seed`` row that somebody has edited here (``edited_at`` is set) keeps its
       name, short name and kind** unless ``force`` is passed.  Without the flag the seed
       is safe to run on a cron; with it, ``keyup seed-agencies --force`` is the explicit
       "the shared list wins" button, which is also how an upstream rename lands.

    A row this app has never edited is refreshed exactly as it always was, so the ordinary
    case — a fresh database, or a deployment nobody has renamed anything on — is unchanged.
    """
    path = Path(path)
    with path.open(newline="") as handle:
        rows = [
            {
                "code": r["code"].strip(),
                "name": r["name"].strip(),
                "short_name": r["short_name"].strip(),
                "kind": r["kind"].strip(),
            }
            for r in csv.DictReader(handle)
            if r.get("code", "").strip()
        ]
    if not rows:
        return 0

    # One SELECT for the whole file rather than a `session.get` round trip per row, and it also
    # gives the loop below the *existing* source, which is what rules 2 and 3 turn on.
    codes = [r["code"] for r in rows]
    existing = {
        agency.code: agency for agency in session.scalars(select(Agency).where(Agency.code.in_(codes)))
    }

    for row in rows:
        current = existing.get(row["code"])
        if current is None:
            session.add(Agency(**row, source=SEED_SOURCE, active=True))
            continue
        if current.source != SEED_SOURCE:  # rule 2
            continue
        if current.edited_at is not None and not force:  # rule 3
            continue
        current.name = row["name"]
        current.short_name = row["short_name"]
        current.kind = row["kind"]
        if force:
            # A forced re-seed is the shared list winning, so the local edit is spent:
            # clearing the stamp puts the row back under ordinary refresh.
            current.edited_at = None
    session.flush()
    return len(rows)


def mark_agency_edited(agency: Agency) -> None:
    """Record that an operator changed this row, so the seed stops overwriting it."""
    agency.edited_at = datetime.now(UTC)


def seed_audio_profiles(session: Session) -> int:
    """Insert the three shipped audio profiles that are not already there.  Returns how many.

    The migration inserts the same rows, so on a deployment this is a no-op; it exists for
    the case the migration cannot cover — a database whose tables have been truncated, which
    is what every test does between cases — and for a future builtin, which appears here and
    is picked up by the next call rather than by a data migration nobody will run twice.

    Existing rows are left exactly as they are, including a builtin an operator has edited:
    a re-seed is "make sure these exist", never "put them back the way they shipped".
    """
    from app.audioproc.profiles import BUILTIN_PROFILES  # noqa: PLC0415 - avoids an import cycle
    from app.models import AudioProfile  # noqa: PLC0415

    names = [profile["name"] for profile in BUILTIN_PROFILES]
    have = set(session.scalars(select(AudioProfile.name).where(AudioProfile.name.in_(names))))
    added = 0
    for profile in BUILTIN_PROFILES:
        if profile["name"] in have:
            continue
        session.add(
            AudioProfile(
                name=profile["name"],
                description=profile["description"],
                chain=profile["chain"],
                builtin=True,
            )
        )
        added += 1
    session.flush()
    return added


__all__ = [
    "SEED_SOURCE",
    "mark_agency_edited",
    "seed_agencies",
    "seed_audio_profiles",
]
