"""Every migration can be undone and done again.

The ``migrated_database`` fixture proves ``upgrade head`` works on an empty database; nothing
proved the way back.  This walks the whole chain on a database of its own, so it never
disturbs the session-wide test schema:

1. ``upgrade head`` and record the schema;
2. one revision at a time, newest first: downgrade past it, upgrade it again, downgrade past
   it again — so each revision's ``downgrade`` is shown to leave a schema its own ``upgrade``
   accepts (the classic failure is a re-run ``CREATE`` of something the downgrade forgot);
3. at ``base`` nothing of Keyup's is left but ``alembic_version`` (and ``pg_trgm``, which
   the fuzzy-search downgrade deliberately keeps — see ``ec34a3a39b74``);
4. ``upgrade head`` again, and the schema is identical to step 1's.

Some downgrades are lossy by design and pass here all the same: enum labels stay (PostgreSQL
has no ``DROP VALUE``; every ``ADD VALUE`` is ``IF NOT EXISTS`` so the re-upgrade accepts
them), and the two alert-rule rewrites (``0ebc81310604``, ``3216a949614d``) leave the rules
in the new form because the old matcher read both.  Data is not round-tripped; the schema is.
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL, requires_db

pytestmark = requires_db

#: What ``downgrade base`` is allowed to leave behind in ``public``.
BASE_LEFTOVERS = {("table", "alembic_version")}


def _with_database(url: str, name: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{name}"))


@pytest.fixture
def scratch_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh database beside the test database, dropped afterwards.

    ``alembic/env.py`` reads ``DATABASE_URL`` through a new ``Settings()`` on every run, so
    pointing the variable at the scratch database is all the redirection needed.
    """
    base = urlparse(TEST_DATABASE_URL).path.lstrip("/") or "keyup_test"
    name = f"{base}_roundtrip"
    admin = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _with_database(TEST_DATABASE_URL, name)
    monkeypatch.setenv("DATABASE_URL", url)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _schema(url: str) -> dict[str, list[tuple]]:
    """Everything in ``public`` a migration can create, in a comparable form."""
    queries = {
        "columns": """
            SELECT table_name, column_name, data_type, udt_name, is_nullable,
                   coalesce(column_default, ''), coalesce(character_maximum_length, 0)
            FROM information_schema.columns WHERE table_schema = 'public'
            ORDER BY 1, 2
        """,
        "indexes": """
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname = 'public' ORDER BY 1, 2
        """,
        "constraints": """
            SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid)
            FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = 'public' ORDER BY 1, 2
        """,
        "enums": """
            SELECT t.typname, e.enumlabel, e.enumsortorder
            FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'public' ORDER BY 1, 3
        """,
        "sequences": """
            SELECT sequence_name FROM information_schema.sequences
            WHERE sequence_schema = 'public' ORDER BY 1
        """,
        "views": """
            SELECT table_name FROM information_schema.views
            WHERE table_schema = 'public' ORDER BY 1
        """,
        "triggers": """
            SELECT event_object_table, trigger_name, action_timing, event_manipulation
            FROM information_schema.triggers WHERE trigger_schema = 'public' ORDER BY 1, 2, 4
        """,
        "functions": """
            SELECT p.proname, pg_get_function_identity_arguments(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
            WHERE n.nspname = 'public' AND d.objid IS NULL ORDER BY 1, 2
        """,
    }
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {key: [tuple(row) for row in conn.execute(text(sql))] for key, sql in queries.items()}
    finally:
        engine.dispose()


def _objects_left(url: str) -> set[tuple[str, str]]:
    """Tables, views, sequences, types and functions of Keyup's own still in ``public``."""
    sql = """
        SELECT 'table', tablename FROM pg_tables WHERE schemaname = 'public'
        UNION ALL
        SELECT 'view', viewname FROM pg_views WHERE schemaname = 'public'
        UNION ALL
        SELECT 'sequence', sequencename FROM pg_sequences WHERE schemaname = 'public'
        UNION ALL
        SELECT 'type', t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        LEFT JOIN pg_depend d ON d.objid = t.oid AND d.deptype = 'e'
        WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'r', 'm') AND d.objid IS NULL
        UNION ALL
        SELECT 'function', p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
        WHERE n.nspname = 'public' AND d.objid IS NULL
    """
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {(str(kind), str(name)) for kind, name in conn.execute(text(sql))}
    finally:
        engine.dispose()


def _applied(script, url: str) -> set[str]:  # noqa: ANN001 - alembic's ScriptDirectory
    """Every revision the database is at or past: the heads in ``alembic_version`` and all of
    their ancestors."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            heads = tuple(row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()
    if not heads:
        return set()
    return {rev.revision for rev in script.iterate_revisions(heads, "base")}


def test_every_migration_downgrades_and_upgrades_again(scratch_url: str) -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from alembic import command

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1, f"more than one head: {script.get_heads()}"

    command.upgrade(config, "head")
    at_head = _schema(scratch_url)

    # Newest first, and every revision after all of its descendants, so each one is a
    # current head by the time it is reached.
    failures: list[str] = []
    for revision in script.walk_revisions(base="base", head="heads"):
        down = revision.down_revision
        # ``<rev>@-1`` unapplies exactly that revision.  Downgrading to its parent would not:
        # when the parent is a branch point, the sibling branch descends from it too and goes
        # as well.  A merge is the exception: ``@-1`` is ambiguous there, and stepping back to
        # either parent unapplies just the merge (the other parent is not its descendant).
        if down is None:
            target = "base"
        elif isinstance(down, str):
            target = f"{revision.revision}@-1"
        else:
            target = down[0]
        step = f"{revision.revision} ({(revision.doc or '').splitlines()[0][:70]})"
        try:
            before = _applied(script, scratch_url)
            command.downgrade(config, target)
            command.upgrade(config, revision.revision)
            command.downgrade(config, target)
            removed = before - _applied(script, scratch_url)
            if removed != {revision.revision}:
                failures.append(f"{step}: stepping back to {target} unapplied {sorted(removed)}")
                break
        except Exception as error:  # noqa: BLE001 - collect every broken step, then fail once
            failures.append(f"{step}: {type(error).__name__}: {error}")
            break  # the database is in an unknown state; the steps after this would lie
    assert not failures, "a migration does not round-trip:\n" + "\n".join(failures)

    left = _objects_left(scratch_url)
    assert left == BASE_LEFTOVERS, f"downgrade base left objects behind: {sorted(left - BASE_LEFTOVERS)}"

    command.upgrade(config, "head")
    again = _schema(scratch_url)
    for key in at_head:
        assert again[key] == at_head[key], f"{key} differ after the round trip"
