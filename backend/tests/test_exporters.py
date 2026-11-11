"""The outbound exporters: the safety gates first, then the request shapes.

Nothing in this file touches Broadcastify or OpenMHz.  The HTTP boundary is mocked with an
``httpx.MockTransport`` in every test that gets as far as a request, and the two
golden-shape tests assert the exact fields each service's own source says it wants.

The tests that matter most are the gating ones.  A live deployment is streaming a 400 000
call archive migration into the same table the live recorder writes to, and an exporter
that published one of those rows would be republishing a decade of old traffic as though it
were happening now.  ``test_backlog_call_is_never_exported`` and
``test_call_before_the_watermark_is_never_exported`` are the two that would catch it.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import re
import socket
import struct
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.exporters import clock, gate, ledger
from app.exporters.base import (
    CallFacts,
    ExportError,
    PermanentExportError,
    SkipCall,
    UploadResult,
    facts_for,
)
from app.exporters.broadcastify import (
    DUPLICATE_REASON,
    LATE_REASON,
    BroadcastifyClient,
    content_type_for,
    encoding_for,
    endpoint,
    format_talkgroup,
    metadata_fields,
    monitoring_url,
    mp3_bitrate_kbps,
    parse_stage1,
    response_class,
    stage1_record,
)
from app.exporters.openmhz import OpenMhzClient, form_fields, source_list
from app.exporters.worker import ExportWorker
from app.models import ActivityEvent, Call, CallExport, Radio, Setting, System, Talkgroup
from tests.conftest import requires_db

pytestmark = requires_db


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def seed_call(
    session,  # noqa: ANN001
    *,
    root: Path | None = None,
    tgid: int = 1701,
    priority: str | None = "live",
    started_at: datetime | None = None,
    duration: float = 3.0,
    encrypted: bool | None = False,
    with_talkgroup: bool = True,
    radio_id: int | None = 5250057,
    frequency_hz: int | None = 851_012_500,
    audio_state: str = "present",
    file_path: str | None = None,
    talker_alias: str | None = None,
    radio_alias: str | None = None,
) -> Call:
    """One system + talkgroup + call, shaped the way the push path leaves it."""
    system = session.scalar(select(System).where(System.name == "Example System"))
    if system is None:
        system = System(name="Example System", protocol="P25 Phase 1")
        session.add(system)
        session.flush()
    talkgroup = None
    if with_talkgroup:
        talkgroup = session.scalar(
            select(Talkgroup).where(Talkgroup.system_id == system.id, Talkgroup.tgid == tgid)
        )
        if talkgroup is None:
            talkgroup = Talkgroup(system_id=system.id, tgid=tgid, alias="EXAMPLE EVENT 1")
            session.add(talkgroup)
            session.flush()
    radio = None
    if radio_id is not None:
        radio = session.scalar(select(Radio).where(Radio.system_id == system.id, Radio.radio_id == radio_id))
        if radio is None:
            radio = Radio(system_id=system.id, radio_id=radio_id)
            session.add(radio)
        radio.alias = radio_alias
        session.flush()

    # A live call's ``started_at`` is "just now"; the tests that care about the watermark
    # stamp it with an explicit cycle first and then seed, so "now" lands after the mark.
    started = started_at or datetime.now(UTC)
    path = file_path or f"20260901/EXAMPLE EVENT 1/call-{int(started.timestamp() * 1000)}-{tgid}.mp3"
    call = Call(
        system_id=system.id,
        talkgroup_id=talkgroup.id if talkgroup else None,
        talkgroup_ids=[tgid] if talkgroup else [],
        radio_id=radio.id if radio else None,
        ingest_priority=priority,
        recorded_at=started + timedelta(seconds=duration),
        started_at=started,
        started_local=started.replace(tzinfo=None),
        ended_at=started + timedelta(seconds=duration),
        duration_s=Decimal(str(duration)),
        frequency_hz=frequency_hz,
        encrypted=encrypted,
        emergency=False,
        audio_state=audio_state,
        talker_alias=talker_alias,
        file_path=path,
        file_name=Path(path).name,
        talkgroup_alias_at_record="EXAMPLE EVENT 1",
    )
    session.add(call)
    session.commit()
    if root is not None:
        target = Path(root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        # A real, tiny, ffmpeg-decodable MP3 is not needed: the transcode is stubbed in the
        # worker tests and exercised for real in test_transcode_applies_the_normalize_filter.
        target.write_bytes(b"\xff\xfb\x90d" + b"\x00" * 256)
    return call


@pytest.fixture
def export_settings(tmp_path) -> Settings:  # noqa: ANN001
    """Settings with both services credentialled but **both switches off**."""
    return Settings(
        _env_file=None,
        audio_root=tmp_path,
        tz="America/Winnipeg",
        broadcastify_api_key="test-bcfy-key",
        broadcastify_system_id="34",
        broadcastify_url="https://api.broadcastify.test/call-upload",
        openmhz_api_key="test-omhz-key",
        openmhz_short_name="examplesys",
        openmhz_url="https://api.openmhz.test",
        export_min_interval_s=0.0,
        export_max_attempts=3,
        # No SNTP query from a unit test: the clock module's own tests bind a fake
        # responder, and everything else here wants an *unmeasured* clock, which is the
        # state that must never pause an upload.
        export_ntp_server="",
    )


def enable(session, service: str, *, value: bool = True) -> None:  # noqa: ANN001
    """Flip a runtime switch the way the Admin page does."""
    from app import runtime_settings

    key = f"export_{service}_enabled"
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        session.add(row)
    row.value = value
    session.commit()
    runtime_settings.invalidate()


class RecordingClient:
    """A stand-in export client that records what it was handed."""

    def __init__(self, service: str = "openmhz", *, error: Exception | None = None) -> None:
        self.service = service
        self.error = error
        self.uploads: list[tuple[CallFacts, bytes]] = []

    def configured(self) -> bool:
        return True

    def upload(self, facts: CallFacts, audio: bytes) -> UploadResult:
        self.uploads.append((facts, audio))
        if self.error is not None:
            raise self.error
        return UploadResult(remote_id="remote-1", response="0 remote-1", response_code=0)


@pytest.fixture
def worker(session, export_settings, tmp_path, monkeypatch):  # noqa: ANN001, ARG001
    """An :class:`ExportWorker` with one recording client and a stubbed transcode."""
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a-bytes")
    client = RecordingClient()
    built = ExportWorker(
        settings=export_settings,
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
    )
    built.test_client = client  # type: ignore[attr-defined]
    return built


# --------------------------------------------------------------------------------------
# the gate: live band
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("priority", "expected"),
    [
        ("live", True),
        ("urgent", True),
        ("100", True),
        ("499", True),
        ("backlog", False),
        ("bulk", False),
        ("500", False),
        ("900", False),
        ("", False),
        ("nonsense", False),
        (None, False),
    ],
)
def test_is_live_band(priority, expected) -> None:  # noqa: ANN001
    """NULL and anything unrecognized fail closed — a bulk import looks exactly like NULL."""

    class Row:
        ingest_priority = priority

    assert gate.is_live_band(Row()) is expected


def test_backlog_call_is_never_exported(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The migration's own rows must never reach a service, switch on or not.

    This is the requirement the whole module exists for: a 400 000-call archive migration
    is running at ``priority=backlog`` into the same table.
    """
    enable(session, "openmhz")
    worker.cycle()  # sets the watermark
    live = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    backlog = seed_call(session, root=tmp_path, tgid=1102, priority="backlog")
    bulk = seed_call(session, root=tmp_path, tgid=1103, priority="bulk")
    unmarked = seed_call(session, root=tmp_path, tgid=1104, priority=None)

    worker.cycle()

    uploaded = {facts.call_id for facts, _ in worker.test_client.uploads}
    assert uploaded == {live.id}
    for call in (backlog, bulk, unmarked):
        assert session.scalar(select(CallExport).where(CallExport.call_id == call.id)) is None, (
            f"call {call.id} should not even have a ledger row"
        )


def test_call_before_the_watermark_is_never_exported(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Enabling a service publishes what comes next, never a backfill of what is already here."""
    old = seed_call(
        session, root=tmp_path, tgid=1701, priority="live",
        started_at=datetime.now(UTC) - timedelta(days=400),
    )  # fmt: skip
    recent = seed_call(
        session, root=tmp_path, tgid=1102, priority="live",
        started_at=datetime.now(UTC) - timedelta(minutes=10),
    )  # fmt: skip
    enable(session, "openmhz")
    worker.cycle()  # stamps the watermark at "now"
    after = seed_call(session, root=tmp_path, tgid=1103, priority="live")

    worker.cycle()

    uploaded = {facts.call_id for facts, _ in worker.test_client.uploads}
    assert uploaded == {after.id}
    assert session.scalar(select(CallExport).where(CallExport.call_id == old.id)) is None
    assert session.scalar(select(CallExport).where(CallExport.call_id == recent.id)) is None


def test_the_second_guard_catches_a_row_the_watermark_outran(session, worker, tmp_path) -> None:  # noqa: ANN001
    """A pending ledger row whose watermark moved must not be uploaded on its own momentum."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")
    ledger.discover(
        session,
        service="openmhz",
        watermark=gate.watermark(session, "openmhz"),
        settings=worker.settings,
    )
    assert session.scalar(select(CallExport).where(CallExport.call_id == call.id)) is not None

    # Somebody re-enabled the service after this row was written: the watermark is now
    # newer than the call.  discover() would not create the row again, but it already
    # exists — the in-Python guard is the one that has to refuse it.
    gate.clear_watermark(session, "openmhz")
    gate.set_watermark(session, "openmhz", datetime.now(UTC) + timedelta(minutes=1))
    session.commit()

    worker.cycle()

    assert worker.test_client.uploads == []
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row.status == "skipped"
    assert "outside the live export window" in row.skip_reason


# --------------------------------------------------------------------------------------
# the gate: the watermark itself
# --------------------------------------------------------------------------------------


def test_watermark_is_stamped_on_enable_and_cleared_on_disable(session, worker) -> None:  # noqa: ANN001
    assert gate.watermark(session, "openmhz") is None

    enable(session, "openmhz")
    worker.cycle()
    first = gate.watermark(session, "openmhz")
    assert first is not None

    # A second cycle must not move it — it is a floor, not a rolling window.
    worker.cycle()
    assert gate.watermark(session, "openmhz") == first

    enable(session, "openmhz", value=False)
    worker.cycle()
    assert gate.watermark(session, "openmhz") is None


def test_the_watermark_key_is_not_an_editable_setting() -> None:
    """It is worker bookkeeping; the Admin page must not be able to write it."""
    from app.runtime_settings import SETTABLE_KEYS

    assert gate.watermark_key("openmhz") not in SETTABLE_KEYS
    assert gate.watermark_key("broadcastify") not in SETTABLE_KEYS


# --------------------------------------------------------------------------------------
# the switches
# --------------------------------------------------------------------------------------


def test_both_services_ship_disabled() -> None:
    """The hard requirement: neither switch may default to on."""
    from app.runtime_settings import SETTINGS_SPEC

    plain = Settings(_env_file=None)
    assert plain.export_broadcastify_enabled is False
    assert plain.export_openmhz_enabled is False
    keys = {key for key, *_ in SETTINGS_SPEC}
    assert {"export_broadcastify_enabled", "export_openmhz_enabled", "export_max_attempts"} <= keys


def test_a_disabled_service_uploads_nothing(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Off means off: no ledger rows, no requests, even with a fresh live call sitting there."""
    seed_call(session, root=tmp_path, priority="live")

    worker.cycle()

    assert worker.test_client.uploads == []
    assert session.scalars(select(CallExport)).all() == []


def test_flipping_the_switch_takes_effect_on_the_next_cycle(session, worker, tmp_path) -> None:  # noqa: ANN001
    worker.cycle()
    assert worker.test_client.uploads == []

    enable(session, "openmhz")
    worker.cycle()  # this one stamps the watermark
    call = seed_call(session, root=tmp_path, priority="live")
    worker.cycle()

    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [call.id]


def test_an_unconfigured_service_does_nothing(session, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A switch on with no credentials must not even build a queue."""
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a")
    bare = Settings(_env_file=None, audio_root=tmp_path, openmhz_api_key="", openmhz_short_name="")
    built = ExportWorker(settings=bare, session_factory=SessionLocal, audio_root=tmp_path)
    enable(session, "openmhz")
    seed_call(session, root=tmp_path, priority="live")

    built.cycle()

    assert session.scalars(select(CallExport)).all() == []


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------


def test_a_call_is_uploaded_exactly_once(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The UNIQUE (call_id, service) dedupe, driven through repeated cycles."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    for _ in range(4):
        worker.cycle()

    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [call.id]
    rows = session.scalars(select(CallExport).where(CallExport.call_id == call.id)).all()
    assert len(rows) == 1
    assert rows[0].status == "uploaded"
    assert rows[0].uploaded_at is not None
    assert rows[0].remote_id == "remote-1"


def test_discover_is_idempotent(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Two discovery passes over the same window create one row, not two."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")
    mark = gate.watermark(session, "openmhz")

    first = ledger.discover(session, service="openmhz", watermark=mark, settings=worker.settings)
    second = ledger.discover(session, service="openmhz", watermark=mark, settings=worker.settings)

    assert (first, second) == (1, 0)
    assert len(session.scalars(select(CallExport).where(CallExport.call_id == call.id)).all()) == 1


def test_discover_writes_a_pass_in_one_insert(session, worker, tmp_path) -> None:  # noqa: ANN001
    """One multi-row INSERT per chunk, not one round trip per call."""
    from sqlalchemy import event

    from app.db import engine

    enable(session, "openmhz")
    worker.cycle()
    # After the watermark the cycle just stamped, and a second apart so each has its own file.
    base = datetime.now(UTC) + timedelta(seconds=1)
    live = [
        seed_call(session, root=tmp_path, priority="live", started_at=base + timedelta(seconds=i))
        for i in range(4)
    ]
    skipped = seed_call(
        session, root=tmp_path, priority="live", started_at=base + timedelta(seconds=10), encrypted=True
    )
    mark = gate.watermark(session, "openmhz")
    inserts: list[str] = []

    def spy(_conn, _cursor, statement, *_args) -> None:  # noqa: ANN001
        if statement.lstrip().upper().startswith("INSERT INTO CALL_EXPORTS"):
            inserts.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        created = ledger.discover(session, service="openmhz", watermark=mark, settings=worker.settings)
        # …and a pass with nothing new sends no INSERT at all.
        again = ledger.discover(session, service="openmhz", watermark=mark, settings=worker.settings)
    finally:
        event.remove(engine, "before_cursor_execute", spy)

    assert (created, again) == (5, 0)
    assert len(inserts) == 1
    rows = {row.call_id: row for row in session.scalars(select(CallExport)).all()}
    assert {call.id for call in live} | {skipped.id} == set(rows)
    assert rows[skipped.id].status == "skipped"
    assert rows[skipped.id].skip_reason == "encrypted"
    assert {rows[call.id].status for call in live} == {"pending"}


def test_discover_inserts_a_long_pass_in_chunks(session, worker, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Past ``INSERT_CHUNK`` rows the pass is split, and every row still lands once."""
    from sqlalchemy import event

    from app.db import engine

    enable(session, "openmhz")
    worker.cycle()
    base = datetime.now(UTC) + timedelta(seconds=1)
    calls = [
        seed_call(session, root=tmp_path, priority="live", started_at=base + timedelta(seconds=i))
        for i in range(5)
    ]
    mark = gate.watermark(session, "openmhz")
    monkeypatch.setattr(ledger, "INSERT_CHUNK", 2)
    inserts: list[str] = []

    def spy(_conn, _cursor, statement, *_args) -> None:  # noqa: ANN001
        if statement.lstrip().upper().startswith("INSERT INTO CALL_EXPORTS"):
            inserts.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        created = ledger.discover(session, service="openmhz", watermark=mark, settings=worker.settings)
    finally:
        event.remove(engine, "before_cursor_execute", spy)

    assert created == 5
    assert len(inserts) == 3
    assert sorted(session.scalars(select(CallExport.call_id)).all()) == sorted(c.id for c in calls)


def test_a_second_row_for_the_same_call_and_service_is_refused(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The constraint itself, not just the code path that avoids tripping it."""
    from sqlalchemy.exc import IntegrityError

    call = seed_call(session, root=tmp_path, priority="live")
    session.add(CallExport(call_id=call.id, service="openmhz"))
    session.commit()
    session.add(CallExport(call_id=call.id, service="openmhz"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # …but the *other* service may of course have its own row.
    session.add(CallExport(call_id=call.id, service="broadcastify"))
    session.commit()
    assert len(session.scalars(select(CallExport).where(CallExport.call_id == call.id)).all()) == 2


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"encrypted": True}, "encrypted"),
        ({"with_talkgroup": False}, "no talkgroup"),
        ({"audio_state": "purged"}, "no audio"),
    ],
)
def test_unpublishable_calls_are_skipped(session, worker, tmp_path, kwargs, reason) -> None:  # noqa: ANN001
    """Encrypted, talkgroup-less and audio-less calls are recorded as skipped, not failed."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live", **kwargs)

    worker.cycle()

    assert worker.test_client.uploads == []
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row.status == "skipped"
    assert row.skip_reason == reason


# --------------------------------------------------------------------------------------
# one upload per transmission, not per call row
# --------------------------------------------------------------------------------------
#
# Two recorders record every call, so one transmission is two ``calls`` rows and
# reconciliation decides which of them is the primary — sometimes *after* the first has
# been published.  The ledger's UNIQUE (call_id, service) cannot see that: the promoted
# copy is a different row.  These are the two guards that can — discovery reading the
# duplicate group, and the swap moving the ledger with the primary.


def _demote(session, loser: Call, winner: Call) -> None:  # noqa: ANN001
    """Point ``loser`` at ``winner`` the way reconciliation's swap leaves the pair."""
    loser.duplicate_of = winner.id
    session.commit()


def test_the_other_copy_of_an_uploaded_call_is_not_discovered(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The production bug: copy A is uploaded, then A is demoted and B becomes primary.

    B has no ledger row of its own and satisfies ``duplicate_of IS NULL``, so discovery
    used to hand the same transmission to the service a second time.
    """
    enable(session, "openmhz")
    worker.cycle()
    first = seed_call(session, root=tmp_path, priority="live")
    worker.cycle()
    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [first.id]

    second = seed_call(session, root=tmp_path, priority="live")
    _demote(session, first, second)

    worker.cycle()

    # Still one upload, and the promoted copy carries the row that says why.
    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [first.id]
    row = session.scalar(select(CallExport).where(CallExport.call_id == second.id))
    assert (row.status, row.skip_reason) == ("skipped", f"already exported as call {first.id}")
    assert row.skip_reason.startswith(ledger.DUPLICATE_UPLOAD_REASON)
    # …and the skipped row is what keeps it from being reconsidered every three seconds.
    assert ledger.discover(
        session, service="openmhz", watermark=gate.watermark(session, "openmhz"), settings=worker.settings
    ) == 0


def test_a_duplicate_group_with_no_export_rows_is_discovered_normally(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The guard is about *published* copies, not about being one of two copies.

    A demoted copy that was never exported — the usual case, since reconciliation at ingest
    beats discovery to it — must leave the primary perfectly publishable.
    """
    enable(session, "openmhz")
    worker.cycle()
    primary = seed_call(session, root=tmp_path, priority="live")
    other = seed_call(session, root=tmp_path, priority="live")
    _demote(session, other, primary)

    worker.cycle()

    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [primary.id]
    row = session.scalar(select(CallExport).where(CallExport.call_id == primary.id))
    assert row.status == "uploaded"
    # The demoted copy gets nothing at all: ``duplicate_of IS NOT NULL`` keeps it out.
    assert session.scalar(select(CallExport).where(CallExport.call_id == other.id)) is None


def test_a_swap_repoints_a_pending_row_at_the_new_primary(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Nothing has been sent yet, and the winner is the better recording — so it goes."""
    from app.ingest.reconcile import make_primary

    enable(session, "openmhz")
    worker.cycle()
    first = seed_call(session, root=tmp_path, priority="live")
    ledger.discover(
        session, service="openmhz", watermark=gate.watermark(session, "openmhz"), settings=worker.settings
    )
    row_id = session.scalar(select(CallExport.id).where(CallExport.call_id == first.id))

    second = seed_call(session, root=tmp_path, priority="live")
    second.duplicate_of = first.id
    session.commit()
    make_primary(session, second, worker.settings)  # the operator forcing the swap
    session.commit()

    rows = session.scalars(select(CallExport)).all()
    assert len(rows) == 1
    assert (rows[0].id, rows[0].call_id, rows[0].status) == (row_id, second.id, "pending")

    # …and it is the new primary that gets uploaded, exactly once.
    worker.cycle()
    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [second.id]


def test_a_swap_leaves_an_uploaded_row_and_skips_the_new_primary(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The audio is already in their archive; nothing here can un-send it."""
    from app.ingest.reconcile import make_primary

    enable(session, "openmhz")
    worker.cycle()
    first = seed_call(session, root=tmp_path, priority="live")
    worker.cycle()

    second = seed_call(session, root=tmp_path, priority="live")
    second.duplicate_of = first.id
    session.commit()
    make_primary(session, second, worker.settings)
    session.commit()

    uploaded = session.scalar(select(CallExport).where(CallExport.call_id == first.id))
    assert (uploaded.status, uploaded.call_id) == ("uploaded", first.id)
    skipped = session.scalar(select(CallExport).where(CallExport.call_id == second.id))
    assert (skipped.status, skipped.skip_reason) == ("skipped", f"already exported as call {first.id}")

    worker.cycle()
    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [first.id]


def test_the_batch_pass_demoting_an_exported_primary_skips_the_winner(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The other direction: the newcomer loses, and *it* is the one that was published.

    The batch pass rules on two rows that were both primaries, so the copy being demoted
    may be the one with the ledger row (``newcomer_was_primary=True``).
    """
    from app.ingest.reconcile import MatchResult, reconcile_pair

    enable(session, "openmhz")
    worker.cycle()
    exported = seed_call(session, root=tmp_path, priority="live")
    worker.cycle()
    keeper = seed_call(session, root=tmp_path, priority="live")

    outcome = reconcile_pair(
        session,
        exported,
        keeper,
        worker.settings,
        match=MatchResult(True, "manual"),
        forced_winner=keeper,
        newcomer_was_primary=True,
    )
    session.commit()

    assert (outcome.primary_id, outcome.duplicate_id) == (keeper.id, exported.id)
    row = session.scalar(select(CallExport).where(CallExport.call_id == keeper.id))
    assert (row.status, row.skip_reason) == ("skipped", f"already exported as call {exported.id}")

    worker.cycle()
    assert [facts.call_id for facts, _ in worker.test_client.uploads] == [exported.id]


def test_failures_back_off_then_park(session, worker, tmp_path) -> None:  # noqa: ANN001
    """Attempts are counted, the retry is pushed out, and the row is parked at the cap."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")
    worker.test_client.error = ExportError("the service is down")

    worker.cycle()
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.next_attempt_at > datetime.now(UTC)
    assert "the service is down" in row.last_error

    # Fast-forward past the backoff twice more; export_max_attempts is 3.
    for expected in (2, 3):
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        worker.cycle()
        session.refresh(row)
        assert row.attempts == expected
    assert row.status == "failed"


def test_a_permanent_refusal_is_parked_at_once(session, worker, tmp_path) -> None:  # noqa: ANN001
    """A rejected key is not worth three attempts."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")
    worker.test_client.error = PermanentExportError("credentials refused")

    worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert (row.status, row.attempts) == ("failed", 1)


def test_a_skipcall_is_recorded_as_skipped(session, worker, tmp_path) -> None:  # noqa: ANN001
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")
    worker.test_client.error = SkipCall("openmhz already has this call")

    worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row.status == "skipped"
    assert row.skip_reason == "openmhz already has this call"


def test_ledger_summary(session, worker, tmp_path) -> None:  # noqa: ANN001
    enable(session, "openmhz")
    worker.cycle()
    seed_call(session, root=tmp_path, tgid=1701, priority="live")
    seed_call(session, root=tmp_path, tgid=1102, priority="live", encrypted=True)
    worker.cycle()

    assert ledger.summary(session)["openmhz"] == {"uploaded": 1, "skipped": 1}


# --------------------------------------------------------------------------------------
# observability
# --------------------------------------------------------------------------------------


def test_activity_records_the_upload_and_the_failure(session, worker, tmp_path) -> None:  # noqa: ANN001
    enable(session, "openmhz")
    worker.cycle()
    seed_call(session, root=tmp_path, tgid=1701, priority="live")
    worker.cycle()

    worker.test_client.error = PermanentExportError("nope")
    seed_call(session, root=tmp_path, tgid=1102, priority="live")
    worker.cycle()

    rows = session.scalars(
        select(ActivityEvent).where(ActivityEvent.source == "export").order_by(ActivityEvent.id)
    ).all()
    kinds = [(r.kind, r.level) for r in rows]
    assert ("export.uploaded", "info") in kinds
    assert ("export.failed", "error") in kinds
    uploaded = next(r for r in rows if r.kind == "export.uploaded")
    assert uploaded.meta["service"] == "openmhz"
    assert uploaded.meta["talkgroup"] == 1701


def test_export_source_is_a_known_activity_source() -> None:
    from app.activity import SOURCES

    assert "export" in SOURCES


# --------------------------------------------------------------------------------------
# the transcode
# --------------------------------------------------------------------------------------


def test_transcode_applies_the_normalize_filter(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """The export encode must carry the same ``-af`` the transcription path uses."""
    import subprocess

    from app.exporters import transcode as transcode_module
    from app.stt.normalize import ffmpeg_audio_filter

    seen: list[list[str]] = []

    # Resolve the filter *before* ffmpeg is stubbed out: the probe behind
    # ``ffmpeg_audio_filter`` is lru_cached, and a stubbed ``shutil.which`` would poison it
    # for the rest of the session.
    af = ffmpeg_audio_filter(Settings(_env_file=None, stt_normalize=True))
    assert af, "the test host's ffmpeg carries none of the normalization filters"

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
        # ``monkeypatch`` replaces ``subprocess.run`` for every module, not just this one —
        # ffmpeg's own filter probe (``ffmpeg -hide_banner -filters``) would otherwise be
        # answered by writing eight bytes to a file called ``-filters``.  Only the encode
        # is intercepted; anything else goes to the real thing.
        if not str(cmd[-1]).endswith(".m4a"):
            return real_run(cmd, **kwargs)
        seen.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"fake-m4a")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(transcode_module.subprocess, "run", fake_run)
    monkeypatch.setattr(transcode_module.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")

    data = transcode_module.transcode_to_m4a(source, af=af)

    assert data == b"fake-m4a"
    cmd = seen[0]
    assert "-af" in cmd
    assert cmd[cmd.index("-af") + 1] == af
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_the_worker_transcodes_with_the_normalize_filter(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """…and the worker is what passes it in — not just the helper's default.

    The worker's ``-af`` is the §5.2 filter **plus** the export loudness stage: the two
    compose, and a regression that dropped either one would show up here.
    """
    from app.db import SessionLocal
    from app.exporters import worker as worker_module
    from app.exporters.loudness import export_filter
    from app.stt.normalize import ffmpeg_audio_filter

    normalize = ffmpeg_audio_filter(export_settings)
    assert normalize, "the test host's ffmpeg carries none of the normalization filters"
    # The seeded call is longer than the short-clip threshold, so this is the loudnorm branch.
    expected = export_filter(normalize, export_settings)
    assert expected is not None and expected.startswith(normalize)
    assert expected.endswith(",loudnorm=I=-11:TP=-3.0:LRA=11,alimiter=limit=0.707946:level=disabled:attack=5:release=50")
    seen: list[str | None] = []

    def fake_transcode(path, *, af=None, timeout_s=0.0):  # noqa: ANN001, ANN202, ARG001
        seen.append(af)
        return b"m4a"

    monkeypatch.setattr(worker_module, "transcode_to_m4a", fake_transcode)
    client = RecordingClient()
    built = ExportWorker(
        settings=export_settings, session_factory=SessionLocal, clients=[client], audio_root=tmp_path
    )
    enable(session, "openmhz")
    built.cycle()
    seed_call(session, root=tmp_path, priority="live")
    built.cycle()

    assert seen == [expected], "the exported audio must be loudness-normalized"


def test_a_real_transcode_produces_an_m4a(tmp_path, fixture_audio) -> None:  # noqa: ANN001
    """End to end through the actual ffmpeg on this host, filter included."""
    from app.exporters.transcode import transcode_to_m4a
    from app.stt.normalize import ffmpeg_audio_filter

    sources = sorted(Path(fixture_audio).rglob("*.mp3"))
    if not sources:
        pytest.skip("no fixture recordings")
    data = transcode_to_m4a(sources[0], af=ffmpeg_audio_filter(Settings(_env_file=None)))
    # ftyp box at offset 4 — this really is an MP4 container.
    assert data[4:8] == b"ftyp"


# --------------------------------------------------------------------------------------
# the export loudness stage
# --------------------------------------------------------------------------------------
#
# The stored file sits at ``audio_target_lufs`` (-16), which is where this app's player and
# its playback compressor want it and is too quiet for a service that has no compressor
# behind it.  ``export_target_lufs`` is the second number, applied at export time on top of
# the stored file; these tests are about the ``-af`` it composes.

LOUDNORM_11 = "loudnorm=I=-11:TP=-3.0:LRA=11"
LIMITER = "alimiter=limit=0.707946:level=disabled:attack=5:release=50"


def _lufs(value: float | int | None, **kw) -> Settings:  # noqa: ANN003
    return Settings(_env_file=None, export_target_lufs=value, **kw)


def test_the_export_filter_composes_with_the_normalize_filter() -> None:
    """An unprocessed original keeps its §5.2 filter and gains the export stage after it."""
    from app.exporters.loudness import export_filter

    got = export_filter("speechnorm=e=12.5:r=0.0001:l=1", _lufs(-11.0))

    assert got == f"speechnorm=e=12.5:r=0.0001:l=1,{LOUDNORM_11},{LIMITER}"


def test_the_export_filter_on_a_processed_source_is_the_stage_alone() -> None:
    """The processed copy is already even, so there is no base to compose with."""
    from app.exporters.loudness import export_filter

    assert export_filter(None, _lufs(-11.0)) == f"{LOUDNORM_11},{LIMITER}"


#: what the §5.5 pass stored for the tone-only call — a 2 s console tone and no speech
TONE_ONLY_META = {
    "head_trim_s": 0.12,
    "tone_removed": False,
    "tone_pattern": "single",
    "tone_spans": [
        {"start_s": 0.168, "end_s": 2.248, "hz": 993.0, "pattern": "single", "attenuate_db": 30.0}
    ],
}


def test_the_tone_envelope_goes_back_on_after_the_export_lift() -> None:
    """The tone-only call left for OpenMHz louder in its tone than the file we stored.

    It is 3.38 s of 993 Hz at -18 dBFS and nothing else.  The §5.5 pass took the span down to
    about -33 dB in the processed copy — and then the export's loudness stage measured that
    copy, found nothing in it but the attenuated tone, and lifted the whole clip to the -11
    LUFS target.  Re-applying the envelope after the lift is what stops that.
    """
    from app.exporters.loudness import export_filter

    got = export_filter(None, _lufs(-11.0), audio_meta=TONE_ONLY_META, processed=True)

    # …after the loudness stage and before the limiter, which is the one promise that has to
    # come last, and shifted by the head trim into the processed copy's own timebase.
    assert got == f"{LOUDNORM_11},volume=volume=-30dB:enable='between(t,0.048,2.128)',{LIMITER}"


def test_the_envelope_uses_the_page_depth_for_a_page() -> None:
    """A page comes down 18 dB on the export exactly as it did in the stored file.

    Twice over: the depth the pass recorded on the span is preferred, and a span from
    before that was recorded falls back to the setting for its pattern.
    """
    from app.exporters.loudness import export_filter

    settings = _lufs(-11.0, audio_tone_attenuate_db=30.0, audio_page_tone_attenuate_db=18.0)
    stored = {
        "head_trim_s": 0.0,
        "tone_spans": [
            {"start_s": 1.0, "end_s": 3.0, "hz": 687.5, "pattern": "two_tone", "attenuate_db": 18.0}
        ],
    }
    assert "volume=volume=-18dB:enable='between(t,1,3)'" in export_filter(
        None, settings, audio_meta=stored, processed=True
    )

    older = {
        "head_trim_s": 0.0,
        "tone_spans": [{"start_s": 1.0, "end_s": 3.0, "hz": 687.5, "pattern": "two_tone"}],
    }
    assert "volume=volume=-18dB:enable='between(t,1,3)'" in export_filter(
        None, settings, audio_meta=older, processed=True
    )

    # …and a `single` from the same era gets the deeper one.
    lone = {"head_trim_s": 0.0, "tone_spans": [{"start_s": 1.0, "end_s": 3.0, "pattern": "single"}]}
    assert "volume=volume=-30dB:" in export_filter(
        None, settings, audio_meta=lone, processed=True
    )


def test_an_unprocessed_original_keeps_the_spans_where_they_were_measured() -> None:
    """Only the *processed* copy has the head trim taken off its front."""
    from app.exporters.loudness import export_filter

    got = export_filter(None, _lufs(-11.0), audio_meta=TONE_ONLY_META, processed=False)
    assert "between(t,0.168,2.248)" in got


def test_no_envelope_when_there_is_nothing_to_put_back() -> None:
    """Removed tones, a call with no pass, and a deployment that does not attenuate."""
    from app.exporters.loudness import export_filter

    plain = f"{LOUDNORM_11},{LIMITER}"
    assert export_filter(None, _lufs(-11.0), audio_meta=None, processed=True) == plain
    assert export_filter(None, _lufs(-11.0), audio_meta={}, processed=True) == plain
    removed = {**TONE_ONLY_META, "tone_removed": True}
    assert export_filter(None, _lufs(-11.0), audio_meta=removed, processed=True) == plain
    keeping = _lufs(-11.0, audio_tone_handling="keep")
    assert export_filter(None, keeping, audio_meta=TONE_ONLY_META, processed=True) == plain


def test_the_export_target_is_the_setting_not_a_constant() -> None:
    from app.exporters.loudness import export_filter

    assert export_filter(None, _lufs(-9.5)) == f"loudnorm=I=-9.5:TP=-3.0:LRA=11,{LIMITER}"


def test_a_short_clip_takes_the_measured_gain_branch(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """Under three seconds ``loudnorm`` cannot integrate; a measured ``volume=`` runs."""
    from app.exporters import loudness

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")
    # A peak far enough down that it does not bind: the mean is what sets the gain here.
    monkeypatch.setattr(
        loudness, "measured_levels", lambda *_a, **_k: loudness.MeasuredLevels(-24.0, -40.0)
    )

    got = loudness.export_filter("speechnorm=x", _lufs(-11.0), path=source, duration_s=2.2)

    # -11 wanted, -24 measured: a 13 dB lift, and -40 + 13 is still well under the ceiling.
    assert got == f"speechnorm=x,volume=13dB,{LIMITER}"


def test_the_measured_gain_stops_at_the_peak_ceiling(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """A quiet clip with one sharp transient must not be lifted onto the limiter.

    The mean says "13 dB quiet"; the peak says there is already a sample at -6 dBFS in
    there — a mic click, or the front of a page tone. Taking the whole 13 dB would put that
    sample at +7 and hand the limiter 10 dB to clamp off a single transient, which is
    audible as a dulled, pumping clip. The gain stops where the peak reaches the ceiling
    instead, and the limiter is left with nothing to do.
    """
    from app.exporters import loudness

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")
    monkeypatch.setattr(
        loudness, "measured_levels", lambda *_a, **_k: loudness.MeasuredLevels(-24.0, -6.0)
    )

    got = loudness.export_filter(None, _lufs(-11.0), path=source, duration_s=2.2)

    # -3 dB ceiling, -6 dB peak: 3 dB, not 13.
    assert got == f"volume=3dB,{LIMITER}"
    assert loudness.EXPORT_TRUE_PEAK_DB == -3.0


def test_the_peak_cap_never_raises_the_gain(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """It is a cap, not a target: a clip already too loud is still turned *down*."""
    from app.exporters import loudness

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")
    # Mean -5 against a -11 target is a 6 dB cut; the peak at -30 would "allow" +27.
    monkeypatch.setattr(
        loudness, "measured_levels", lambda *_a, **_k: loudness.MeasuredLevels(-5.0, -30.0)
    )

    assert loudness.export_filter(
        None, _lufs(-11.0), path=source, duration_s=2.2
    ) == f"volume=-6dB,{LIMITER}"


def test_an_unmeasurable_peak_leaves_the_limiter_as_the_net(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """``volumedetect`` printed a mean and no max: the old behaviour, gain uncapped."""
    from app.exporters import loudness

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")
    monkeypatch.setattr(
        loudness, "measured_levels", lambda *_a, **_k: loudness.MeasuredLevels(-24.0, None)
    )

    got = loudness.export_filter(None, _lufs(-11.0), path=source, duration_s=2.2)
    assert got == f"volume=13dB,{LIMITER}"


def test_the_measured_gain_is_capped_by_the_max_gain_setting(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """A near-silent short clip must not be amplified into a wall of hiss."""
    from app.exporters import loudness

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")
    monkeypatch.setattr(
        loudness, "measured_levels", lambda *_a, **_k: loudness.MeasuredLevels(-80.0, -70.0)
    )

    got = loudness.export_filter(
        None, _lufs(-11.0, audio_max_gain_db=30.0), path=source, duration_s=1.0
    )

    assert got == f"volume=30dB,{LIMITER}"


def test_a_short_clip_that_cannot_be_measured_falls_back_to_loudnorm(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """An under-correcting loudnorm still beats sending the call at the stored level."""
    from app.exporters import loudness

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")
    monkeypatch.setattr(loudness, "measured_levels", lambda *_a, **_k: None)

    got = loudness.export_filter(None, _lufs(-11.0), path=source, duration_s=1.0)

    assert got == f"{LOUDNORM_11},{LIMITER}"


def test_a_long_clip_is_never_measured(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """The measuring pass is the short branch's cost only — it must not be paid per upload."""
    from app.exporters import loudness

    source = tmp_path / "call.mp3"
    source.write_bytes(b"\xff\xfb\x90d")
    calls: list[object] = []

    def spy(path, **_kw):  # noqa: ANN001, ANN202
        calls.append(path)
        return loudness.MeasuredLevels(-30.0, -12.0)

    monkeypatch.setattr(loudness, "measured_levels", spy)

    got = loudness.export_filter(None, _lufs(-11.0), path=source, duration_s=8.0)

    assert calls == []
    assert got == f"{LOUDNORM_11},{LIMITER}"


@pytest.mark.parametrize("value", [0, 0.0])
def test_zero_disables_the_export_stage(value) -> None:  # noqa: ANN001
    """Off means the exports go out exactly as they did before the setting existed."""
    from app.exporters.loudness import export_filter

    assert export_filter("speechnorm=x", _lufs(value)) == "speechnorm=x"
    assert export_filter(None, _lufs(value)) is None


@pytest.mark.parametrize("value", [None, "", "not a number"])
def test_an_unusable_target_costs_the_gain_not_the_upload(value) -> None:  # noqa: ANN001
    """A settings object without a readable target must not raise on the export path."""
    from types import SimpleNamespace

    from app.exporters.loudness import export_filter

    assert export_filter("speechnorm=x", SimpleNamespace(export_target_lufs=value)) == "speechnorm=x"


def test_the_mean_is_read_out_of_volumedetect(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """The measurement is one ffmpeg pass, and the number comes off its stderr."""
    import subprocess

    from app.exporters import loudness

    stderr = (
        b"[Parsed_volumedetect_0 @ 0x1] mean_volume: -27.3 dB\n"
        b"[Parsed_volumedetect_0 @ 0x1] max_volume: -6.1 dB\n"
    )
    monkeypatch.setattr(loudness.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        loudness.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, b"", stderr),
    )

    assert loudness.measured_mean_db(tmp_path / "call.mp3") == -27.3
    # Both numbers come off the one pass, which is what makes the peak free.
    assert loudness.measured_levels(tmp_path / "call.mp3") == loudness.MeasuredLevels(-27.3, -6.1)


def test_a_volumedetect_without_a_max_still_measures(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """No ``max_volume`` line is a missing cap, not a failed measurement."""
    import subprocess

    from app.exporters import loudness

    monkeypatch.setattr(loudness.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        loudness.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, b"", b"mean_volume: -27.3 dB\n"),
    )

    assert loudness.measured_levels(tmp_path / "call.mp3") == loudness.MeasuredLevels(-27.3, None)


def test_digital_silence_measures_as_unusable(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    import subprocess

    from app.exporters import loudness

    stderr = b"mean_volume: -91.0 dB\n"
    monkeypatch.setattr(loudness.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        loudness.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, b"", stderr),
    )

    assert loudness.measured_mean_db(tmp_path / "call.mp3") is None


def test_the_uploaded_row_records_the_filter_that_was_sent(
    session, worker, export_settings, tmp_path
) -> None:  # noqa: ANN001, ARG001
    """An operator reading the ledger's activity row can see the export gain applied."""
    from app.exporters.loudness import export_filter
    from app.stt.normalize import ffmpeg_audio_filter

    enable(session, "openmhz")
    worker.cycle()
    seed_call(session, root=tmp_path, priority="live")
    worker.cycle()

    row = session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "export.uploaded")
    ).all()[-1]
    expected = export_filter(ffmpeg_audio_filter(export_settings), export_settings)
    assert row.meta["filter"] == expected
    assert LIMITER in row.meta["filter"]


# --------------------------------------------------------------------------------------
# request shapes — Broadcastify Calls
# --------------------------------------------------------------------------------------


def facts(**kw) -> CallFacts:  # noqa: ANN003
    base = {
        "call_id": 1,
        "talkgroup": 13012,
        "radio_id": 1234567,
        "started_at": datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
        "duration_s": 11.4,
        "frequency_hz": 851_012_500,
        "emergency": False,
    }
    base.update(kw)
    return CallFacts(**base)


def test_broadcastify_metadata_fields(export_settings) -> None:  # noqa: ANN001
    """The documented stage-1 form: apiKey/systemId/callDuration/ts/tg/src/freq/enc."""
    fields = metadata_fields(facts(), export_settings)

    assert fields["apiKey"] == "test-bcfy-key"
    assert fields["systemId"] == "34"
    assert fields["callDuration"] == "11.4"
    assert fields["ts"] == str(int(datetime(2026, 9, 1, 12, 0, tzinfo=UTC).timestamp()))
    assert fields["tg"] == "13012"
    assert fields["src"] == "1234567"
    # MHz here, Hz for OpenMHz — the single easiest field to cross-wire.
    assert fields["freq"] == "851.0125"
    assert fields["enc"] == "m4a"


def test_broadcastify_formats_a_patch_and_an_unresolved_unit(export_settings) -> None:  # noqa: ANN001
    """Patch 13012 over 13013 and 13014, recorded on 13013: ``13012,13013,13014``.

    The patch id **leads**, per the documentation: "patched calls: comma-delimited list of
    all talkgroups in the patch with the patch ID listed first". keyup files a patched call
    under its *first member* (``app/ingest/parser.py``), so ``talkgroup`` here is 13013 —
    a member — and the patch id has to come from ``patch_id`` or the wrong number leads.
    """
    fields = metadata_fields(
        facts(talkgroup=13013, patch_id=13012, patched=[13014], radio_id=None), export_settings
    )
    assert fields["tg"] == "13012,13013,13014"
    assert fields["src"] == "0"


def test_broadcastify_puts_the_patch_id_first_not_a_member() -> None:
    """The regression this whole field exists for."""
    patched = facts(talkgroup=13013, patch_id=13012, patched=[13014])
    assert format_talkgroup(patched) == "13012,13013,13014"
    assert format_talkgroup(patched).split(",")[0] == "13012"
    # An unpatched call is still just its talkgroup, with no leading comma or stray 0.
    assert format_talkgroup(facts()) == "13012"


def test_broadcastify_never_repeats_a_talkgroup_in_a_patch() -> None:
    """A patch id that is also a member is sent once, still first."""
    assert format_talkgroup(facts(talkgroup=13012, patch_id=13012, patched=[13013])) == "13012,13013"


def test_facts_carry_the_patch_id_from_the_patches_row(session, tmp_path) -> None:  # noqa: ANN001
    """``patches.patch_id`` — the supergroup dispatch announced — not the row's primary key."""
    from app.models import Patch

    call = seed_call(session, root=tmp_path, tgid=13013)
    patch = Patch(system_id=call.system_id, patch_id=13012, member_tgids=[13013, 13014])
    session.add(patch)
    session.flush()
    call.patch_id = patch.id
    session.commit()

    resolved = facts_for(session, call)
    assert resolved is not None
    assert resolved.patch_id == 13012
    assert patch.id != 13012  # the two are never the same number
    assert format_talkgroup(resolved) == "13012,13013,13014"


# --------------------------------------------------------------------------------------
# encoding: enc, the ffmpeg flags and the stage-2 Content-Type all follow one setting
# --------------------------------------------------------------------------------------


def test_encoding_defaults_to_m4a(export_settings) -> None:  # noqa: ANN001
    assert encoding_for(export_settings) == "m4a"
    assert content_type_for(export_settings) == "audio/aac"
    assert metadata_fields(facts(), export_settings)["enc"] == "m4a"


def test_choosing_mp3_changes_enc_and_the_upload_content_type(export_settings) -> None:  # noqa: ANN001
    """ "Content-Type ``audio/aac`` for M4A/AAC, ``audio/mpeg`` for MP3" — one setting, both."""
    mp3 = export_settings.model_copy(update={"broadcastify_encoding": "mp3"})
    assert metadata_fields(facts(), mp3)["enc"] == "mp3"
    assert content_type_for(mp3) == "audio/mpeg"

    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("call-upload"):
            seen["post"] = request
            return httpx.Response(200, text="0 https://upload.example.test/one-time/mp3")
        seen["put"] = request
        return httpx.Response(200)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    BroadcastifyClient(mp3, client=http).upload(facts(), b"MP3-BYTES")

    assert _parts(seen["post"])["enc"] == "mp3"
    assert seen["put"].headers["content-type"] == "audio/mpeg"


def test_an_unreadable_encoding_reads_as_m4a(export_settings) -> None:  # noqa: ANN001
    odd = export_settings.model_copy(update={"broadcastify_encoding": "opus"})
    assert encoding_for(odd) == "m4a"
    assert content_type_for(odd) == "audio/aac"


def test_mp3_bitrate_is_one_of_the_two_the_spec_lists(export_settings) -> None:  # noqa: ANN001
    assert mp3_bitrate_kbps(export_settings) == 32
    assert mp3_bitrate_kbps(export_settings.model_copy(update={"broadcastify_mp3_bitrate_kbps": 16})) == 16
    # 64 is not on Broadcastify's list; it reads as the default rather than being sent.
    assert mp3_bitrate_kbps(export_settings.model_copy(update={"broadcastify_mp3_bitrate_kbps": 64})) == 32


def test_the_worker_encodes_broadcastify_the_way_the_setting_says(export_settings, tmp_path) -> None:  # noqa: ANN001
    """The one place the encoding choice becomes ffmpeg arguments."""
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    calls: list[tuple[str, dict]] = []
    worker_module.transcode_to_m4a = lambda path, **kw: (calls.append(("m4a", kw)), b"AAC")[1]  # type: ignore[assignment]
    worker_module.transcode_to_mp3 = lambda path, **kw: (calls.append(("mp3", kw)), b"MP3")[1]  # type: ignore[assignment]
    try:
        built = ExportWorker(settings=export_settings, session_factory=SessionLocal, audio_root=tmp_path)
        source = tmp_path / "x.mp3"
        source.write_bytes(b"\xff\xfb")

        assert built._audio_for("broadcastify", source, af=None, settings=export_settings) == b"AAC"
        mp3 = export_settings.model_copy(update={"broadcastify_encoding": "mp3"})
        assert built._audio_for("broadcastify", source, af=None, settings=mp3) == b"MP3"
        # OpenMHz is untouched by a Broadcastify setting.
        assert built._audio_for("openmhz", source, af=None, settings=mp3) == b"AAC"
    finally:
        importlib.reload(worker_module)

    assert [kind for kind, _ in calls] == ["m4a", "mp3", "m4a"]
    # CBR mono 22050 Hz at the chosen bitrate, per the spec's MP3 line.
    assert calls[1][1]["sample_rate"] == 22_050
    assert calls[1][1]["bitrate_kbps"] == 32


def test_the_aac_encode_carries_the_18khz_cutoff(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """ "approximately 32 kb/s with an 18 kHz cutoff using libfdk_aac or equivalent"."""
    from app.exporters import transcode

    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN202, ARG001
        seen["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"M4A")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(transcode, "aac_encoder", lambda **kw: "libfdk_aac")
    monkeypatch.setattr(transcode.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(transcode.subprocess, "run", fake_run)
    source = tmp_path / "in.mp3"
    source.write_bytes(b"\xff\xfb")

    assert transcode.transcode_to_m4a(source) == b"M4A"
    cmd = seen["cmd"]
    assert cmd[cmd.index("-cutoff") + 1] == "18000"
    assert cmd[cmd.index("-c:a") + 1] == "libfdk_aac"
    assert cmd[cmd.index("-b:a") + 1] == "32k"
    assert cmd[cmd.index("-ac") + 1] == "1"


def test_the_aac_encoder_probe_falls_back_when_libfdk_is_absent(monkeypatch) -> None:  # noqa: ANN001
    """A distribution ffmpeg has no libfdk_aac; the built-in ``aac`` is the "or equivalent"."""
    from app.exporters import transcode

    monkeypatch.setattr(transcode, "_aac_encoder", None)
    monkeypatch.setattr(transcode.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        transcode.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"aac  AAC (Advanced Audio)\n", b""),
    )
    assert transcode.aac_encoder() == "aac"

    monkeypatch.setattr(transcode, "_aac_encoder", None)
    monkeypatch.setattr(
        transcode.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b" A....D libfdk_aac  Fraunhofer\n", b""),
    )
    assert transcode.aac_encoder() == "libfdk_aac"
    # Probed once: a second call must not spawn ffmpeg again.
    monkeypatch.setattr(transcode.subprocess, "run", _explode)
    assert transcode.aac_encoder() == "libfdk_aac"


def _explode(*a, **kw):  # noqa: ANN002, ANN003, ANN202, ARG001
    raise AssertionError("the encoder probe should have been cached")


def test_the_calls_mp3_is_cbr_mono_22050(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """ "32 kb/s mono 22050 Hz CBR, or 16 kb/s mono 22050 Hz CBR"."""
    from app.exporters import transcode

    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN202, ARG001
        seen["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"MP3")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(transcode.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(transcode.subprocess, "run", fake_run)
    source = tmp_path / "in.mp3"
    source.write_bytes(b"\xff\xfb")

    transcode.transcode_to_mp3(source, bitrate_kbps=16, sample_rate=22_050, xing=True)
    cmd = seen["cmd"]
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert cmd[cmd.index("-ar") + 1] == "22050"
    assert cmd[cmd.index("-b:a") + 1] == "16k"
    # -b:a with no -q:a *is* libmp3lame's CBR mode.
    assert "-q:a" not in cmd
    # A standalone file wants the Xing frame; the feed's bitstream must not have one.
    assert cmd[cmd.index("-write_xing") + 1] == "1"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("0 https://upload.example.test/path/key", "https://upload.example.test/path/key"),
        ("0 https://upload.example.test/path/key\r\n", "https://upload.example.test/path/key"),
    ],
)
def test_broadcastify_parses_a_success_line(body, expected) -> None:  # noqa: ANN001
    """…and strips it, which neither reference client does."""
    assert parse_stage1(body) == expected


@pytest.mark.parametrize(
    ("body", "exc"),
    [
        ("1 SKIPPED---ALREADY-RECEIVED-THIS-CALL", SkipCall),
        ("1 REJECTED-SOMETHING", PermanentExportError),
        ("1 Invalid-API-Key", PermanentExportError),
        ("1 API-Key-Access-Denied", PermanentExportError),
        ("100 NO-API-KEY-SPECIFIED", PermanentExportError),
        ("7 SOMETHING-TRANSIENT", ExportError),
        ("", ExportError),
    ],
)
def test_broadcastify_classifies_the_error_lines(body, exc) -> None:  # noqa: ANN001
    with pytest.raises(exc):
        parse_stage1(body)


def test_broadcastify_two_stage_upload_over_a_mocked_transport(export_settings) -> None:  # noqa: ANN001
    """The golden shape: a multipart metadata POST, then a raw PUT of the audio."""
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/call-upload":
            seen["post"] = request
            return httpx.Response(200, text="0 https://upload.example.test/one-time/abc123\n")
        seen["put"] = request
        return httpx.Response(200)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = BroadcastifyClient(export_settings, client=http).upload(facts(), b"AUDIO-BYTES")

    post = seen["post"]
    assert post.method == "POST"
    assert post.headers["content-type"].startswith("multipart/form-data")
    body = post.content.decode("latin-1")
    for name, value in (
        ("apiKey", "test-bcfy-key"),
        ("systemId", "34"),
        ("ts", "1788264000"),
        ("tg", "13012"),
        ("src", "1234567"),
        ("freq", "851.0125"),
        ("enc", "m4a"),
    ):
        assert f'name="{name}"' in body
        assert value in body

    put = seen["put"]
    assert put.method == "PUT"
    assert str(put.url) == "https://upload.example.test/one-time/abc123"
    assert put.headers["content-type"] == "audio/aac"
    assert put.headers["content-length"] == str(len(b"AUDIO-BYTES"))
    assert "transfer-encoding" not in put.headers
    assert put.content == b"AUDIO-BYTES"
    assert result.remote_id == "abc123"


def test_broadcastify_is_unconfigured_without_a_system_id(export_settings) -> None:  # noqa: ANN001
    assert BroadcastifyClient(export_settings).configured() is True
    bare = export_settings.model_copy(update={"broadcastify_system_id": ""})
    assert BroadcastifyClient(bare).configured() is False


# --------------------------------------------------------------------------------------
# request shapes — OpenMHz
# --------------------------------------------------------------------------------------


def test_openmhz_form_fields(export_settings) -> None:  # noqa: ANN001
    fields = form_fields(facts(), export_settings)

    assert fields["api_key"] == "test-omhz-key"
    assert fields["talkgroup_num"] == "13012"
    # Hz, not MHz.
    assert fields["freq"] == "851012500"
    assert fields["start_time"] == "1788264000"
    assert fields["stop_time"] == "1788264011"
    assert fields["call_length"] == "11"
    assert fields["emergency"] == "0"
    assert json.loads(fields["source_list"]) == [{"pos": 0.0, "src": 1234567}]
    # No patch → the field is omitted entirely; "[]" makes the server store a NaN.
    assert "patch_list" not in fields
    # freq_list was removed after trunk-recorder 4.4.0 and the current server ignores it.
    assert "freq_list" not in fields


def test_openmhz_patch_list_is_bracketed_csv_not_json(export_settings) -> None:  # noqa: ANN001
    """``patch_list``, not ``patches``; and the server splits it on commas, not JSON.parse."""
    fields = form_fields(facts(patched=[13013, 13014]), export_settings)
    assert fields["patch_list"] == "[13012,13013,13014]"
    assert "patches" not in fields


def test_openmhz_source_list_is_empty_when_the_unit_is_unresolved() -> None:
    assert source_list(facts(radio_id=None)) == "[]"


def test_openmhz_upload_over_a_mocked_transport(export_settings) -> None:  # noqa: ANN001
    """The golden shape: one multipart POST whose file part is named ``call``."""
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["post"] = request
        return httpx.Response(200, text="")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    OpenMhzClient(export_settings, client=http).upload(facts(emergency=True), b"AUDIO")

    post = seen["post"]
    assert post.method == "POST"
    assert str(post.url) == "https://api.openmhz.test/examplesys/upload"
    body = post.content.decode("latin-1")
    # multer's upload.single('call') — the file part MUST be named exactly this
    assert 'name="call"' in body
    # …and the extension is load-bearing: anything else is a hard 500.
    assert '.m4a"' in body
    assert "AUDIO" in body
    assert 'name="api_key"' in body
    assert 'name="talkgroup_num"' in body
    assert 'name="source_list"' in body
    assert "851012500" in body
    assert 'name="emergency"' in body


@pytest.mark.parametrize(
    ("body", "exc"),
    [
        ("API Keys do not match!\n", PermanentExportError),
        ("ShortName does not exist: nope\n", PermanentExportError),
        ("Error, invalid filename:\n", PermanentExportError),
        ("Talkgroup does not exist, skipping.\n", SkipCall),
        ("Error parsing sourcelist boom", ExportError),
    ],
)
def test_openmhz_classifies_the_server_errors(export_settings, body, exc) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(500, text=body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(exc):
        OpenMhzClient(export_settings, client=http).upload(facts(), b"AUDIO")


def test_openmhz_is_unconfigured_without_a_short_name(export_settings) -> None:  # noqa: ANN001
    assert OpenMhzClient(export_settings).configured() is True
    assert OpenMhzClient(export_settings.model_copy(update={"openmhz_short_name": ""})).configured() is False


# --------------------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------------------


def test_facts_for_resolves_the_talkgroup_and_unit(session, tmp_path) -> None:  # noqa: ANN001
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    resolved = facts_for(session, call)
    assert resolved is not None
    assert resolved.talkgroup == 1701  # the tgid, not the row id
    assert resolved.radio_id == 5250057
    assert resolved.frequency_hz == 851_012_500


def test_facts_for_refuses_a_call_with_no_talkgroup(session, tmp_path) -> None:  # noqa: ANN001
    call = seed_call(session, root=tmp_path, with_talkgroup=False, priority="live")
    assert facts_for(session, call) is None


# --------------------------------------------------------------------------------------
# the source alias — the unit's, never the talkgroup's
# --------------------------------------------------------------------------------------


def test_the_source_alias_is_the_radios_not_the_talkgroups(session, tmp_path) -> None:  # noqa: ANN001
    """Both services' source-alias fields mean the *transmitting unit*.

    Sending the talkgroup name would label every call on the channel as coming from a unit
    of that name, which is wrong on the wire and wrong on their site.
    """
    call = seed_call(session, root=tmp_path, priority="live", talker_alias="MEDIC 12")
    resolved = facts_for(session, call)

    assert resolved.radio_alias == "MEDIC 12"
    # the talkgroup is called "EXAMPLE EVENT 1" — it must not leak into the alias
    assert resolved.radio_alias != "EXAMPLE EVENT 1"


def test_the_directory_alias_is_the_fallback_for_the_source_alias(session, tmp_path) -> None:  # noqa: ANN001
    """No over-the-air talker alias → the alias an operator gave that unit."""
    call = seed_call(session, root=tmp_path, priority="live", radio_alias="ENGINE 1")
    assert facts_for(session, call).radio_alias == "ENGINE 1"


def test_the_talker_alias_wins_over_the_directory_alias(session, tmp_path) -> None:  # noqa: ANN001
    call = seed_call(session, root=tmp_path, priority="live", talker_alias="MEDIC 12", radio_alias="ENGINE 1")
    assert facts_for(session, call).radio_alias == "MEDIC 12"


def test_no_alias_anywhere_leaves_it_unset(session, tmp_path) -> None:  # noqa: ANN001
    """The common case: nothing announced a talker alias and nobody named the unit."""
    call = seed_call(session, root=tmp_path, priority="live")
    assert facts_for(session, call).radio_alias is None


def test_broadcastify_omits_the_source_alias_when_there_is_none(export_settings) -> None:  # noqa: ANN001
    assert "srcId_alias" not in metadata_fields(facts(), export_settings)
    with_alias = metadata_fields(facts(radio_alias="MEDIC 12"), export_settings)
    assert with_alias["srcId_alias"] == "MEDIC 12"


def test_openmhz_omits_the_source_tag_when_there_is_none() -> None:
    assert json.loads(source_list(facts())) == [{"pos": 0.0, "src": 1234567}]
    assert json.loads(source_list(facts(radio_alias="MEDIC 12"))) == [
        {"pos": 0.0, "src": 1234567, "tag": "MEDIC 12"}
    ]


# --------------------------------------------------------------------------------------
# the ledger's remote_id
# --------------------------------------------------------------------------------------


def test_the_presigning_query_string_is_not_kept_in_the_ledger(export_settings) -> None:  # noqa: ANN001
    """The one-time URL's query carries a signature and an expiry — not ledger material."""
    signed = "https://upload.example.test/one-time/abc123?X-Amz-Signature=deadbeefcafe&X-Amz-Expires=300"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/call-upload":
            return httpx.Response(200, text=f"0 {signed}\n")
        return httpx.Response(200)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = BroadcastifyClient(export_settings, client=http).upload(facts(), b"AUDIO")

    assert result.remote_id == "abc123"
    # …and the same goes for the stored response line, which carries the segment too.
    assert result.response == "0 abc123"
    assert result.response_code == 0
    assert "X-Amz-Signature" not in f"{result.remote_id} {result.response}"


# --------------------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------------------


def test_uploads_to_one_service_are_spaced_by_the_minimum_interval(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """``EXPORT_MIN_INTERVAL_S`` is the gentleness knob: we are a guest on their server."""
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a")
    slept: list[float] = []
    paced = export_settings.model_copy(update={"export_min_interval_s": 5.0})
    client = RecordingClient()
    built = ExportWorker(
        settings=paced,
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
        sleep=slept.append,
    )
    enable(session, "openmhz")
    built.cycle()
    for tgid in (1701, 1102, 1103):
        seed_call(session, root=tmp_path, tgid=tgid, priority="live")

    built.cycle()

    assert len(client.uploads) == 3
    # The first upload of the cycle waits for nothing; each one after it is spaced.
    assert len(slept) == 2
    assert all(0 < wait <= 5.0 for wait in slept), slept


def test_the_first_upload_to_each_service_is_not_delayed(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """The interval is per service — one being busy must not slow the other down."""
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a")
    slept: list[float] = []
    paced = export_settings.model_copy(update={"export_min_interval_s": 30.0})
    clients = [RecordingClient("broadcastify"), RecordingClient("openmhz")]
    built = ExportWorker(
        settings=paced,
        session_factory=SessionLocal,
        clients=clients,
        audio_root=tmp_path,
        sleep=slept.append,
    )
    enable(session, "broadcastify")
    enable(session, "openmhz")
    built.cycle()
    seed_call(session, root=tmp_path, priority="live")

    built.cycle()

    # One call, uploaded once to each service — two firsts, so nothing sleeps at all.
    assert [c.service for c in clients if c.uploads] == ["broadcastify", "openmhz"]
    assert slept == []


def test_a_zero_interval_disables_the_pacing(session, export_settings, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a")
    slept: list[float] = []
    client = RecordingClient()
    built = ExportWorker(
        settings=export_settings,  # export_min_interval_s = 0.0
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
        sleep=slept.append,
    )
    enable(session, "openmhz")
    built.cycle()
    for tgid in (1701, 1102):
        seed_call(session, root=tmp_path, tgid=tgid, priority="live")

    built.cycle()

    assert len(client.uploads) == 2
    assert slept == []


# --------------------------------------------------------------------------------------
# a settings-read failure must not look like "both switches are off"
# --------------------------------------------------------------------------------------


def test_a_settings_read_failure_skips_the_cycle_and_keeps_the_watermark(
    session, worker, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """The subtle one: falling back to the process defaults would erase the watermark.

    Both defaults are ``False``, so a hiccup would read as "disabled", clear the enable
    stamp, and the next healthy cycle would re-stamp it at *now* — silently dropping every
    call recorded during the gap.  The cycle is abandoned instead.
    """
    from app.exporters import worker as worker_module

    enable(session, "openmhz")
    worker.cycle()
    mark = gate.watermark(session, "openmhz")
    assert mark is not None

    healthy = worker_module.effective_settings

    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the settings table is unreachable")

    monkeypatch.setattr(worker_module, "effective_settings", boom)
    call = seed_call(session, root=tmp_path, priority="live")

    assert worker.cycle() == {}

    session.expire_all()
    assert gate.watermark(session, "openmhz") == mark, "the watermark must survive a hiccup"
    assert worker.test_client.uploads == []
    assert session.scalar(select(CallExport).where(CallExport.call_id == call.id)) is None

    # …and once the database is back, the call recorded during the gap still goes out.
    # Only the settings read is restored — undoing every patch would also drop the
    # fixture's transcode stub and send this through a real ffmpeg.
    monkeypatch.setattr(worker_module, "effective_settings", healthy)
    worker.cycle()
    assert [f.call_id for f, _ in worker.test_client.uploads] == [call.id]


# --------------------------------------------------------------------------------------
# the claim transaction
# --------------------------------------------------------------------------------------


class _RollbackSpy:
    """A session proxy that records when ``rollback`` is called relative to other work."""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.events: list[str] = []

    def rollback(self):  # noqa: ANN201
        self.events.append("rollback")
        return self._inner.rollback()

    def commit(self):  # noqa: ANN201
        self.events.append("commit")
        return self._inner.commit()

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._inner, name)


@pytest.mark.parametrize(
    ("error", "outcome"),
    [(ExportError("down"), "pending"), (SkipCall("already have it"), "skipped")],
)
def test_the_outcome_is_written_inside_the_claim_transaction(
    session, export_settings, tmp_path, monkeypatch, error, outcome
) -> None:  # noqa: ANN001
    """No rollback before the ledger write — it would drop the claim's FOR UPDATE lock.

    A rollback there leaves the row ``pending`` and still due with no lock on it, so a
    second worker process could claim the same call and attempt it a second time.
    """
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a")
    spies: list[_RollbackSpy] = []

    def spying_factory():  # noqa: ANN202
        spy = _RollbackSpy(SessionLocal())
        spies.append(spy)
        return spy

    client = RecordingClient(error=error)
    built = ExportWorker(
        settings=export_settings,
        session_factory=spying_factory,
        clients=[client],
        audio_root=tmp_path,
    )
    enable(session, "openmhz")
    built.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    # Mark the moment the outcome is written, so the assertion is about *ordering* rather
    # than about rollbacks never happening: the claim that finds an empty queue at the end
    # of the batch rolls back quite legitimately.
    for name in ("mark_failed", "mark_skipped"):
        original = getattr(worker_module.ledger, name)

        def marked(*a, _orig=original, **k):  # noqa: ANN002, ANN003, ANN202
            spies[-1].events.append("outcome")
            return _orig(*a, **k)

        monkeypatch.setattr(worker_module.ledger, name, marked)

    built.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == outcome

    events = spies[-1].events
    assert "outcome" in events, events
    before = events[: events.index("outcome")]
    # The claim's FOR UPDATE lock must still be held when the outcome is written; a
    # rollback in between would release it with the row still pending and still due.
    assert "rollback" not in before, events


# --------------------------------------------------------------------------------------
# the selection: which talkgroups, which systems, which recordings
# --------------------------------------------------------------------------------------
#
# The rule under test, stated once: ``all`` publishes everything and never looks at the
# per-talkgroup checkbox; ``allow`` publishes only the talkgroups whose checkbox is ticked;
# ``deny`` publishes everything except the talkgroups whose checkbox is cleared.  Both
# ``allow`` and ``deny`` read the same column, so with every row still at its default
# (true) they agree — which is exactly what the first two rows below assert.


def _settings(export_settings, **overrides):  # noqa: ANN001, ANN202
    return export_settings.model_copy(update=overrides)


def _flag(session, tgid: int, service: str, value: bool) -> None:  # noqa: ANN001
    """Set *this service's* checkbox on a talkgroup.

    Which column that is, is the service's own business now: Broadcastify Calls reads
    ``export_bcfy_calls`` and the live Feed reads ``export_broadcastify``
    (:data:`app.exporters.gate.FLAG_COLUMNS`), so the tests go through the same lookup the
    gate does rather than assuming a name.
    """
    row = session.scalar(select(Talkgroup).where(Talkgroup.tgid == tgid))
    column = gate.export_flag_column(Talkgroup, service)
    setattr(row, column.key, value)
    session.commit()


@pytest.mark.parametrize(
    ("mode", "flag", "expected"),
    [
        ("all", True, True),
        ("all", False, True),  # the checkbox is not consulted at all in `all`
        ("allow", True, True),
        ("allow", False, False),
        ("deny", True, True),
        ("deny", False, False),
    ],
)
def test_the_selection_rule_for_every_mode(  # noqa: ANN001
    session, export_settings, tmp_path, mode, flag, expected
) -> None:
    """`all` ignores the flag; `allow` and `deny` both come down to "is it ticked"."""
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    _flag(session, 1701, "openmhz", flag)
    settings = _settings(export_settings, export_openmhz_talkgroup_mode=mode)
    assert gate.selected(session, call, settings, "openmhz") is expected


def test_an_unreadable_mode_publishes_everything(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """A typo in the mode must not silently stop publishing — it reads as `all`."""
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    _flag(session, 1701, "openmhz", False)
    settings = _settings(export_settings, export_openmhz_talkgroup_mode="nonsense")
    assert gate.selection_mode(settings, "openmhz") == "all"
    assert gate.selected(session, call, settings, "openmhz") is True


def test_the_two_services_have_their_own_checkbox(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Clearing the Broadcastify box must not stop the OpenMHz upload."""
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    _flag(session, 1701, "broadcastify", False)
    settings = _settings(
        export_settings,
        export_broadcastify_talkgroup_mode="allow",
        export_openmhz_talkgroup_mode="allow",
    )
    assert gate.selected(session, call, settings, "broadcastify") is False
    assert gate.selected(session, call, settings, "openmhz") is True


def test_selection_filters_the_discovery_query(session, worker, export_settings, tmp_path) -> None:  # noqa: ANN001
    """The SQL half agrees with the Python half: an unticked talkgroup gets no ledger row."""
    enable(session, "openmhz")
    worker.cycle()
    seed_call(session, root=tmp_path, tgid=1701, priority="live")
    seed_call(session, root=tmp_path, tgid=1102, priority="live")
    _flag(session, 1102, "openmhz", False)
    worker.settings = _settings(export_settings, export_openmhz_talkgroup_mode="allow")

    worker.cycle()

    exported = {
        row.talkgroup_alias_at_record: row.id
        for row in session.scalars(select(Call))
        if session.scalar(select(CallExport).where(CallExport.call_id == row.id)) is not None
    }
    rows = session.scalars(select(CallExport)).all()
    assert len(rows) == 1
    assert exported  # the surviving row is the ticked talkgroup's
    kept = session.get(Call, rows[0].call_id)
    assert kept.talkgroup_ids == [1701]


# ---- conventional traffic, keyed by frequency ----------------------------------------


def _frequency_row(session, *, frequency_hz: int, **flags):  # noqa: ANN001, ANN202
    from app.models import Site, SiteFrequency

    system = session.scalar(select(System).where(System.name == "Example System"))
    site = session.scalar(select(Site).where(Site.system_id == system.id))
    if site is None:
        site = Site(system_id=system.id, name="Westside")
        session.add(site)
        session.flush()
    row = SiteFrequency(
        site_id=site.id,
        frequency_hz=frequency_hz,
        role="voice",
        source="observed",
        **flags,
    )
    session.add(row)
    session.commit()
    return row


def test_a_conventional_call_is_judged_by_its_frequency(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """No talkgroup means the channel *is* the frequency, and its checkbox decides."""
    call = seed_call(session, root=tmp_path, with_talkgroup=False, frequency_hz=154_265_000, priority="live")
    _frequency_row(session, frequency_hz=154_265_000, export_openmhz=False, export_broadcastify=True)
    allow = _settings(export_settings, export_openmhz_talkgroup_mode="allow")
    assert gate.selected(session, call, allow, "openmhz") is False

    both = _settings(export_settings, export_broadcastify_talkgroup_mode="allow")
    assert gate.selected(session, call, both, "broadcastify") is True


def test_an_uncatalogued_frequency_is_not_published(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Fail closed: a frequency nobody has catalogued is not one to publish on a default."""
    call = seed_call(session, root=tmp_path, with_talkgroup=False, frequency_hz=154_995_000, priority="live")
    allow = _settings(export_settings, export_openmhz_talkgroup_mode="allow")
    assert gate.selected(session, call, allow, "openmhz") is False
    # …but `all` still publishes it, because `all` never asks.
    assert gate.selected(session, call, export_settings, "openmhz") is True


# ---- encrypted, short, and the system list -------------------------------------------


def test_encrypted_calls_can_be_opted_into(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Off by default; the switch is the only thing that lets one through."""
    call = seed_call(session, root=tmp_path, priority="live", encrypted=True)
    assert ledger.skip_reason_for(call, export_settings) == "encrypted"
    assert ledger.skip_reason_for(call, _settings(export_settings, export_include_encrypted=True)) is None


def test_a_call_nobody_spoke_on_is_not_published(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """A mic key-up is a real transmission and a worthless upload.

    The key-up pair: 0.648 s, one click, no words — and both copies were queued
    for both services because nothing in the ledger's three refusals is about *content*.
    The audio pass measures it (``audio_meta.content``) and this is where that lands.
    """
    call = seed_call(session, root=tmp_path, priority="live")
    call.audio_meta = {"content": "none", "speech_s": 0.04, "speech_floor_db": -40.0}
    session.flush()

    reason = ledger.skip_reason_for(call, export_settings)
    assert reason == "no speech (0.04 s above -40 dB)"
    assert reason.startswith(ledger.NO_SPEECH_REASON)

    # The live Feed is the channel as it sounded, key-ups included: it opts out.
    assert ledger.skip_reason_for(call, export_settings, no_speech=False) is None

    # A call the pass has not reached yet carries no verdict and is exported as before.
    call.audio_meta = {"silent": False}
    assert ledger.skip_reason_for(call, export_settings) is None
    call.audio_meta = None
    assert ledger.skip_reason_for(call, export_settings) is None

    # …and one it measured speech in is not refused either.
    call.audio_meta = {"content": "speech", "speech_s": 1.4, "speech_floor_db": -40.0}
    assert ledger.skip_reason_for(call, export_settings) is None

    # Nor is a **page**: the tone-out pair are 4.25 s of fire dispatch tone-out and not
    # one word, and they are the most important audio on the system.  Only `none` refuses.
    call.audio_meta = {"content": "page", "speech_s": 0.0, "speech_floor_db": -40.0}
    assert ledger.skip_reason_for(call, export_settings) is None


def test_discovery_writes_the_no_speech_skip_rather_than_a_pending_row(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """The ledger says we saw it and decided against it, which is the point of a skip row."""
    call = seed_call(session, root=tmp_path, priority="live")
    call.audio_meta = {"content": "none", "speech_s": 0.04, "speech_floor_db": -40.0}
    session.flush()

    ledger.discover(
        session,
        service="openmhz",
        settings=export_settings,
        watermark=call.started_at - timedelta(minutes=1),
    )
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row is not None
    assert row.status == "skipped"
    assert row.skip_reason.startswith(ledger.NO_SPEECH_REASON)


def test_short_calls_are_not_published(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """A dead key is not worth putting on somebody else's archive."""
    short = seed_call(session, root=tmp_path, tgid=1701, priority="live", duration=0.2)
    long = seed_call(session, root=tmp_path, tgid=1102, priority="live", duration=3.0)
    settings = _settings(export_settings, export_min_duration_s=0.5)
    assert gate.content_ok(short, settings) is False
    assert gate.content_ok(long, settings) is True
    assert gate.content_ok(short, _settings(export_settings, export_min_duration_s=0)) is True


def test_short_calls_are_not_even_discovered(session, worker, export_settings, tmp_path) -> None:  # noqa: ANN001
    """The duration floor is a discovery filter, so raising it later is not retroactive."""
    enable(session, "openmhz")
    worker.cycle()
    seed_call(session, root=tmp_path, tgid=1701, priority="live", duration=0.2)
    worker.settings = _settings(export_settings, export_min_duration_s=0.5)

    worker.cycle()

    assert session.scalars(select(CallExport)).all() == []


def test_the_system_selection_narrows_to_named_systems(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Empty means every system; a list means only those keyup system ids."""
    call = seed_call(session, root=tmp_path, priority="live")
    assert gate.system_ok(call, export_settings, "openmhz") is True
    assert gate.system_ok(
        call, _settings(export_settings, export_openmhz_systems=str(call.system_id)), "openmhz"
    )
    assert gate.system_ok(call, _settings(export_settings, export_openmhz_systems="999"), "openmhz") is False
    # A junk entry is dropped rather than taken as "no systems at all".
    assert gate.systems_for(_settings(export_settings, export_openmhz_systems="1, nope ,2"), "openmhz") == {
        1,
        2,
    }


def test_a_deselected_call_is_skipped_at_upload_time(session, worker, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Discovery said yes; the operator unticked the box before the upload went out."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    worker.cycle()  # discovers and uploads under `all`

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row.status == "uploaded"

    # A second call, discovered under `all`, then deselected before its turn.
    later = seed_call(session, root=tmp_path, tgid=1102, priority="live")
    worker.settings = export_settings
    ledger.discover(
        session, service="openmhz", watermark=gate.watermark(session, "openmhz"), settings=export_settings
    )
    _flag(session, 1102, "openmhz", False)
    worker.settings = _settings(export_settings, export_openmhz_talkgroup_mode="allow")

    worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == later.id))
    assert row.status == "skipped"
    assert row.skip_reason == "talkgroup not selected for export"


# --------------------------------------------------------------------------------------
# settings precedence: the environment seeds, the table wins
# --------------------------------------------------------------------------------------


def test_the_settings_table_overrides_the_environment(session, export_settings) -> None:  # noqa: ANN001
    """Same precedence the STT backends use: unset falls through, set wins, cleared reverts."""
    from app import runtime_settings

    assert export_settings.openmhz_short_name == "examplesys"
    merged = runtime_settings.effective_settings(session, base=export_settings)
    assert merged.openmhz_short_name == "examplesys"

    session.add(Setting(key="openmhz_short_name", value="from-the-table"))
    session.commit()
    runtime_settings.invalidate()
    merged = runtime_settings.effective_settings(session, base=export_settings)
    assert merged.openmhz_short_name == "from-the-table"

    session.delete(session.get(Setting, "openmhz_short_name"))
    session.commit()
    runtime_settings.invalidate()
    merged = runtime_settings.effective_settings(session, base=export_settings)
    assert merged.openmhz_short_name == "examplesys"


def test_every_export_knob_is_settable_at_runtime() -> None:
    """A knob the Exports tab shows has to be one the settings endpoint accepts."""
    from app import runtime_settings

    expected = {
        "broadcastify_api_key",
        "broadcastify_system_id",
        "broadcastify_slot",
        "broadcastify_url",
        "openmhz_api_key",
        "openmhz_short_name",
        "openmhz_url",
        "export_broadcastify_enabled",
        "export_openmhz_enabled",
        "export_broadcastify_talkgroup_mode",
        "export_openmhz_talkgroup_mode",
        "export_broadcastify_systems",
        "export_openmhz_systems",
        "export_include_encrypted",
        "export_min_duration_s",
        "export_poll_seconds",
        "export_min_interval_s",
        "export_timeout_s",
        "export_batch_size",
        "export_max_age_minutes",
        "export_max_attempts",
    }
    assert expected <= runtime_settings.SETTABLE_KEYS
    # The credentials are write-only, exactly like the webhook signing secret.
    assert {"broadcastify_api_key", "openmhz_api_key"} <= runtime_settings.WRITE_ONLY_KEYS


def test_both_switches_default_off() -> None:
    """The one rule this whole feature must never break."""
    settings = Settings(_env_file=None)
    assert settings.export_broadcastify_enabled is False
    assert settings.export_openmhz_enabled is False
    assert settings.export_broadcastify_talkgroup_mode == "all"
    assert settings.export_openmhz_talkgroup_mode == "all"


# --------------------------------------------------------------------------------------
# testing the credentials, without publishing anything
# --------------------------------------------------------------------------------------


def _record(seen: list[httpx.Request], body: str = "0 OK", status: int = 200):  # noqa: ANN202
    """A transport that records every request and answers stage 1 with ``body``."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _parts(request: httpx.Request) -> dict[str, str]:
    """The multipart form fields of a stage-1/test POST, as a plain dict."""
    body = request.content.decode("latin-1")
    out: dict[str, str] = {}
    for chunk in body.split("--" + request.headers["content-type"].split("boundary=")[1]):
        if 'name="' not in chunk:
            continue
        name = chunk.split('name="', 1)[1].split('"', 1)[0]
        out[name] = chunk.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]
    return out


def test_testing_broadcastify_sends_the_documented_test_parameter(export_settings) -> None:  # noqa: ANN001
    """``test=1`` with ``apiKey`` and ``systemId``, and **nothing is ever PUT**."""
    seen: list[httpx.Request] = []
    result = BroadcastifyClient(export_settings, client=_record(seen)).test_credentials()

    assert [r.method for r in seen] == ["POST"]
    post = seen[0]
    assert str(post.url) == "https://api.broadcastify.test/call-upload"
    assert post.headers["content-type"].startswith("multipart/form-data")
    # Exactly the three the documentation names — no ts, no tg, no freq, so there is no
    # call to register and no upload URL to be handed back.
    assert _parts(post) == {"apiKey": "test-bcfy-key", "systemId": "34", "test": "1"}
    assert result["ok"] is True
    assert result["environment"] == "production"
    # The raw first line, verbatim: what a success actually looks like is unknown until it
    # has been seen against a real account.
    assert "0 OK" in result["detail"]


def test_testing_broadcastify_never_puts_audio(export_settings) -> None:  # noqa: ANN001
    """Even when the endpoint answers with something that parses as an upload URL."""
    seen: list[httpx.Request] = []
    client = BroadcastifyClient(
        export_settings, client=_record(seen, body="0 https://upload.example.test/one-time/abc")
    )
    assert client.test_credentials()["ok"] is True
    assert [r.method for r in seen] == ["POST"]
    assert not any(r.method == "PUT" for r in seen)


def test_testing_broadcastify_reports_refused_credentials(export_settings) -> None:  # noqa: ANN001
    """``100 NO-API-KEY-SPECIFIED`` is the documented refusal and must not read as ok."""
    seen: list[httpx.Request] = []
    client = BroadcastifyClient(export_settings, client=_record(seen, body="100 NO-API-KEY-SPECIFIED"))
    result = client.test_credentials()
    assert result["ok"] is False
    assert "NO-API-KEY-SPECIFIED" in result["detail"]


def test_testing_broadcastify_redacts_the_key_from_any_reply(export_settings) -> None:  # noqa: ANN001
    """Stage 1 does not echo the key; the reply is scrubbed on the way out regardless."""
    seen: list[httpx.Request] = []
    client = BroadcastifyClient(export_settings, client=_record(seen, body="0 saw test-bcfy-key"))
    result = client.test_credentials()
    assert "test-bcfy-key" not in str(result["detail"])
    assert "***" in str(result["detail"])


def test_testing_broadcastify_without_a_key_says_which_one(tmp_path) -> None:  # noqa: ANN001
    settings = Settings(
        _env_file=None, audio_root=tmp_path, broadcastify_system_id="34", export_ntp_server=""
    )
    result = BroadcastifyClient(settings).test_credentials()
    assert result["ok"] is False
    assert "BROADCASTIFY_API_KEY" in result["detail"]


def test_testing_broadcastify_in_development_names_the_dev_variables(tmp_path) -> None:  # noqa: ANN001
    """A development environment with no development key is unconfigured, not a fallback."""
    settings = Settings(
        _env_file=None,
        audio_root=tmp_path,
        broadcastify_environment="development",
        broadcastify_api_key="the-production-key",
        broadcastify_system_id="34",
        export_ntp_server="",
    )
    result = BroadcastifyClient(settings).test_credentials()
    assert result["ok"] is False
    assert "BROADCASTIFY_DEV_API_KEY" in result["detail"]
    assert "BROADCASTIFY_DEV_SYSTEM_ID" in result["detail"]
    assert "the-production-key" not in str(result["detail"])


# --------------------------------------------------------------------------------------
# environment routing
# --------------------------------------------------------------------------------------


def _dev(export_settings, **kw):  # noqa: ANN001, ANN202
    return export_settings.model_copy(
        update={
            "broadcastify_environment": "development",
            "broadcastify_dev_url": "https://api.broadcastify.test/calls-dev/call-upload",
            "broadcastify_dev_api_key": "dev-key",
            "broadcastify_dev_system_id": "99",
            **kw,
        }
    )


def test_environment_picks_url_key_and_system_id_together(export_settings) -> None:  # noqa: ANN001
    """Neither set of credentials can reach the other environment's endpoint."""
    live = endpoint(export_settings)
    assert (live.name, live.api_key, live.system_id) == ("production", "test-bcfy-key", "34")
    assert live.url == "https://api.broadcastify.test/call-upload"

    dev = endpoint(_dev(export_settings))
    assert (dev.name, dev.api_key, dev.system_id) == ("development", "dev-key", "99")
    assert dev.url == "https://api.broadcastify.test/calls-dev/call-upload"


def test_an_unreadable_environment_reads_as_production(export_settings) -> None:  # noqa: ANN001
    bad = export_settings.model_copy(update={"broadcastify_environment": "staging"})
    assert endpoint(bad).name == "production"


def test_development_upload_uses_the_development_credentials(export_settings) -> None:  # noqa: ANN001
    """The whole point of the environment field, asserted on the wire."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("call-upload"):
            return httpx.Response(200, text="0 https://upload.example.test/one-time/dev1")
        return httpx.Response(200)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    BroadcastifyClient(_dev(export_settings), client=http).upload(facts(), b"AUDIO")

    post = seen[0]
    assert str(post.url) == "https://api.broadcastify.test/calls-dev/call-upload"
    fields = _parts(post)
    assert fields["apiKey"] == "dev-key"
    assert fields["systemId"] == "99"
    assert "test-bcfy-key" not in post.content.decode("latin-1")


def test_the_monitoring_links_follow_the_environment(export_settings) -> None:  # noqa: ANN001
    assert monitoring_url(export_settings) == "https://www.broadcastify.com/calls/status/"
    # The node's own page: the configured system id is the node id in the manage URL.
    assert monitoring_url(export_settings, "manage") == "https://www.broadcastify.com/calls/manage/34"
    bare = export_settings.model_copy(update={"broadcastify_system_id": ""})
    assert monitoring_url(bare, "manage") == "https://www.broadcastify.com/calls/manage/"
    dev = _dev(export_settings)
    assert monitoring_url(dev) == "https://www.broadcastify.com/calls-dev/status/"
    # Development has no node-management page, and the empty string is how the UI is told
    # not to render a link to one.
    assert monitoring_url(dev, "manage") == ""


def test_testing_openmhz_resolves_the_short_name(export_settings) -> None:  # noqa: ANN001
    """A 404 from the public calls endpoint means the shortName is wrong, and says so."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/examplesys/calls"
        return httpx.Response(404, text="not found")

    client = OpenMhzClient(export_settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = client.test_credentials()

    assert result["ok"] is False
    assert "examplesys" in result["detail"]


# --------------------------------------------------------------------------------------
# the ledger's observability reads
# --------------------------------------------------------------------------------------


def test_window_summary_only_counts_recent_rows(session, worker, tmp_path) -> None:  # noqa: ANN001
    enable(session, "openmhz")
    worker.cycle()
    seed_call(session, root=tmp_path, tgid=1701, priority="live")
    worker.cycle()

    assert ledger.window_summary(session, hours=24)["openmhz"] == {"uploaded": 1}

    row = session.scalars(select(CallExport)).one()
    row.updated_at = datetime.now(UTC) - timedelta(days=3)
    session.commit()
    assert ledger.window_summary(session, hours=24) == {}


def test_requeue_refuses_an_uploaded_row(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The uploaded row *is* the promise a call was not published twice."""
    enable(session, "openmhz")
    worker.cycle()
    seed_call(session, root=tmp_path, tgid=1701, priority="live")
    worker.cycle()

    row = session.scalars(select(CallExport)).one()
    assert ledger.requeue(session, row.id) is None

    row.status = "failed"
    row.attempts = 5
    row.last_error = "boom"
    session.commit()
    again = ledger.requeue(session, row.id)
    assert again is not None
    assert again.status == "pending"
    assert again.attempts == 0


# --------------------------------------------------------------------------------------
# step 1: the clock
# --------------------------------------------------------------------------------------
#
# Step 1 of Broadcastify's own workflow is "verify time synchronization on the host
# system.  NTP accuracy is required; calls with bad timestamps are rejected or
# deduplicated incorrectly."  ``ts`` is part of the tuple they deduplicate on, so a drifted
# clock does damage that cannot be repaired afterwards.  These tests cover the measurement,
# the two thresholds and — the one that matters — that an *unmeasured* clock never stops an
# upload.


class FakeNtp:
    """A UDP responder on 127.0.0.1 that answers with a known, deliberate offset.

    The reply is a real 48-byte SNTP packet: mode 4 (server), stratum 2, and receive and
    transmit timestamps set to ``now + offset`` in NTP's 1900 epoch.  That makes the offset
    this module computes exactly ``-offset`` — the host is *behind* a server that is ahead —
    which is the sign convention :class:`app.exporters.clock.ClockCheck` documents.
    """

    def __init__(self, offset_s: float = 0.0) -> None:
        self.offset_s = offset_s
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.address = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _stamp(self, when: float) -> bytes:
        ntp = when + clock.NTP_EPOCH_DELTA
        return struct.pack("!II", int(ntp), int((ntp % 1) * 2**32))

    def _serve(self) -> None:
        try:
            _, peer = self.sock.recvfrom(1024)
        except OSError:  # pragma: no cover - the socket was closed while waiting
            return
        server_now = time.time() + self.offset_s
        packet = bytearray(48)
        packet[0] = 0x24  # LI=0, VN=4, Mode=4 (server)
        packet[1] = 2  # stratum
        packet[32:40] = self._stamp(server_now)  # receive
        packet[40:48] = self._stamp(server_now)  # transmit
        with contextlib.suppress(OSError):  # pragma: no cover - the socket may be closed
            self.sock.sendto(bytes(packet), peer)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture(autouse=True)
def _fresh_clock():  # noqa: ANN202
    """Module-level measurement cache, dropped around every test in this file."""
    clock.reset()
    yield
    clock.reset()


def test_ntp_offset_measures_a_known_skew() -> None:
    """A server 30 s ahead makes this host read as 30 s behind."""
    server = FakeNtp(offset_s=30.0)
    try:
        check = clock.ntp_offset(server.address)
    finally:
        server.close()
    assert check.source == "ntp"
    assert check.offset_s is not None
    assert check.offset_s == pytest.approx(-30.0, abs=0.5)
    assert check.ok is False
    assert check.should_pause is True


def test_ntp_offset_on_a_synchronized_host_is_ok() -> None:
    server = FakeNtp(offset_s=0.0)
    try:
        check = clock.ntp_offset(server.address)
    finally:
        server.close()
    assert check.offset_s == pytest.approx(0.0, abs=0.5)
    assert check.ok is True
    assert check.should_pause is False
    assert check.as_dict() == {
        "clock_offset_s": check.offset_s,
        "clock_ok": True,
        "clock_source": "ntp",
    }


def test_an_unreachable_ntp_server_is_unknown_and_never_pauses() -> None:
    """ "We could not measure the clock" is not "the clock is wrong"."""
    # Port 1 on the loopback: nothing is listening, so the short timeout expires.
    check = clock.ntp_offset("127.0.0.1:1", timeout_s=0.2)
    assert check.offset_s is None
    assert check.source is None
    assert check.ok is None
    assert check.should_pause is False
    assert "127.0.0.1:1" in check.detail


def test_a_blank_ntp_server_falls_back_to_the_date_header(tmp_path) -> None:  # noqa: ANN001
    """The coarse route, for when there is no NTP server to ask."""
    settings = Settings(_env_file=None, audio_root=tmp_path, export_ntp_server="")
    when = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).timestamp()
    check = clock.check_clock(
        settings,
        date_header="Tue, 01 Sep 2026 12:00:00 GMT",
        sent_at=when + 9.0,
        received_at=when + 11.0,
        url="https://api.broadcastify.test/call-upload",
    )
    # Midpoint of the bracket is 10 s after the server's Date: this host is 10 s ahead.
    assert check.source == "http"
    assert check.offset_s == pytest.approx(10.0, abs=0.01)
    assert check.should_pause is True


def test_ntp_wins_over_the_date_header(tmp_path) -> None:  # noqa: ANN001
    """NTP first, always: it is orders of magnitude the more precise of the two."""
    server = FakeNtp(offset_s=0.0)
    try:
        settings = Settings(_env_file=None, audio_root=tmp_path, export_ntp_server=server.address)
        check = clock.check_clock(
            settings,
            date_header="Tue, 01 Sep 2020 12:00:00 GMT",
            sent_at=time.time(),
            received_at=time.time(),
        )
    finally:
        server.close()
    assert check.source == "ntp"
    assert check.ok is True


def test_the_worker_pauses_broadcastify_on_a_bad_clock(export_settings, tmp_path, caplog) -> None:  # noqa: ANN001
    """Past five seconds, the Calls cycle does nothing — and says so, once."""
    from app.db import SessionLocal

    server = FakeNtp(offset_s=30.0)
    try:
        drifted = export_settings.model_copy(update={"export_ntp_server": server.address})
        built = ExportWorker(settings=drifted, session_factory=SessionLocal, audio_root=tmp_path)
        with caplog.at_level(logging.WARNING, logger="app.exporters.worker"):
            assert built._clock_pauses(drifted) is True
            # A second cycle still pauses, but must not repeat the warning.
            assert built._clock_pauses(drifted) is True
    finally:
        server.close()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "paused" in warnings[0].getMessage()


def test_the_worker_never_pauses_on_an_unmeasured_clock(export_settings, tmp_path) -> None:  # noqa: ANN001
    """A missing NTP server must not be able to stop uploading."""
    from app.db import SessionLocal

    blank = export_settings.model_copy(update={"export_ntp_server": ""})
    built = ExportWorker(settings=blank, session_factory=SessionLocal, audio_root=tmp_path)
    assert built._clock_pauses(blank) is False


def test_a_paused_broadcastify_cycle_uploads_nothing(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """The gate is checked before discovery, so a drifted clock builds no queue either."""
    from app.db import SessionLocal

    server = FakeNtp(offset_s=30.0)
    try:
        drifted = export_settings.model_copy(update={"export_ntp_server": server.address})
        client = RecordingClient("broadcastify")
        built = ExportWorker(
            settings=drifted, session_factory=SessionLocal, clients=[client], audio_root=tmp_path
        )
        enable(session, "broadcastify")
        seed_call(session, root=tmp_path, priority="live")
        built.cycle()
    finally:
        server.close()

    assert client.uploads == []
    assert session.scalars(select(CallExport)).all() == []


def test_the_clock_recovers_and_says_so(export_settings, tmp_path, caplog) -> None:  # noqa: ANN001
    """Recovery resumes uploads and logs it — the watermark is never cleared."""
    from app.db import SessionLocal

    bad = FakeNtp(offset_s=30.0)
    try:
        drifted = export_settings.model_copy(update={"export_ntp_server": bad.address})
        built = ExportWorker(settings=drifted, session_factory=SessionLocal, audio_root=tmp_path)
        assert built._clock_pauses(drifted) is True
    finally:
        bad.close()
    clock.reset()
    good = FakeNtp(offset_s=0.0)
    try:
        healthy = export_settings.model_copy(update={"export_ntp_server": good.address})
        with caplog.at_level(logging.INFO, logger="app.exporters.worker"):
            assert built._clock_pauses(healthy) is False
    finally:
        good.close()
    assert any("resuming" in r.getMessage() for r in caplog.records)


def test_the_test_button_reports_the_clock(export_settings) -> None:  # noqa: ANN001
    """A test press is an operator asking "is this fit to upload?"; step 1 is part of that."""
    server = FakeNtp(offset_s=0.0)
    try:
        settings = export_settings.model_copy(update={"export_ntp_server": server.address})
        result = BroadcastifyClient(settings, client=_record([])).test_credentials()
    finally:
        server.close()
    assert result["clock_source"] == "ntp"
    assert result["clock_ok"] is True
    assert isinstance(result["clock_offset_s"], float)



# --------------------------------------------------------------------------------------
# trunked systems only: the Broadcastify Calls gate
# --------------------------------------------------------------------------------------
#
# Calls files every upload under a system id issued for a *trunked* system.  Keyup archives
# both kinds side by side — a county's trunked network and the conventional fire channels
# beside it — so "is this call from a trunked system" is a real question here, and the wrong
# answer is a call published somewhere it does not belong.  Hence a rule rather than a
# setting: nothing in the Admin page can widen it, and every layer refuses the same call.


def _conventional(session, call, protocol: str | None = "Conventional analog") -> System:  # noqa: ANN001
    """Move a seeded call (and its talkgroup) onto a system that is not trunked."""
    system = System(name=f"County Fire {protocol}", protocol=protocol)
    session.add(system)
    session.flush()
    if call.talkgroup_id is not None:
        session.get(Talkgroup, call.talkgroup_id).system_id = system.id
    call.system_id = system.id
    session.commit()
    return system


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [
        ("P25 Phase 1", True),
        ("P25 Phase 2", True),
        ("Motorola SmartZone", True),
        ("DMR Tier III", True),
        ("EDACS", True),
        ("LTR", True),
        ("DMR conventional", False),  # the family is trunked; this member is not
        ("Conventional analog", False),
        ("Narrowband FM", False),
        ("", False),
        (None, False),  # nobody has said → not published to somebody else's archive
    ],
)
def test_the_protocol_guess_reads_the_decoders_words(protocol, expected) -> None:  # noqa: ANN001
    from app.models import infer_trunked

    assert infer_trunked(protocol) is expected
    assert System(name="x", protocol=protocol).is_trunked is expected


def test_the_trunked_column_overrules_the_guess() -> None:
    """An operator who knows better than the decoder's free text is never overruled."""
    assert System(name="x", protocol="P25 Phase 1", trunked=False).is_trunked is False
    assert System(name="x", protocol="Conventional analog", trunked=True).is_trunked is True


def test_the_sql_half_agrees_with_the_python_half(session) -> None:  # noqa: ANN001
    """The discovery filter and the in-process check must select the same systems."""
    for name, protocol, trunked in [
        ("A", "P25 Phase 1", None),
        ("B", "Conventional analog", None),
        ("C", "Conventional analog", True),
        ("D", "P25 Phase 1", False),
        ("E", None, None),
    ]:
        session.add(System(name=name, protocol=protocol, trunked=trunked))
    session.commit()
    in_sql = set(
        session.scalars(select(System.name).where(gate.trunked_system_clause())).all()
    )
    in_python = {row.name for row in session.scalars(select(System)) if row.is_trunked}
    assert in_sql == in_python == {"A", "C"}
    assert {row.name for row in gate.trunked_systems(session)} == {"A", "C"}


def test_broadcastify_refuses_a_call_from_a_conventional_system(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """The distinct ledger reason, so the Exports page can say *why* nothing is uploading."""
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    _conventional(session, call)

    assert gate.trunked_ok(session, call, "broadcastify") is False
    assert gate.excluded_reason(session, call, export_settings, "broadcastify") == "not_trunked"
    mark = datetime.now(UTC) - timedelta(minutes=5)
    assert (
        gate.exportable(call, mark, settings=export_settings, service="broadcastify", session=session)
        is False
    )


def test_the_feed_and_openmhz_are_not_gated_on_trunked(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Conventional traffic is perfectly fine on the live stream and on OpenMHz."""
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    _conventional(session, call)

    assert gate.excluded_reason(session, call, export_settings, "openmhz") is None
    assert gate.excluded_reason(session, call, export_settings, "broadcastify_feed") is None
    assert gate.trunked_clause("openmhz") is None
    assert gate.trunked_clause("broadcastify_feed") is None


def test_a_conventional_system_never_gets_a_calls_ledger_row(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """The SQL half: filtered out of discovery rather than parked as a skipped row.

    Discovery is driven directly here rather than through a worker cycle, because it *is*
    the assertion: the row must never be created, and a worker with no Broadcastify client
    would prove nothing about why.
    """
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    system = _conventional(session, call)
    mark = datetime.now(UTC) - timedelta(minutes=5)

    assert ledger.discover(session, service="broadcastify", watermark=mark, settings=export_settings) == 0
    # …and it is the trunked rule doing it, not the band, the watermark or the content.
    assert ledger.discover(session, service="openmhz", watermark=mark, settings=export_settings) == 1

    system.trunked = True
    session.commit()

    assert ledger.discover(session, service="broadcastify", watermark=mark, settings=export_settings) == 1
    rows = session.scalars(select(CallExport).where(CallExport.service == "broadcastify")).all()
    assert [row.call_id for row in rows] == [call.id]


def test_a_new_nbfm_system_is_not_trunked() -> None:
    """What a trunkd conventional push names a new system (``decoder=NBFM``)."""
    from app.models import infer_trunked

    assert infer_trunked("NBFM") is False
    assert System(name="x", protocol="NBFM").is_trunked is False


@pytest.mark.parametrize(
    ("raw_meta", "analog"),
    [
        ({"mode": "nbfm", "decoder": "NBFM"}, True),  # what trunkd sends
        ({"mode": "NBFM"}, True),
        ({"decoder": " nbfm "}, True),
        ({"mode": "digital", "decoder": "P25 Phase 1"}, False),
        ({"audio_type": "analog"}, False),  # a trunked system's analog talkgroup is fine
        ({"id3": {"Decoder": "NBFM"}}, False),  # only the recorder's own top-level keys
        ({"mode": 1}, False),
        ({}, False),
        (None, False),
    ],
)
def test_is_analog_call_reads_only_the_recorders_mode_and_decoder(raw_meta, analog) -> None:  # noqa: ANN001
    assert gate.is_analog_call(Call(raw_meta=raw_meta)) is analog


def test_the_sql_analog_mark_agrees_with_the_python_one(session, tmp_path) -> None:  # noqa: ANN001
    metas = [
        {"mode": "nbfm", "decoder": "NBFM"},
        {"decoder": " nbfm "},
        {"mode": "digital"},
        {"audio_type": "analog"},
        {"id3": {"Decoder": "NBFM"}},
        None,
    ]
    calls = []
    for n, meta in enumerate(metas):
        call = seed_call(
            session, root=tmp_path, tgid=1701, priority="live", file_path=f"20260901/analog/{n}.mp3"
        )
        call.raw_meta = meta
        calls.append(call)
    session.commit()
    in_sql = set(session.scalars(select(Call.id).where(gate.analog_call_clause())).all())
    in_python = {c.id for c in calls if gate.is_analog_call(c)}
    assert in_sql == in_python == {calls[0].id, calls[1].id}


def test_broadcastify_refuses_an_nbfm_call_filed_under_a_trunked_system(
    session, export_settings, tmp_path  # noqa: ANN001
) -> None:
    """Defence in depth: a conventional channel filed under a P25 system's label.

    The system passes the trunked rule; the call itself says it is analog NBFM, and Calls
    refuses it in both halves — never discovered, and ``not_trunked`` if it was claimed.
    The Feed and OpenMHz, which take conventional traffic, still take it.
    """
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    assert session.get(System, call.system_id).is_trunked is True
    call.raw_meta = {"recorder": "trunkd", "mode": "nbfm", "decoder": "NBFM", "tone": "192.8"}
    session.commit()
    mark = datetime.now(UTC) - timedelta(minutes=5)

    assert gate.trunked_ok(session, call, "broadcastify") is False
    assert gate.trunked_ok(None, call, "broadcastify") is False  # no session needed to say no
    assert gate.excluded_reason(session, call, export_settings, "broadcastify") == "not_trunked"
    assert gate.excluded_reason(session, call, export_settings, "openmhz") is None
    assert gate.excluded_reason(session, call, export_settings, "broadcastify_feed") is None
    assert ledger.discover(session, service="broadcastify", watermark=mark, settings=export_settings) == 0
    assert ledger.discover(session, service="openmhz", watermark=mark, settings=export_settings) == 1

    # Even an operator's explicit ``trunked = True`` does not let an analog call through.
    session.get(System, call.system_id).trunked = True
    session.commit()
    assert gate.trunked_ok(session, call, "broadcastify") is False
    assert ledger.discover(session, service="broadcastify", watermark=mark, settings=export_settings) == 0


def test_a_p25_call_without_the_analog_mark_is_unaffected(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Existing data: no ``mode``/``decoder`` in ``raw_meta`` → exactly the old answer."""
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    call.raw_meta = {"recorder": "trunkd", "nac": "0x4A3", "audio_type": "digital"}
    session.commit()
    mark = datetime.now(UTC) - timedelta(minutes=5)

    assert gate.trunked_ok(session, call, "broadcastify") is True
    assert gate.excluded_reason(session, call, export_settings, "broadcastify") is None
    assert ledger.discover(session, service="broadcastify", watermark=mark, settings=export_settings) == 1


def test_calls_and_the_feed_read_different_checkboxes(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """One Broadcastify account, two products, two decisions."""
    call = seed_call(session, root=tmp_path, tgid=1701, priority="live")
    settings = _settings(export_settings, export_broadcastify_talkgroup_mode="allow")

    _flag(session, 1701, "broadcastify_feed", False)
    assert gate.selected(session, call, settings, "broadcastify") is True
    assert gate.selected(session, call, settings, "broadcastify_feed") is False

    _flag(session, 1701, "broadcastify_feed", True)
    _flag(session, 1701, "broadcastify", False)
    assert gate.selected(session, call, settings, "broadcastify") is False
    assert gate.selected(session, call, settings, "broadcastify_feed") is True


def test_the_feed_still_reads_broadcastifys_mode_and_system_list(export_settings) -> None:  # noqa: ANN001
    """The *selection* is shared even though the checkbox is not: one account, one list."""
    settings = _settings(
        export_settings,
        export_broadcastify_talkgroup_mode="deny",
        export_broadcastify_systems="7, 9",
    )
    assert gate.selection_mode(settings, "broadcastify_feed") == "deny"
    assert gate.systems_for(settings, "broadcastify_feed") == {7, 9}


def test_a_key_entered_after_startup_is_seen_without_a_restart(export_settings, monkeypatch) -> None:  # noqa: ANN001
    """The worker rebuilds its clients from the runtime settings every cycle."""
    from app.exporters.worker import ExportWorker

    started_without = export_settings.model_copy(update={"broadcastify_api_key": ""})
    worker = ExportWorker(settings=started_without, session_factory=lambda: None)
    assert not worker.clients[0].configured()
    seen: list[str] = []
    monkeypatch.setattr(worker, "runtime", lambda _session: export_settings)
    monkeypatch.setattr(worker, "_run_feed", lambda _s, _st: None)
    monkeypatch.setattr(
        worker, "_run_service", lambda _s, client, _st: seen.append(client.service) or 0
    )

    class _Session:
        def close(self) -> None:
            pass

    worker.session_factory = lambda: _Session()
    worker.cycle()
    assert seen[0] == "broadcastify"
    assert worker.clients[0].configured(), "the cycle must build clients from the runtime settings"


# --------------------------------------------------------------------------------------
# what the service said: call_exports.response / response_code
# --------------------------------------------------------------------------------------
#
# The ledger used to record the *outcome* of an attempt and throw the answer away, which
# lost the one number this deployment most wanted: 3 485 rows skipped as duplicates
# against 63 uploads.  Every attempt now stores the line, capped and redacted.


@pytest.mark.parametrize(
    ("body", "code", "stored", "bucket"),
    [
        (
            "0 https://upload.example.test/one-time/abc123?X-Amz-Signature=deadbeef\n",
            0,
            "0 abc123",
            "0 OK",
        ),
        (
            "1 SKIPPED---ALREADY-RECEIVED-THIS-CALL",
            1,
            "1 SKIPPED---ALREADY-RECEIVED-THIS-CALL",
            "1 SKIPPED",
        ),
        (
            "1 REJECTED-CALL-SKEW-TOO-LONG-4172-See:https://www.broadcastify.com/skew",
            1,
            "1 REJECTED-CALL-SKEW-TOO-LONG-4172-See:https://www.broadcastify.com/skew",
            "1 REJECTED-CALL-SKEW-TOO-LONG",
        ),
        ("100 NO-API-KEY-SPECIFIED", 100, "100 NO-API-KEY-SPECIFIED", "100 NO-API-KEY-SPECIFIED"),
    ],
)
def test_every_response_class_is_stored_and_bucketed(body, code, stored, bucket) -> None:  # noqa: ANN001
    """The published table, row by row: what is kept and what it counts as.

    The success line keeps only the URL's last path segment — the rest is a presigning
    signature with a lifetime of minutes — and the skew rejection keeps its seconds, which
    are the interesting part of it, while the *bucket* drops them so the histogram has one
    entry per kind of answer rather than one per call.
    """
    assert stage1_record(body) == (code, stored)
    assert response_class(stored) == bucket


def test_a_stored_response_is_redacted_and_capped(export_settings) -> None:  # noqa: ANN001
    """It has never been seen echoing the key.  That is a claim about somebody else's server."""
    code, stored = stage1_record(f"7 SOMETHING-ODD-{export_settings.broadcastify_api_key}", export_settings)

    assert code == 7
    assert export_settings.broadcastify_api_key not in stored
    assert stored.endswith("***")
    assert len(stage1_record("9 " + "x" * 900, export_settings)[1]) == 500


def test_the_parser_hands_the_line_to_the_caller_on_every_refusal(export_settings) -> None:  # noqa: ANN001
    """Each exception carries the answer that caused it, ready for the ledger."""
    with pytest.raises(SkipCall) as duplicate:
        parse_stage1("1 SKIPPED---ALREADY-RECEIVED-THIS-CALL", export_settings)
    assert duplicate.value.response == "1 SKIPPED---ALREADY-RECEIVED-THIS-CALL"
    assert duplicate.value.response_code == 1

    with pytest.raises(PermanentExportError) as refused:
        parse_stage1("100 NO-API-KEY-SPECIFIED", export_settings)
    assert (refused.value.response, refused.value.response_code) == ("100 NO-API-KEY-SPECIFIED", 100)


def test_an_upload_records_what_the_service_answered(session, worker, tmp_path) -> None:  # noqa: ANN001
    """The success path: the ledger keeps the line, not only the identifier."""
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert (row.status, row.response, row.response_code) == ("uploaded", "0 remote-1", 0)


def test_a_duplicate_is_recorded_as_a_response_and_as_its_own_activity_kind(
    session, worker, tmp_path
) -> None:  # noqa: ANN001
    """``1 SKIPPED---ALREADY-RECEIVED-THIS-CALL``: not a failure, and worth counting.

    The skip reason is unchanged — it is what the historical rows say and what the
    duplicate tally matches on — and the activity line gets its own kind so the count does
    not depend on a string search.
    """
    from app.exporters.broadcastify import DUPLICATE_REASON

    worker.test_client.error = SkipCall(
        DUPLICATE_REASON,
        response="1 SKIPPED---ALREADY-RECEIVED-THIS-CALL",
        response_code=1,
    )
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "skipped"
    assert row.skip_reason == DUPLICATE_REASON
    assert (row.response, row.response_code) == ("1 SKIPPED---ALREADY-RECEIVED-THIS-CALL", 1)

    event = session.scalars(
        select(ActivityEvent).where(ActivityEvent.kind == "export.duplicate")
    ).all()
    assert [(e.level, e.message) for e in event] == [("info", DUPLICATE_REASON)]


def test_a_rejection_records_the_response_alongside_the_failure(session, worker, tmp_path) -> None:  # noqa: ANN001
    """A parked row still says what it was told — the skew and their help URL included."""
    line = "1 REJECTED-CALL-SKEW-TOO-LONG-4172-See:https://www.broadcastify.com/skew"
    worker.test_client.error = PermanentExportError(
        "rejected: CALL-SKEW-TOO-LONG", response=line, response_code=1
    )
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "failed"
    assert (row.response, row.response_code) == (line, 1)


def test_a_failure_that_never_heard_an_answer_leaves_the_response_alone(
    session, worker, tmp_path
) -> None:  # noqa: ANN001
    """A timeout is "we never heard", not "it said nothing" — and must not blank a line."""
    worker.test_client.error = ExportError("openmhz unreachable: timed out")
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.response is None
    assert row.response_code is None


# --------------------------------------------------------------------------------------
# the skew window: register first, skip what is already too late, newest first
# --------------------------------------------------------------------------------------


def bcfy_worker(export_settings, tmp_path, handler, **overrides):  # noqa: ANN001, ANN003, ANN201
    """An :class:`ExportWorker` driving a real Broadcastify client over a mock transport."""
    from app.db import SessionLocal

    settings = export_settings.model_copy(update=overrides) if overrides else export_settings
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = BroadcastifyClient(settings, client=http)
    built = ExportWorker(
        settings=settings,
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
    )
    built.test_client = client  # type: ignore[attr-defined]
    return built


def backdate_watermark(session, service: str, *, seconds: float) -> None:  # noqa: ANN001
    """Move one service's enable watermark back, so an older call is still exportable."""
    from app.exporters.gate import watermark_key

    row = session.get(Setting, watermark_key(service))
    row.value = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    session.commit()


def test_a_duplicate_is_answered_before_anything_is_transcoded(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """The point of the split: nine answers in ten are "already have it", and cost no ffmpeg."""
    from app.exporters import worker as worker_module

    encoded: list[Path] = []

    def never(path, **kw):  # noqa: ANN001, ANN003, ANN202, ARG001
        encoded.append(path)
        return b"AAC"

    monkeypatch.setattr(worker_module, "transcode_to_m4a", never)
    monkeypatch.setattr(worker_module, "transcode_to_mp3", never)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(200, text="1 SKIPPED---ALREADY-RECEIVED-THIS-CALL")

    built = bcfy_worker(export_settings, tmp_path, handler)
    enable(session, "broadcastify")
    built.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    built.cycle()

    # Stage 1 went out, stage 2 never did, and ffmpeg was never asked for anything.
    assert seen == ["POST"]
    assert encoded == []
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "skipped"
    assert row.skip_reason == DUPLICATE_REASON
    assert row.response == "1 SKIPPED---ALREADY-RECEIVED-THIS-CALL"


def test_a_registered_call_is_then_encoded_and_put(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """The accepted path still ends with the audio on the presigned URL."""
    from app.exporters import worker as worker_module

    order: list[str] = []
    monkeypatch.setattr(
        worker_module,
        "transcode_to_m4a",
        lambda path, **kw: (order.append("encode"), b"AAC-BYTES")[1],  # noqa: ARG005
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/call-upload":
            order.append("register")
            return httpx.Response(200, text="0 https://upload.example.test/one-time/abc123\n")
        order.append("put")
        assert request.content == b"AAC-BYTES"
        return httpx.Response(200)

    built = bcfy_worker(export_settings, tmp_path, handler)
    enable(session, "broadcastify")
    built.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    built.cycle()

    assert order == ["register", "encode", "put"]
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "uploaded"
    assert (row.remote_id, row.response, row.response_code) == ("abc123", "0 abc123", 0)


@pytest.mark.parametrize(
    ("skew", "registrations", "expected"),
    [
        # The window on (the deployment): one re-registration at once, inside the window,
        # then parked — a retry after the 30 s backoff could only ever be skipped as late.
        (15.0, 2, ("failed", 2)),
        # The window off: nothing to hurry for, and the ordinary backoff applies.
        (0.0, 1, ("pending", 1)),
    ],
)
def test_a_put_that_fails_after_a_good_register_keeps_the_register_line(
    session, export_settings, tmp_path, monkeypatch, skew, registrations, expected
) -> None:  # noqa: ANN001
    """Retryable, and the ledger still holds the only line the service gave us."""
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"AAC")
    registered: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/call-upload":
            registered.append("register")
            return httpx.Response(200, text="0 https://upload.example.test/one-time/abc123")
        return httpx.Response(503)

    built = bcfy_worker(export_settings, tmp_path, handler, broadcastify_skew_limit_s=skew)
    enable(session, "broadcastify")
    built.cycle()
    call = seed_call(session, root=tmp_path, priority="live")

    built.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert len(registered) == registrations
    assert (row.status, row.attempts) == expected
    assert row.response == "0 abc123"
    assert "PUT" in (row.last_error or "")


def test_a_call_past_the_skew_window_is_skipped_without_a_request(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """The cheap half of the fix: no HTTP, no ffmpeg, and a reason that names the delay."""
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", _explode)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("nothing may be sent for a call that is already too late")

    built = bcfy_worker(export_settings, tmp_path, handler, broadcastify_skew_limit_s=15.0)
    enable(session, "broadcastify")
    built.cycle()
    # The watermark is stamped at "now"; back it up so a call recorded a moment ago is
    # inside the export window and the *only* thing wrong with it is its age.
    backdate_watermark(session, "broadcastify", seconds=600)
    # 21.2 s of call age at the moment the worker looks: 18.2 s past the end of a 3 s call.
    call = seed_call(
        session,
        root=tmp_path,
        priority="live",
        started_at=datetime.now(UTC) - timedelta(seconds=21.2),
        duration=3.0,
    )

    built.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "skipped"
    assert row.skip_reason.startswith(LATE_REASON)
    # One decimal, and counted from the end of the transmission rather than its start.
    assert re.fullmatch(rf"{re.escape(LATE_REASON)} \(18\.\d s after call end\)", row.skip_reason)
    assert (row.response, row.response_code) == (None, None)


def test_the_skew_pre_check_keeps_a_margin_for_the_round_trip(export_settings) -> None:  # noqa: ANN001
    """14.5 s is inside a 15 s window on the wire only if the request is instant."""
    from app.exporters.worker import late_reason

    settings = export_settings.model_copy(update={"broadcastify_skew_limit_s": 15.0})

    class Row:
        def __init__(self, age: float) -> None:
            self.ended_at = datetime.now(UTC) - timedelta(seconds=age)
            self.started_at = self.ended_at - timedelta(seconds=3)
            self.duration_s = 3.0

    assert late_reason(Row(5.0), settings) is None
    assert late_reason(Row(13.5), settings) is None
    assert late_reason(Row(14.5), settings) is not None
    # A row with no ``ended_at`` falls back to the start plus the duration.
    naive = Row(20.0)
    naive.ended_at = None
    assert late_reason(naive, settings) is not None
    # Zero turns the check off and lets Broadcastify answer for itself.
    assert late_reason(Row(600.0), settings.model_copy(update={"broadcastify_skew_limit_s": 0.0})) is None


def test_broadcastify_claims_newest_first_and_openmhz_oldest_first(session, tmp_path) -> None:  # noqa: ANN001
    """A backlog worked oldest-first spends every attempt on calls that are already late."""
    now = datetime.now(UTC)
    calls = [
        seed_call(session, root=tmp_path, priority="live", started_at=now - timedelta(seconds=age))
        for age in (300, 200, 100)
    ]
    for service in ("broadcastify", "openmhz"):
        for call in calls:
            session.add(
                CallExport(
                    call_id=call.id,
                    service=service,
                    status="pending",
                    next_attempt_at=now - timedelta(seconds=60),
                )
            )
    session.commit()

    newest = ledger.claim(session, service="broadcastify", newest_first=True)
    assert newest.call_id == calls[-1].id
    session.rollback()
    oldest = ledger.claim(session, service="openmhz")
    assert oldest.call_id == calls[0].id
    session.rollback()


def test_run_gives_each_service_its_own_loop_and_stops_on_the_flag(export_settings, tmp_path) -> None:  # noqa: ANN001
    """One thread per call-export service, the feed in the caller's, and all of them stop."""
    from app.db import SessionLocal
    from app.models import CALL_EXPORT_SERVICES

    built = ExportWorker(
        settings=export_settings.model_copy(update={"export_poll_seconds": 0.01}),
        session_factory=SessionLocal,
        clients=[],
        audio_root=tmp_path,
    )
    stop = threading.Event()
    seen: list[tuple[str, str]] = []
    lock = threading.Lock()

    def record(service: str) -> float:
        with lock:
            seen.append((service, threading.current_thread().name))
            done = {name for name, _ in seen} >= {*CALL_EXPORT_SERVICES, "feed"}
        if done:
            stop.set()
        return 0.01

    built._service_pass = record  # type: ignore[method-assign]
    built._feed_pass = lambda: record("feed")  # type: ignore[method-assign]
    built.feed = type("NoFeed", (), {"shutdown": staticmethod(lambda: None)})()  # type: ignore[assignment]

    runner = threading.Thread(target=built.run, args=(stop,))
    runner.start()
    runner.join(timeout=10.0)

    assert not runner.is_alive(), "run() did not stop when the flag was set"
    threads = dict(seen)
    assert set(threads) >= {*CALL_EXPORT_SERVICES, "feed"}
    # Each service ran on a thread of its own, and the feed on the caller's.
    for service in CALL_EXPORT_SERVICES:
        assert threads[service] == f"export-{service}"
    assert threads["feed"] != threads[CALL_EXPORT_SERVICES[0]]


def test_a_service_loop_never_dies_on_an_exception(export_settings, tmp_path, caplog) -> None:  # noqa: ANN001
    """A pass that raises is logged and the loop goes round again."""
    from app.db import SessionLocal

    built = ExportWorker(
        settings=export_settings.model_copy(update={"export_poll_seconds": 0.01}),
        session_factory=SessionLocal,
        clients=[],
        audio_root=tmp_path,
    )
    stop = threading.Event()
    passes: list[int] = []

    def boom(service: str) -> float:  # noqa: ARG001
        passes.append(1)
        if len(passes) >= 3:
            stop.set()
        raise RuntimeError("the queue is on fire")

    built._service_pass = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        built._service_loop(stop, "broadcastify")

    assert len(passes) >= 3
    assert "the queue is on fire" in caplog.text


# --------------------------------------------------------------------------------------
# the Broadcastify claim registry (docs/EXPORTERS.md, "Recorder-direct upload")
# --------------------------------------------------------------------------------------
#
# The registry is what stops two uploaders publishing one call twice.  It cannot lean on
# Broadcastify's own duplicate detection, because that keys on a *whole-second* ``ts`` and
# two recorders can round the same transmission into different seconds.


class StagedClient(RecordingClient):
    """A Broadcastify-shaped client: it registers and PUTs in two steps, like the real one."""

    def __init__(self) -> None:
        super().__init__(service="broadcastify")
        self.registered: list[CallFacts] = []
        self.put: list[bytes] = []

    def register(self, facts: CallFacts):  # noqa: ANN201
        from app.exporters.broadcastify import Registration

        self.registered.append(facts)
        return Registration(
            url="https://upload.broadcastify.test/put/abc123?sig=x",
            response="0 abc123",
            response_code=0,
        )

    def put_audio(self, url: str, audio: bytes) -> None:  # noqa: ARG002
        self.put.append(audio)


@pytest.fixture
def staged_worker(session, export_settings, tmp_path, monkeypatch):  # noqa: ANN001, ARG001
    """An :class:`ExportWorker` whose one client speaks the two-stage Calls protocol."""
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a-bytes")
    client = StagedClient()
    built = ExportWorker(
        settings=export_settings,
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
    )
    built.test_client = client  # type: ignore[attr-defined]
    return built


def test_a_claim_is_granted_once_and_denied_after(session) -> None:  # noqa: ANN001
    """The first claimant wins; everybody else is told who holds it."""
    from app.exporters import claims

    first = claims.acquire(session, instance="rec-a", tgid=1701, ts=1_757_030_462)
    session.commit()
    assert first.granted is True
    assert first.holder == "rec-a"

    second = claims.acquire(session, instance="rec-b", tgid=1701, ts=1_757_030_462)
    session.commit()
    assert second.granted is False
    assert second.holder == "rec-a"
    assert second.claim_id == first.claim_id


def test_a_claim_is_idempotent_for_the_same_instance(session) -> None:  # noqa: ANN001
    """A retry — a redelivered push, a recorder that lost the answer — is not a loss."""
    from app.exporters import claims

    first = claims.acquire(session, instance="rec-a", tgid=1701, ts=1_757_030_462)
    session.commit()
    again = claims.acquire(session, instance="rec-a", tgid=1701, ts=1_757_030_462)
    session.commit()
    assert again.granted is True
    assert again.claim_id == first.claim_id


@pytest.mark.parametrize(("offset", "granted"), [(0, False), (2, False), (-2, False), (3, True)])
def test_the_claim_tolerance_is_two_seconds_either_way(session, offset, granted) -> None:  # noqa: ANN001
    """±2 s is the *rounding* tolerance, not a guess at how long a call is.

    Broadcastify's ``ts`` is a whole second and two recorders can disagree about which
    second a transmission began in; three seconds apart is a different call.
    """
    from app.exporters import claims

    claims.acquire(session, instance="rec-a", tgid=1701, ts=1_757_030_462)
    session.commit()
    other = claims.acquire(session, instance="rec-b", tgid=1701, ts=1_757_030_462 + offset)
    session.commit()
    assert other.granted is granted


def test_a_claim_on_another_talkgroup_is_untouched(session) -> None:  # noqa: ANN001
    """The lock and the lookup are per talkgroup: one busy channel blocks nothing else."""
    from app.exporters import claims

    claims.acquire(session, instance="rec-a", tgid=1701, ts=1_757_030_462)
    session.commit()
    other = claims.acquire(session, instance="rec-b", tgid=1102, ts=1_757_030_462)
    session.commit()
    assert other.granted is True


def test_pruning_drops_only_the_old_claims(session) -> None:  # noqa: ANN001
    """Two days, and the vacuum job is where it happens."""
    from app.exporters import claims
    from app.models import BcfyClaim

    fresh = claims.acquire(session, instance="rec-a", tgid=1701, ts=1_757_030_462)
    stale = claims.acquire(session, instance="rec-a", tgid=1102, ts=1_757_030_462)
    session.get(BcfyClaim, stale.claim_id).created_at = datetime.now(UTC) - timedelta(days=3)
    session.commit()

    assert claims.prune(session) == 1
    session.commit()
    assert session.get(BcfyClaim, stale.claim_id) is None
    assert session.get(BcfyClaim, fresh.claim_id) is not None


def test_the_worker_claims_before_it_registers(session, staged_worker, tmp_path) -> None:  # noqa: ANN001
    """keyup's own uploader is a claimant like any other, under the name ``keyup``."""
    from app.exporters import claims
    from app.models import BcfyClaim

    enable(session, "broadcastify")
    staged_worker.cycle()
    call = seed_call(session, root=tmp_path)
    assert staged_worker.cycle()["broadcastify"] == 1

    held = session.scalars(select(BcfyClaim)).all()
    assert [row.instance for row in held] == [claims.KEYUP]
    assert held[0].ts == int(call.started_at.timestamp())
    # …and the claim is closed with what happened, so the registry reads as "who uploaded
    # what" rather than only "who asked".
    assert held[0].outcome == "uploaded"
    assert held[0].call_id == call.id


def test_the_worker_skips_a_call_a_recorder_has_claimed(session, staged_worker, tmp_path) -> None:  # noqa: ANN001
    """The whole point: keyup stands down rather than publishing the call a second time."""
    from app.exporters import claims
    from app.models import CallExport

    enable(session, "broadcastify")
    staged_worker.cycle()
    call = seed_call(session, root=tmp_path)
    claims.acquire(
        session,
        instance="trunkd-central-01",
        tgid=1701,
        ts=int(call.started_at.timestamp()),
    )
    session.commit()

    assert staged_worker.cycle()["broadcastify"] == 0
    # Nothing was sent — not even a registration, which is the round trip this saves.
    assert staged_worker.test_client.registered == []
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row.status == "skipped"
    assert row.skip_reason == f"{claims.CLAIMED_REASON} trunkd-central-01"
    assert row.response is None


def test_a_keyup_upload_is_stamped_with_who_sent_it(session, staged_worker, tmp_path) -> None:  # noqa: ANN001
    """``uploaded_by`` is what separates the two paths on the Exports card."""
    from app.exporters import claims
    from app.models import CallExport

    enable(session, "broadcastify")
    staged_worker.cycle()
    call = seed_call(session, root=tmp_path)
    staged_worker.cycle()

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row.status == "uploaded"
    assert row.uploaded_by == claims.KEYUP


def test_a_private_call_follows_its_radios_affiliated_talkgroup(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Every TRANSIT talkgroup deselected for the feed used to leave a TRANSIT base's private
    calls on it anyway: a private call has no talkgroup for the checkbox to bite on.  Its
    radios do — their latest group affiliation — so that talkgroup's flag decides."""
    from datetime import UTC, datetime

    from app.models import RadioAffiliation

    call = seed_call(session, root=tmp_path, with_talkgroup=False, radio_id=5238001, priority="live")
    system_id = call.system_id
    callee = Radio(system_id=system_id, radio_id=5238104)
    session.add(callee)
    session.flush()
    call.kind = "unit"
    call.target_radio_id = callee.id
    transit = Talkgroup(system_id=system_id, tgid=1551, alias="TRANSIT OPS", export_broadcastify=False)
    session.add(transit)
    session.flush()
    settings = export_settings.model_copy(update={"export_broadcastify_talkgroup_mode": "allow"})

    # No affiliation on record: nothing to go on, the plain switch decides (still allowed).
    assert gate.selected(session, call, settings, "broadcastify_feed") is True

    session.add(
        RadioAffiliation(
            radio_id_fk=call.radio_id, talkgroup_id_fk=transit.id, call_count=5, last_heard_at=datetime.now(UTC)
        )
    )
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is False
    assert gate.excluded_reason(session, call, settings, "broadcastify_feed") == "talkgroup not selected for export"

    transit.export_broadcastify = True
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is True


def _private_call_pair(session, tmp_path):  # noqa: ANN001, ANN202
    """A unit-to-unit call between two directory radios, and an allow-mode feed setting."""
    call = seed_call(session, root=tmp_path, with_talkgroup=False, radio_id=5238001, priority="live")
    callee = Radio(system_id=call.system_id, radio_id=5238104)
    session.add(callee)
    session.flush()
    call.kind = "unit"
    call.target_radio_id = callee.id
    on = Talkgroup(system_id=call.system_id, tgid=1701, alias="SHERIFF", export_broadcastify=True)
    off = Talkgroup(system_id=call.system_id, tgid=1551, alias="TRANSIT OPS", export_broadcastify=False)
    session.add_all([on, off])
    session.flush()
    return call, on, off


def test_a_private_call_follows_the_target_radios_talkgroup_too(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Either side deselected keeps the call off; the source on a selected talkgroup does not rescue it."""
    from app.models import RadioAffiliation

    call, on, off = _private_call_pair(session, tmp_path)
    settings = export_settings.model_copy(update={"export_broadcastify_talkgroup_mode": "allow"})
    now = datetime.now(UTC)
    session.add_all(
        [
            RadioAffiliation(radio_id_fk=call.radio_id, talkgroup_id_fk=on.id, last_heard_at=now),
            RadioAffiliation(radio_id_fk=call.target_radio_id, talkgroup_id_fk=off.id, last_heard_at=now),
        ]
    )
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is False
    off.export_broadcastify = True
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is True


def test_a_private_call_follows_each_radios_latest_affiliation(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """Only a radio's most recent affiliation counts; one never heard (NULL) sorts last."""
    from app.models import RadioAffiliation

    call, on, off = _private_call_pair(session, tmp_path)
    settings = export_settings.model_copy(update={"export_broadcastify_talkgroup_mode": "allow"})
    now = datetime.now(UTC)
    older = RadioAffiliation(
        radio_id_fk=call.radio_id, talkgroup_id_fk=off.id, last_heard_at=now - timedelta(hours=2)
    )
    newer = RadioAffiliation(radio_id_fk=call.radio_id, talkgroup_id_fk=on.id, last_heard_at=now)
    session.add_all([older, newer])
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is True

    # Move the deselected one to the front: now it is the latest and it decides.
    older.last_heard_at = now + timedelta(minutes=1)
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is False

    # A NULL last_heard_at sorts after every heard row, so it cannot outrank one.
    older.last_heard_at = None
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is True

    # …but a radio whose only row is NULL is still judged by it.
    session.add(RadioAffiliation(radio_id_fk=call.target_radio_id, talkgroup_id_fk=off.id, last_heard_at=None))
    session.flush()
    assert gate.selected(session, call, settings, "broadcastify_feed") is False


def test_a_private_call_is_gated_in_one_query(session, export_settings, tmp_path) -> None:  # noqa: ANN001
    """O21: both radios' latest affiliation and its talkgroup flag come back in one
    statement, where the gate used to ask twice per radio (four for a two-radio call)."""
    from sqlalchemy import event

    from app.models import RadioAffiliation

    call, on, off = _private_call_pair(session, tmp_path)
    settings = export_settings.model_copy(update={"export_broadcastify_talkgroup_mode": "allow"})
    now = datetime.now(UTC)
    session.add_all(
        [
            RadioAffiliation(radio_id_fk=call.radio_id, talkgroup_id_fk=on.id, last_heard_at=now),
            RadioAffiliation(radio_id_fk=call.target_radio_id, talkgroup_id_fk=off.id, last_heard_at=now),
        ]
    )
    session.flush()
    # Nothing in the identity map to answer from: the old code's session.get then had to
    # go to the database for each talkgroup, as it does in the worker.
    session.expunge(on)
    session.expunge(off)

    statements: list[str] = []
    bind = session.connection()

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001, ARG001
        statements.append(statement)

    event.listen(bind, "before_cursor_execute", record)
    try:
        assert gate.selected(session, call, settings, "broadcastify_feed") is False
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert len(statements) == 1, statements


# --------------------------------------------------------------------------------------
# The claim is its own short transaction; Broadcastify retries fit the skew window
# --------------------------------------------------------------------------------------


def _staged_with(export_settings, tmp_path, monkeypatch, client, **overrides):  # noqa: ANN001, ANN003, ANN202
    """An :class:`ExportWorker` driving ``client`` (a two-stage stand-in), transcode stubbed."""
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a-bytes")
    settings = export_settings.model_copy(update=overrides) if overrides else export_settings
    return ExportWorker(
        settings=settings,
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
        sleep=lambda _s: None,
    )


class LockProbeClient(StagedClient):
    """Checks, from other connections, what the worker is holding while it sends."""

    def __init__(self) -> None:
        super().__init__()
        self.lock_free: list[bool] = []
        self.claim_visible: list[int] = []
        self.recorder_waited: list[float] = []

    def register(self, facts: CallFacts):  # noqa: ANN201
        from sqlalchemy import func, text

        from app.db import SessionLocal
        from app.exporters import claims
        from app.models import BcfyClaim

        probe = SessionLocal()
        try:
            free = probe.scalar(
                text("SELECT pg_try_advisory_xact_lock(:ns, :tg)"),
                {"ns": claims.LOCK_NAMESPACE, "tg": facts.talkgroup},
            )
            visible = probe.scalar(
                select(func.count())
                .select_from(BcfyClaim)
                .where(BcfyClaim.tgid == facts.talkgroup, BcfyClaim.instance == claims.KEYUP)
            )
            self.lock_free.append(bool(free))
            self.claim_visible.append(int(visible or 0))
        finally:
            probe.rollback()
            probe.close()
        return super().register(facts)

    def put_audio(self, url: str, audio: bytes) -> None:
        from app.db import SessionLocal
        from app.exporters import claims

        # A recorder claiming the *next* call on the same talkgroup, mid-upload.
        other = SessionLocal()
        try:
            started = time.monotonic()
            held = claims.acquire(other, instance="trunkd-a", tgid=1701, ts=int(time.time()) + 60)
            other.commit()
            self.recorder_waited.append(time.monotonic() - started)
            assert held.granted
        finally:
            other.close()
        super().put_audio(url, audio)


def test_the_claim_lock_is_not_held_across_the_upload(session, export_settings, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """E2: the per-talkgroup lock is released — and the claim committed — before register."""
    client = LockProbeClient()
    built = _staged_with(export_settings, tmp_path, monkeypatch, client)
    enable(session, "broadcastify")
    built.cycle()
    call = seed_call(session, root=tmp_path)

    assert built.cycle()["broadcastify"] == 1

    assert client.lock_free == [True]
    # Committed, so another connection sees it: the grant is exclusive before we send.
    assert client.claim_visible == [1]
    # A recorder's claim on the same talkgroup during our PUT did not wait on us.
    assert len(client.recorder_waited) == 1
    assert client.recorder_waited[0] < 0.5
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    assert row.status == "uploaded"


def test_a_busy_claim_lock_gives_up_within_the_timeout(session) -> None:  # noqa: ANN001, ARG001
    """A claimant never waits on another's lock for longer than LOCK_TIMEOUT_MS."""
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.exporters import claims

    holder = SessionLocal()
    waiter = SessionLocal()
    try:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :tg)"), {"ns": claims.LOCK_NAMESPACE, "tg": 1701}
        )
        started = time.monotonic()
        with pytest.raises(claims.ClaimBusy):
            claims.acquire(waiter, instance="rec-a", tgid=1701, ts=1_757_030_462)
        waited = time.monotonic() - started
        waiter.rollback()
        assert 0.8 <= waited < 2.0, waited
        # …and the session is usable again after the rollback, timeouts gone with it.
        holder.rollback()
        held = claims.acquire(waiter, instance="rec-a", tgid=1701, ts=1_757_030_462)
        waiter.commit()
        assert held.granted
    finally:
        holder.close()
        waiter.close()


def _claim_row(session, claim_id: int):  # noqa: ANN001, ANN202
    from app.models import BcfyClaim

    return session.get(BcfyClaim, claim_id)


def test_a_failed_recorder_claim_is_released_for_keyup(session) -> None:  # noqa: ANN001
    """E1: keyup is the retry path, so the recorder's `failed` claim must not block it."""
    from app.exporters import claims

    first = claims.acquire(session, instance="trunkd-a", tgid=1701, ts=1_757_030_462)
    session.commit()
    claims.finish(session, first.claim_id, outcome="failed", response="PUT: Peer disconnected")
    session.commit()

    taken = claims.acquire(session, instance=claims.KEYUP, tgid=1701, ts=1_757_030_463)
    session.commit()
    assert (taken.granted, taken.holder, taken.taken_over_from) == (True, claims.KEYUP, "trunkd-a")
    assert taken.claim_id != first.claim_id
    # The recorder's row is kept: the registry still shows who asked first.
    assert _claim_row(session, first.claim_id).instance == "trunkd-a"

    # keyup is idempotent on its own takeover…
    again = claims.acquire(session, instance=claims.KEYUP, tgid=1701, ts=1_757_030_462)
    session.commit()
    assert (again.granted, again.claim_id) == (True, taken.claim_id)
    # …and keyup, not the failed recorder, now holds it against everybody else.
    for instance in ("trunkd-a", "trunkd-b"):
        other = claims.acquire(session, instance=instance, tgid=1701, ts=1_757_030_462)
        session.commit()
        assert (other.granted, other.holder) == (False, claims.KEYUP)


@pytest.mark.parametrize(
    ("age_s", "outcome", "granted"),
    [
        (5, None, False),  # a recorder still in the middle of its upload
        (31, None, True),  # silent past STALE_S: it crashed or lost the call
        (31, "uploaded", False),  # it answered, and the answer was "sent"
        (31, "duplicate", False),
    ],
)
def test_a_silent_recorder_claim_goes_stale_for_keyup(session, age_s, outcome, granted) -> None:  # noqa: ANN001
    """E1, window off: a recorder that claimed and never came back cannot block keyup for ever."""
    from app.exporters import claims

    first = claims.acquire(session, instance="trunkd-a", tgid=1701, ts=1_757_030_462)
    row = _claim_row(session, first.claim_id)
    row.created_at = datetime.now(UTC) - timedelta(seconds=age_s)
    row.outcome = outcome
    session.commit()

    held = claims.acquire(session, instance=claims.KEYUP, tgid=1701, ts=1_757_030_462)
    session.commit()
    assert held.granted is granted
    # Only keyup takes a released claim over; a second recorder is still told no.
    other = claims.acquire(session, instance="trunkd-b", tgid=1701, ts=1_757_030_462)
    session.commit()
    assert other.granted is False


def _push_failed(session, export_settings, call: Call, *, instance: str = "trunkd-a") -> None:  # noqa: ANN001
    """What a recorder's call push with ``broadcastifyOutcome=failed`` writes."""
    from app.api.call_upload import record_broadcastify_outcome

    record_broadcastify_outcome(
        session,
        call,
        {
            "broadcastifyOutcome": "failed",
            "broadcastifyResponse": "PUT: Peer disconnected",
            "talkgroup": "1701",
            "dateTime": str(int(call.started_at.timestamp())),
            "recorder": instance,
        },
        export_settings,
    )
    session.commit()


def test_a_recorder_failed_row_is_retried_once_inside_the_window(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """E1: the recorder's failed PUT becomes a real keyup attempt, not a row that can never send."""
    from app.exporters import claims
    from app.models import BcfyClaim

    client = StagedClient()
    built = _staged_with(
        export_settings, tmp_path, monkeypatch, client, broadcastify_skew_limit_s=15.0
    )
    enable(session, "broadcastify")
    built.cycle()
    call = seed_call(session, root=tmp_path)
    claims.acquire(session, instance="trunkd-a", tgid=1701, ts=int(call.started_at.timestamp()))
    session.commit()
    _push_failed(session, export_settings, call)

    assert built.cycle()["broadcastify"] == 1

    assert len(client.registered) == 1
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert (row.status, row.uploaded_by) == ("uploaded", claims.KEYUP)
    held = session.scalars(select(BcfyClaim).order_by(BcfyClaim.id)).all()
    assert [(c.instance, c.outcome) for c in held] == [("trunkd-a", "failed"), (claims.KEYUP, "uploaded")]


def test_a_recorder_failed_row_past_the_window_is_skipped_as_late(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """…or, when the push came in too late for any retry, a clean late skip — no request."""
    from app.exporters import claims

    client = StagedClient()
    built = _staged_with(
        export_settings, tmp_path, monkeypatch, client, broadcastify_skew_limit_s=15.0
    )
    enable(session, "broadcastify")
    built.cycle()
    backdate_watermark(session, "broadcastify", seconds=600)
    call = seed_call(
        session, root=tmp_path, started_at=datetime.now(UTC) - timedelta(seconds=30), duration=3.0
    )
    claims.acquire(session, instance="trunkd-a", tgid=1701, ts=int(call.started_at.timestamp()))
    session.commit()
    _push_failed(session, export_settings, call)

    assert built.cycle()["broadcastify"] == 0

    assert client.registered == []
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "skipped"
    assert row.skip_reason.startswith(LATE_REASON)


class FlakyPutClient(StagedClient):
    """A PUT that fails ``failures`` times, then goes through."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def put_audio(self, url: str, audio: bytes) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ExportError("audio PUT failed: Peer disconnected")
        super().put_audio(url, audio)


def test_a_failed_broadcastify_send_is_retried_at_once_inside_the_window(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """One immediate re-registration, same claim, one encode — and it lands."""
    from app.exporters import claims
    from app.models import BcfyClaim

    client = FlakyPutClient(failures=1)
    built = _staged_with(
        export_settings, tmp_path, monkeypatch, client, broadcastify_skew_limit_s=15.0
    )
    enable(session, "broadcastify")
    built.cycle()
    call = seed_call(session, root=tmp_path)

    assert built.cycle()["broadcastify"] == 1

    assert len(client.registered) == 2
    assert len(client.put) == 1
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "uploaded"
    held = session.scalars(select(BcfyClaim)).all()
    assert [(c.instance, c.outcome) for c in held] == [(claims.KEYUP, "uploaded")]


def test_the_recorder_claim_route_answers_busy_not_denied(session) -> None:  # noqa: ANN001, ARG001
    """A busy registry must read to a recorder as "unanswered" (it uploads), never as "denied"."""
    from fastapi import HTTPException
    from sqlalchemy import text

    from app.api.recorders import claim_broadcastify
    from app.db import SessionLocal
    from app.exporters import claims
    from app.schemas import BroadcastifyClaimIn

    holder = SessionLocal()
    db = SessionLocal()
    try:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :tg)"), {"ns": claims.LOCK_NAMESPACE, "tg": 1701}
        )
        body = BroadcastifyClaimIn(instance_id="rec-a", tgid=1701, ts=1_757_030_462, duration_s=3.0)
        started = time.monotonic()
        with pytest.raises(HTTPException) as raised:
            claim_broadcastify(db=db, token=None, body=body)
        assert time.monotonic() - started < 2.0
        assert raised.value.status_code == 503
        assert raised.value.headers == {"Retry-After": "1"}
    finally:
        holder.rollback()
        holder.close()
        db.close()


# --------------------------------------------------------------------------------------
# Ambiguous OpenMHz sends, outcome writes that fail, and repoint under the lock
# --------------------------------------------------------------------------------------


def _omhz_worker(export_settings, tmp_path, monkeypatch, handler):  # noqa: ANN001, ANN202
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a-bytes")
    client = OpenMhzClient(export_settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    return ExportWorker(
        settings=export_settings,
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
        sleep=lambda _s: None,
    )


def _make_due(session, call_id: int) -> None:  # noqa: ANN001
    from sqlalchemy import update

    session.execute(
        update(CallExport)
        .where(CallExport.call_id == call_id)
        .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    session.commit()


def _mongo_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.mark.parametrize("lookup", ["found", "absent", "broken"])
def test_an_ambiguous_openmhz_send_is_looked_up_before_it_is_sent_again(
    session, export_settings, tmp_path, monkeypatch, lookup
) -> None:  # noqa: ANN001
    """E3: a read timeout after the body went out is never re-posted blind."""
    from app.exporters.ledger import AMBIGUOUS_PREFIX, VERIFY_DELAY_S

    posts: list[int] = []
    lookups: list[httpx.Request] = []
    state = {"timeout": True, "start": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(1)
            if state["timeout"]:
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200)
        lookups.append(request)
        if lookup == "broken":
            return httpx.Response(500, text="Sorry")
        calls = [
            # Same talkgroup, a different second: not ours.
            {"_id": "other", "talkgroupNum": 1701, "time": _mongo_time(state["start"] + 9)},
        ]
        if lookup == "found":
            calls.append({"_id": "65f0abc", "talkgroupNum": 1701, "time": _mongo_time(state["start"])})
        return httpx.Response(200, json={"calls": calls, "direction": "newer"})

    built = _omhz_worker(export_settings, tmp_path, monkeypatch, handler)
    enable(session, "openmhz")
    built.cycle()
    call = seed_call(session, root=tmp_path)
    state["start"] = int(call.started_at.timestamp())

    built.cycle()
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert (row.status, row.attempts, len(posts)) == ("pending", 1, 1)
    assert row.last_error.startswith(AMBIGUOUS_PREFIX)
    # Not due for a while: a call OpenMHz did take is listed only after its S3 put and save.
    assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=VERIFY_DELAY_S - 5)

    state["timeout"] = False
    _make_due(session, call.id)
    built.cycle()
    session.refresh(row)

    assert len(lookups) == 1
    assert lookups[0].url.path == "/examplesys/calls/newer"
    assert dict(lookups[0].url.params) == {
        "time": str(state["start"] * 1000 - 1),
        "filter-type": "talkgroup",
        "filter-code": "1701",
    }
    if lookup == "found":
        assert len(posts) == 1, "found on OpenMHz: must not be posted a second time"
        assert (row.status, row.remote_id) == ("uploaded", "65f0abc")
    elif lookup == "absent":
        assert len(posts) == 2
        assert row.status == "uploaded"
    else:
        # "Could not ask" is not "not there": still no second POST.
        assert len(posts) == 1
        assert (row.status, row.attempts) == ("pending", 2)
        assert row.last_error.startswith(AMBIGUOUS_PREFIX)


def test_an_ambiguous_send_with_no_way_to_check_is_parked_uncertain(session, worker, tmp_path) -> None:  # noqa: ANN001
    """A client that cannot look calls up never re-posts an ambiguous one."""
    from app.exporters.base import AmbiguousExportError
    from app.exporters.ledger import UNCERTAIN_REASON

    worker.test_client.error = AmbiguousExportError("no answer after the body was sent")
    enable(session, "openmhz")
    worker.cycle()
    call = seed_call(session, root=tmp_path)

    worker.cycle()
    _make_due(session, call.id)
    worker.cycle()

    assert len(worker.test_client.uploads) == 1
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert row.status == "skipped"
    assert row.skip_reason.startswith(UNCERTAIN_REASON)
    assert row.attempts == 1


@pytest.mark.parametrize(
    ("answer", "ambiguous"),
    [
        (httpx.ReadTimeout("read timed out"), True),
        (httpx.RemoteProtocolError("Server disconnected without sending a response."), True),
        (httpx.ConnectError("refused"), False),
        (httpx.WriteTimeout("write timed out"), False),
        (502, True),
        (504, True),
        (524, True),
        # The application itself answers 200 before it stores anything and refuses with a
        # 500 before it stores anything, so its own 500 is a clean "not stored".
        (500, False),
        (503, False),
    ],
)
def test_openmhz_classifies_what_is_ambiguous(export_settings, answer, ambiguous) -> None:  # noqa: ANN001
    from app.exporters.base import AmbiguousExportError

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            answer.request = request
            raise answer
        return httpx.Response(answer, text="upstream trouble")

    client = OpenMhzClient(export_settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ExportError) as raised:
        client.upload(facts(), b"m4a")
    assert isinstance(raised.value, AmbiguousExportError) is ambiguous


def _failing_once(original, calls: list[int], *, times: int = 1):  # noqa: ANN001, ANN202
    """Wrap a ledger writer so its first ``times`` calls fail the way the database does."""
    from sqlalchemy import text

    def wrapped(session, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        calls.append(1)
        if len(calls) <= times:
            session.execute(text("SELECT 1/0"))  # a real error: the transaction is now failed
        return original(session, *a, **k)

    return wrapped


def _plain_worker(export_settings, tmp_path, monkeypatch, client):  # noqa: ANN001, ANN202
    from app.db import SessionLocal
    from app.exporters import worker as worker_module

    monkeypatch.setattr(worker_module, "transcode_to_m4a", lambda *a, **k: b"m4a-bytes")
    return ExportWorker(
        settings=export_settings,
        session_factory=SessionLocal,
        clients=[client],
        audio_root=tmp_path,
        sleep=lambda _s: None,
    )


def test_a_failed_success_write_is_retried_not_re_uploaded(session, export_settings, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """E4: the call went out; a DB error recording that must not send it again."""
    from app.exporters import worker as worker_module

    client = RecordingClient()
    built = _plain_worker(export_settings, tmp_path, monkeypatch, client)
    enable(session, "openmhz")
    built.cycle()
    call = seed_call(session, root=tmp_path)
    writes: list[int] = []
    monkeypatch.setattr(
        worker_module.ledger, "mark_uploaded", _failing_once(ledger.mark_uploaded, writes)
    )

    built.cycle()
    built.cycle()

    assert len(client.uploads) == 1
    assert len(writes) == 2
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert (row.status, row.remote_id) == ("uploaded", "remote-1")
    logged = session.scalars(select(ActivityEvent).where(ActivityEvent.kind == "export.uploaded")).all()
    assert len(logged) == 1


def test_an_unwritable_success_is_remembered_and_never_re_sent(session, export_settings, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Even when every write fails, the next time the row comes up it is recorded, not sent."""
    from app.exporters import worker as worker_module

    client = RecordingClient()
    built = _plain_worker(export_settings, tmp_path, monkeypatch, client)
    enable(session, "openmhz")
    built.cycle()
    call = seed_call(session, root=tmp_path)
    writes: list[int] = []
    original = ledger.mark_uploaded
    monkeypatch.setattr(
        worker_module.ledger, "mark_uploaded", _failing_once(original, writes, times=100)
    )

    built.cycle()
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    # Still pending and still due — and still not sent twice in the same pass.
    assert row.status == "pending"
    assert len(client.uploads) == 1

    monkeypatch.setattr(worker_module.ledger, "mark_uploaded", original)
    built.cycle()
    session.refresh(row)
    assert row.status == "uploaded"
    assert len(client.uploads) == 1


def test_a_failed_failure_write_backs_off_and_is_not_reclaimed_in_the_pass(
    session, export_settings, tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """E4: when `_fail` itself cannot write, the row is bumped in a fresh transaction."""
    from app.exporters import worker as worker_module

    client = RecordingClient(error=ExportError("openmhz returned HTTP 500"))
    built = _plain_worker(export_settings, tmp_path, monkeypatch, client)
    enable(session, "openmhz")
    built.cycle()
    call = seed_call(session, root=tmp_path)
    writes: list[int] = []
    monkeypatch.setattr(
        worker_module.ledger, "mark_failed", _failing_once(ledger.mark_failed, writes, times=100)
    )

    built.cycle()

    # One attempt in the pass, not a batch of them.
    assert len(client.uploads) == 1
    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    session.refresh(row)
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=20)
    assert "HTTP 500" in (row.last_error or "")


def test_the_batch_loop_never_reclaims_a_row_it_handled(session, export_settings, tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A row left due by a failed write is not taken straight back in the same pass."""
    client = RecordingClient()
    built = _plain_worker(export_settings, tmp_path, monkeypatch, client)
    enable(session, "openmhz")
    built.cycle()
    call = seed_call(session, root=tmp_path)
    built.cycle()
    assert len(client.uploads) == 1

    row = session.scalar(select(CallExport).where(CallExport.call_id == call.id))
    first = ledger.claim(session, service="openmhz", exclude={row.id})
    assert first is None


def test_repoint_does_not_move_a_row_the_worker_just_uploaded(session, tmp_path) -> None:  # noqa: ANN001
    """E5: repoint waits on the worker's lock, then acts on the status it finds *after* it."""
    from sqlalchemy import func, text

    from app.db import SessionLocal

    loser = seed_call(session, root=tmp_path, file_path="20260901/EXAMPLE EVENT 1/loser.mp3")
    winner = seed_call(session, root=tmp_path, file_path="20260901/EXAMPLE EVENT 1/winner.mp3")
    session.add(CallExport(call_id=loser.id, service="openmhz", status="pending"))
    session.commit()
    loser_id, winner_id = loser.id, winner.id

    worker_side = SessionLocal()
    claimed = ledger.claim(worker_side, service="openmhz")
    assert claimed is not None and claimed.call_id == loser_id

    errors: list[BaseException] = []

    def reconcile_side() -> None:
        other = SessionLocal()
        try:
            other.execute(text("SET LOCAL lock_timeout = '15s'"))
            # Read the rows into the identity map first, the way a reconcile pass that
            # already looked at them would: populate_existing is what refreshes them.
            list(other.scalars(select(CallExport).where(CallExport.call_id == loser_id)))
            ledger.repoint_exports(other, winner_id=winner_id, loser_id=loser_id)
            other.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            other.rollback()
        finally:
            other.close()

    thread = threading.Thread(target=reconcile_side)
    thread.start()
    deadline = time.monotonic() + 5
    watcher = SessionLocal()
    try:
        while time.monotonic() < deadline:
            waiting = watcher.scalar(
                select(func.count()).select_from(text("pg_stat_activity")).where(
                    text("wait_event_type = 'Lock' AND datname = current_database()")
                )
            )
            watcher.rollback()
            if waiting:
                break
            time.sleep(0.05)
    finally:
        watcher.close()

    ledger.mark_uploaded(worker_side, claimed.export_id, remote_id="r1", uploaded_by="keyup")
    worker_side.commit()
    worker_side.close()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert errors == []

    rows = {
        (r.call_id, r.status, r.skip_reason)
        for r in session.scalars(select(CallExport).execution_options(populate_existing=True))
    }
    assert rows == {
        (loser_id, "uploaded", None),
        (winner_id, "skipped", ledger.duplicate_upload_reason(loser_id)),
    }
