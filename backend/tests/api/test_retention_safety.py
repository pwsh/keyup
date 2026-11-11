"""Retention safety through the API and the schema: archive paths are checked, remembered and served;
the four FK-side indexes exist and are usable by the per-row FK action.

The retention pass itself is covered in ``tests/test_retention.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from tests.api.conftest import bearer
from tests.conftest import BACKEND_DIR, requires_db

pytestmark = requires_db

MIGRATION = "c4e9a1d07b35"
BEFORE = "0ebc81310604"
FK_INDEXES = {
    "ix_activity_events_call_id": "activity_events",
    "ix_conversations_first_call_id": "conversations",
    "ix_recorder_gaps_resumed_call_id": "recorder_gaps",
    "ix_notifications_call_id": "notifications",
}


@pytest.fixture
def allowed(tmp_path: Path, monkeypatch) -> Path:  # noqa: ANN001
    """``RETENTION_ARCHIVE_ALLOWED_ROOTS`` pointed at a temporary tree for one test."""
    from app import runtime_settings
    from app.config import get_settings

    root = tmp_path / "srv"
    root.mkdir()
    monkeypatch.setenv("RETENTION_ARCHIVE_ALLOWED_ROOTS", str(root))
    get_settings.cache_clear()
    runtime_settings.invalidate()
    yield root
    get_settings.cache_clear()
    runtime_settings.invalidate()


def _history(session: Session) -> list[str]:
    from app.models import Setting

    session.expire_all()
    row = session.get(Setting, "retention_archive_roots")
    return list(row.value) if row is not None else []


def test_a_policy_archive_path_is_checked_and_remembered(client, session, admin_token, allowed) -> None:  # noqa: ANN001
    outside = allowed.parent / "elsewhere"
    outside.mkdir()
    for bad in (str(outside), str(allowed / "not-mounted"), "relative/path"):
        response = client.post(
            "/api/admin/retention/policies",
            json={"scope": "global", "keep_audio_days": 30, "archive_path": bad},
            headers=bearer(admin_token),
        )
        assert response.status_code == 422, (bad, response.text)
        assert response.json()["detail"].startswith("archive_path:")
    assert _history(session) == []

    good = allowed / "archive"
    good.mkdir()
    saved = client.post(
        "/api/admin/retention/policies",
        json={"scope": "global", "keep_audio_days": 30, "archive_path": f"{good}/"},
        headers=bearer(admin_token),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()[0]["archive_path"] == str(good.resolve())
    assert _history(session) == [str(good.resolve())]


def test_the_global_archive_path_setting_is_checked_and_its_past_values_kept(  # noqa: ANN201
    client,  # noqa: ANN001
    session,  # noqa: ANN001
    admin_token,  # noqa: ANN001
    allowed,  # noqa: ANN001
):
    first, second = allowed / "first", allowed / "second"
    first.mkdir()
    second.mkdir()

    refused = client.put(
        "/api/admin/settings",
        json={"retention_archive_path": str(allowed.parent)},
        headers=bearer(admin_token),
    )
    assert refused.status_code == 422, refused.text

    for path in (first, second):
        assert client.put(
            "/api/admin/settings", json={"retention_archive_path": str(path)}, headers=bearer(admin_token)
        ).status_code == 200
    assert client.put(
        "/api/admin/settings", json={"retention_archive_path": ""}, headers=bearer(admin_token)
    ).status_code == 200

    assert _history(session) == [str(first.resolve()), str(second.resolve())]


def test_audio_archived_under_a_policy_path_plays(client, session, archive, allowed) -> None:  # noqa: ANN001
    """R1: the route resolves with the runtime settings, whose roots include the history."""
    from app import runtime_settings
    from app.config import get_settings
    from app.models import Call
    from app.retention import remember_archive_root

    policy_archive = allowed / "policy"
    policy_archive.mkdir()
    call = session.get(Call, archive.call_ids[0])
    source = Path(get_settings().audio_root) / call.file_path
    moved = policy_archive / f"{call.id}-{source.name}"
    shutil.copyfile(source, moved)
    call.file_path = str(moved)
    call.audio_state = "archived"
    session.commit()

    # Not a known archive root yet: outside every allowed root, so 404.
    assert client.get(f"/api/calls/{call.id}/audio?raw=1").status_code == 404

    remember_archive_root(session, str(policy_archive))
    session.commit()
    runtime_settings.invalidate()
    response = client.get(f"/api/calls/{call.id}/audio?raw=1")
    assert response.status_code == 200
    assert response.content == moved.read_bytes()


def test_the_fk_side_indexes_exist_and_serve_the_fk_action(session) -> None:  # noqa: ANN001
    """The per-row statement a call DELETE runs can use each index.

    The ``ON DELETE SET NULL`` action runs ``UPDATE … WHERE $1 = <column>`` with a
    parameter, so the plan is checked in its generic form — which is also what proves the
    partial ``activity_events`` index is usable when the value is not a literal.
    """
    rows = session.execute(
        text(
            "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = ANY(:names)"
        ),
        {"names": list(FK_INDEXES)},
    ).all()
    assert {name: valid for name, valid in rows} == dict.fromkeys(FK_INDEXES, True)

    columns = {
        "activity_events": "call_id",
        "conversations": "first_call_id",
        "recorder_gaps": "resumed_call_id",
        "notifications": "call_id",
    }
    session.execute(text("SET LOCAL enable_seqscan = off"))
    session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    for index, table in FK_INDEXES.items():
        column = columns[table]
        session.execute(text(f"PREPARE fk_probe(bigint) AS UPDATE {table} SET {column} = NULL WHERE $1 = {column}"))
        try:
            plan = "\n".join(r[0] for r in session.execute(text("EXPLAIN EXECUTE fk_probe(1)")))
        finally:
            session.execute(text("DEALLOCATE fk_probe"))
        assert index in plan, plan
    session.rollback()


def test_the_migration_goes_down_and_up(migrated_database) -> None:  # noqa: ANN001, ARG001
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command
    from app.db import engine

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    def state() -> tuple[set[str], bool]:
        engine.dispose()
        inspector = inspect(engine)
        names = {ix["name"] for table in set(FK_INDEXES.values()) for ix in inspector.get_indexes(table)}
        return names & set(FK_INDEXES), "disk_samples" in inspector.get_table_names()

    assert state() == (set(FK_INDEXES), True)
    command.downgrade(config, BEFORE)
    try:
        assert state() == (set(), False)
    finally:
        command.upgrade(config, "head")
    assert state() == (set(FK_INDEXES), True)
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        states = conn.execute(text("SELECT unnest(enum_range(NULL::audio_state))::text")).scalars().all()
        kinds = conn.execute(text("SELECT unnest(enum_range(NULL::alert_kind))::text")).scalars().all()
    script = ScriptDirectory.from_config(config)
    # back at head, with this migration on the way to it (later ones may sit above it)
    assert current == script.get_current_head()
    assert MIGRATION in {rev.revision for rev in script.walk_revisions()}
    assert "missing" in states and "disk_low" in kinds
