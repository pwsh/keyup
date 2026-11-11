"""The directory upserts must not wait on a call's foreign-key locks.

This is a lock-mode regression, not a correctness one, so it is tested the only way a
lock mode can be: with two live connections.  One inserts a call and stays open, which
leaves a ``FOR KEY SHARE`` lock on the system, site, talkgroup and radio rows the call
points at — that is what an FK check takes, and it is held until that transaction ends.
The other then runs the upsert under a short ``lock_timeout``.

An ``ON CONFLICT … DO UPDATE`` whose SET names a unique-key column takes ``FOR UPDATE``
on the conflicting row *from the column list alone*, whatever value it assigns; that
conflicts with ``FOR KEY SHARE`` and waits.  A SET of only non-key columns takes ``FOR NO
KEY UPDATE``, which does not.  In production the first shape put call uploads 79 s behind
a repair pass that was only inserting calls, so:

* every ``upsert_*`` here must come back immediately while a call holds its FK lock, and
* ``test_a_key_column_set_is_what_blocks`` keeps the mechanism itself honest — if that one
  stops timing out, the others have stopped proving anything and the fixture is broken.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ingest.playlist_import import resolve_system
from app.ingest.service import upsert_radio, upsert_site, upsert_system, upsert_talkgroup
from app.models import Call, Radio, Site, Talkgroup
from tests.conftest import requires_db

pytestmark = requires_db

BASE = datetime(2026, 9, 1, 20, 9, 45, tzinfo=UTC)
SYSTEM = "Example System"
SITE = "Central"
TGID = 1101
RADIO_ID = 5250057
#: long enough that a real wait cannot be mistaken for scheduling noise, short enough that
#: a regression fails the suite in seconds rather than hanging it
LOCK_TIMEOUT = "2s"


class Directory:
    """The ids of one committed system/site/talkgroup/radio, for comparing against."""

    def __init__(self, system: int, site: int, talkgroup: int, radio: int) -> None:
        self.system, self.site, self.talkgroup, self.radio = system, site, talkgroup, radio


@pytest.fixture
def directory(session) -> Directory:  # noqa: ANN001
    """A committed directory — the other connection has to be able to see it."""
    system_id = upsert_system(session, SYSTEM, protocol="P25 Phase 1", wacn=None)
    site_id = upsert_site(session, system_id, SITE)
    talkgroup = upsert_talkgroup(session, system_id, TGID, alias=None, seen_at=BASE)
    radio_pk = upsert_radio(
        session, system_id, radio_id=RADIO_ID, wacn=0, home_system=0, seen_at=BASE
    )
    assert site_id is not None
    session.commit()
    return Directory(system_id, site_id, talkgroup.id, radio_pk)


@pytest.fixture
def call_fk_lock(directory: Directory) -> Iterator[Directory]:
    """A second connection holding an uncommitted call that references the directory.

    This is every STT worker mid-transaction, every reconcile batch and every repair pass:
    nothing here updates a directory row, it only points at one.
    """
    from app.db import SessionLocal

    holder: Session = SessionLocal()
    try:
        holder.add(
            Call(
                system_id=directory.system,
                site_id=directory.site,
                talkgroup_id=directory.talkgroup,
                radio_id=directory.radio,
                talkgroup_ids=[TGID],
                ingest_source="file",
                recorded_at=BASE,
                started_at=BASE,
                started_local=BASE.replace(tzinfo=None),
                ended_at=BASE + timedelta(seconds=8),
                time_source="file_write",
                duration_s=Decimal("8.000"),
                audio_state="present",
                transcript_status="pending",
                file_path="synthetic/fk-lock-holder.mp3",
                file_name="fk-lock-holder.mp3",
            )
        )
        holder.flush()  # the INSERT — and its FK checks — but no COMMIT
        yield directory
    finally:
        # Before the session fixture's next TRUNCATE, which would wait on these same rows.
        holder.rollback()
        holder.close()


def _with_lock_timeout(session) -> None:  # noqa: ANN001
    """Fail the current transaction fast instead of waiting out a blocked upsert."""
    session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))


# --------------------------------------------------------------------------------------
# the upserts on the live call-upload path
# --------------------------------------------------------------------------------------


def test_upsert_site_does_not_wait_on_a_call(session, call_fk_lock: Directory) -> None:  # noqa: ANN001
    _with_lock_timeout(session)
    assert upsert_site(session, call_fk_lock.system, SITE) == call_fk_lock.site


def test_upsert_talkgroup_does_not_wait_on_a_call(session, call_fk_lock: Directory) -> None:  # noqa: ANN001
    _with_lock_timeout(session)
    touched = upsert_talkgroup(session, call_fk_lock.system, TGID, alias=None, seen_at=BASE)
    assert touched.id == call_fk_lock.talkgroup


def test_upsert_radio_does_not_wait_on_a_call(session, call_fk_lock: Directory) -> None:  # noqa: ANN001
    _with_lock_timeout(session)
    assert (
        upsert_radio(session, call_fk_lock.system, radio_id=RADIO_ID, wacn=0, home_system=0, seen_at=BASE)
        == call_fk_lock.radio
    )


def test_upsert_system_does_not_wait_on_a_call(session, call_fk_lock: Directory) -> None:  # noqa: ANN001
    _with_lock_timeout(session)
    assert upsert_system(session, SYSTEM, protocol="P25 Phase 1", wacn=None) == call_fk_lock.system


def test_playlist_resolve_system_does_not_wait_on_a_call(session, call_fk_lock: Directory) -> None:  # noqa: ANN001
    """The importer names an existing system the same way ingest does."""
    _with_lock_timeout(session)
    assert resolve_system(session, SYSTEM) == call_fk_lock.system


def test_the_upserts_left_the_rows_alone(session, call_fk_lock: Directory) -> None:  # noqa: ANN001
    """Same rows, same names, after all four have run against a locked directory."""
    _with_lock_timeout(session)
    upsert_site(session, call_fk_lock.system, SITE)
    upsert_talkgroup(session, call_fk_lock.system, TGID, alias=None, seen_at=BASE)
    upsert_radio(
        session, call_fk_lock.system, radio_id=RADIO_ID, wacn=0, home_system=0, seen_at=BASE
    )
    assert session.execute(select(Site.id, Site.name)).all() == [(call_fk_lock.site, SITE)]
    assert session.execute(select(Talkgroup.id, Talkgroup.tgid)).all() == [
        (call_fk_lock.talkgroup, TGID)
    ]
    assert session.execute(select(Radio.id, Radio.radio_id)).all() == [
        (call_fk_lock.radio, RADIO_ID)
    ]


# --------------------------------------------------------------------------------------
# the mechanism itself
# --------------------------------------------------------------------------------------


def test_a_key_column_set_is_what_blocks(session, call_fk_lock: Directory) -> None:  # noqa: ANN001
    """The statement this branch removed, run verbatim: it still waits.

    ``name`` is half of ``uq_sites_system_name``, and assigning it — to the value already
    in the row — is enough to ask for ``FOR UPDATE``.  Nothing about the row changes; only
    the lock does.  Keep this here: it is the proof that the fixture really holds a
    ``FOR KEY SHARE`` lock, which is what makes the tests above mean anything.
    """
    _with_lock_timeout(session)
    with pytest.raises(OperationalError) as blocked:
        session.execute(
            text(
                "INSERT INTO sites (system_id, name) VALUES (:system_id, :name) "
                "ON CONFLICT ON CONSTRAINT uq_sites_system_name DO UPDATE SET name = :name"
            ),
            {"system_id": call_fk_lock.system, "name": SITE},
        )
    assert "lock timeout" in str(blocked.value).lower()
    session.rollback()
