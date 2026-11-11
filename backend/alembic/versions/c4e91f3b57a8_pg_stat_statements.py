"""``CREATE EXTENSION pg_stat_statements``, if the library is preloaded.

The index review that produced the two preceding migrations was done with
``pg_stat_user_indexes``, which answers "was this index ever scanned" and nothing else.
The question it could not answer — *which queries* are spilling 27 GB of temp files, and
which ones the 83% cache hit rate is being spent on — needs ``pg_stat_statements``, so
compose now starts Postgres with it in ``shared_preload_libraries`` and this migration
creates the extension that reads the shared memory it allocates.

The guard is the point of the ``DO`` block.  ``CREATE EXTENSION pg_stat_statements``
errors with *"pg_stat_statements must be loaded via shared_preload_libraries"* when the
library is not preloaded, and that is the normal state of a developer's database, a test
database, and production itself until the db container is recreated with the new
``command:``.  A migration that fails there fails the ``alembic upgrade head`` in the api
service's start command, which means the whole stack refuses to come up over a
*diagnostics* extension.  So the failure is caught and downgraded to a NOTICE: the
extension appears the first time the migration runs on a cluster that has the library, and
until then nothing here is load-bearing.

The notice is built with ``RAISE … USING MESSAGE =`` and string concatenation rather than
the usual ``RAISE NOTICE '…%…', SQLERRM``, for a reason that has nothing to do with
Postgres: psycopg parses the SQL it is handed for its own ``%s``/``%b``/``%t`` placeholders
before the server ever sees it, and a bare ``%`` in the format string fails client-side
with *"only '%s', '%b', '%t' are allowed as placeholders"*.  ``%%`` would escape it, but
concatenation says what it means and does not depend on which driver runs the migration.

``EXCEPTION WHEN OTHERS`` rather than a named condition on purpose — the error is raised
with different SQLSTATEs depending on how the load failed (``0A000`` for the preload
check, ``42501`` for a non-superuser), and every one of them means the same thing to this
migration: not available here, carry on.  The block runs in its own subtransaction, so the
caught failure does not poison the migration's transaction.

Revision ID: c4e91f3b57a8
Revises: b8d4e60a17c2
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c4e91f3b57a8"
down_revision: str | None = "b8d4e60a17c2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

CREATE = """
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE USING MESSAGE =
        'pg_stat_statements not created (' || SQLERRM || '): the library is not in '
        'shared_preload_libraries on this cluster, or this role cannot create extensions. '
        'This is advisory only - query statistics are unavailable until the db container '
        'runs with it preloaded.';
END
$$;
"""

#: Dropping it is unconditional and safe: IF EXISTS covers the cluster where the create
#: above was skipped, and the extension owns no user data — it is a view over shared
#: memory, and recreating it starts the counters again, which is all a downgrade loses.
DROP = "DROP EXTENSION IF EXISTS pg_stat_statements"


def upgrade() -> None:
    op.get_bind().exec_driver_sql(CREATE)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(DROP)
