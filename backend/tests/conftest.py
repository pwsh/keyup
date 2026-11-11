"""Shared fixtures.

The database tests run against a real PostgreSQL: generated tsvector columns, GIN
indexes on ``int[]``, ``ON CONFLICT`` upserts and advisory locks have no SQLite
equivalent, so there is nothing useful to fake.  ``make test-db`` starts a throwaway
container on 55441; when nothing is listening there the database tests skip and the
parser tests still run.

The environment is set here, before anything imports ``app.config`` or ``app.db``,
because both read it at import time.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_AUDIO = FIXTURES / "audio"
#: the real archive, present on the development host and absent in CI
CORPUS_ROOT = BACKEND_DIR.parent / "audio"
#: the example site profile (``examples/``): the agency list the tests seed and the
#: classification rules they replay, so the shipped examples stay coherent and exercised
EXAMPLES = BACKEND_DIR.parent / "examples"
EXAMPLE_AGENCIES = EXAMPLES / "agencies.example.csv"
EXAMPLE_RULES = EXAMPLES / "classification.example.csv"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://keyup:keyup@localhost:55441/keyup_test",
)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("AUDIO_ROOT", str(FIXTURE_AUDIO))
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("TZ", "America/Winnipeg")
# The live feed's heartbeat: short enough that a test can wait for one.
os.environ.setdefault("LIVE_STATUS_SECONDS", "1")


def _database_reachable(url: str, timeout: float = 1.0) -> bool:
    parsed = urlparse(url.replace("postgresql+psycopg", "postgresql"))
    host, port = parsed.hostname or "localhost", parsed.port or 5432
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


DB_AVAILABLE = _database_reachable(TEST_DATABASE_URL)
requires_db = pytest.mark.skipif(
    not DB_AVAILABLE,
    reason=f"no PostgreSQL at {TEST_DATABASE_URL} — run `make test-db`",
)
requires_corpus = pytest.mark.skipif(
    not CORPUS_ROOT.is_dir(),
    reason=f"the recording archive is not present at {CORPUS_ROOT}",
)


@pytest.fixture(scope="session")
def fixture_audio() -> Path:
    """The synthetic sdrtrunk recordings (`fixtures/gen_fixtures.py`) covering every shape the
    corpus contains."""
    return FIXTURE_AUDIO


@pytest.fixture(scope="session")
def corpus_root() -> Path:
    return CORPUS_ROOT


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """A database at ``head``, built by running the migrations from an empty schema.

    Building it this way rather than from ``Base.metadata.create_all`` is deliberate: it
    is the only thing that proves ``alembic upgrade head`` works on a clean database.
    """
    if not DB_AVAILABLE:  # pragma: no cover - the skip marker handles this
        pytest.skip("no test database")

    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command
    from app.db import engine

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(config, "head")
    yield TEST_DATABASE_URL


@pytest.fixture
def session(migrated_database):  # noqa: ANN001, ARG001
    """An empty database and a session on it.  Every table is truncated first."""
    from sqlalchemy import text

    from app import runtime_settings
    from app.db import SessionLocal
    from app.models import Base
    from app.services import sites as site_service

    names = ", ".join(f'"{t}"' for t in Base.metadata.tables)
    db = SessionLocal()
    db.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    db.commit()
    # The settings table is layered over the environment behind a few seconds of cache;
    # a truncated table must not leave the previous test's override in force.
    runtime_settings.invalidate()
    # Same rule for the per-site call counts, which are cached for a minute, and for the
    # spectrum path's tuner-key sets: a truncated table must not leave the previous test's
    # answer standing.
    site_service.forget_call_counts()
    # …and for the headline-count cache (/api/status, /admin/ingest, /stats/overview):
    # a truncated table must not leave the previous test's totals standing.
    from app.services import counts as count_service  # noqa: PLC0415 - avoids an import cycle

    count_service.invalidate()
    from app.api.recorders import forget_tuner_keys  # noqa: PLC0415 - avoids an import cycle

    forget_tuner_keys()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def settings():  # noqa: ANN201
    """Settings pointed at the fixture tree, independent of the developer's .env."""
    from app.config import Settings

    return Settings(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        audio_root=FIXTURE_AUDIO,
        tz="America/Winnipeg",
        transcribe_min_seconds=0.75,
        dedupe_window_seconds=1.0,
        dedupe_duration_tolerance=0.10,
        conversation_gap_seconds=20,
    )


def _stray_processed_copies() -> list[Path]:
    """Keyup's own processed copies (``<stem>.proc.mp3`` and its ``.proc-*.mp3`` temp)
    under the fixture tree.  None is tracked; one left behind by a local audioproc run
    changes what an ingest walk of the tree sees (``files_seen`` 14, not 13)."""
    return sorted({*FIXTURES.rglob("*.proc.mp3"), *FIXTURES.rglob("*.proc-*.mp3")})


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG001
    """Fail the run if a processed copy sits under ``tests/fixtures`` when it ends —
    whether a test wrote it or it was there before the run started."""
    strays = _stray_processed_copies()
    if not strays:
        return
    lines = "\n".join(f"  {path.relative_to(BACKEND_DIR)}" for path in strays)
    message = f"stray processed audio under tests/fixtures (delete it):\n{lines}"
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write("\n")
        reporter.write_sep("=", "fixture tree is dirty", red=True)
        reporter.write_line(message)
    else:  # pragma: no cover - terminal reporting disabled
        print(message)  # noqa: T201
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
