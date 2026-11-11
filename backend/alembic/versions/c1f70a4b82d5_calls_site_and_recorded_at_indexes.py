"""``calls`` gets the two indexes the Sites page and the freshness stamp have been missing.

``GET /sites`` took 60-78 seconds in production against 1.1 M calls, and two open browsers
kept about ten of those in flight at once, which is what starved Postgres (294 % CPU,
load 15) and made every other route — the recorder's spectrum posts, the admin spectrum
stream — slow at the same time.  ``calls`` had no index on ``site_id`` at all, so the
three questions the page asks were three sequential scans of the whole archive:

* ``SELECT site_id, count(*) … WHERE site_id IN (…) GROUP BY site_id`` — the per-site call
  counts every row of the listing shows;
* ``SELECT count(*) … WHERE site_id = $1`` — the same question asked again, once per site,
  by the delete button's "what is blocking this" check (:func:`app.refile.site_blockers`);
* ``SELECT max(recorded_at) FROM calls`` — the "last heard" stamp on the admin dashboard.

``ix_calls_site_recorded`` answers the first two as an index-only scan on its leading
column, and answers "the newest call at this site" from its first entry.
``ix_calls_recorded_at`` turns the third into a one-row backwards index scan.
``ix_calls_started_at`` could not stand in for it: ``started_at`` is
``recorded_at − duration_s``, so the two orders differ by each call's own length and the
maximum of one is not the maximum of the other.

``ix_calls_site_id_recorded_at`` **already exists in production**: it was built by hand,
concurrently, as an emergency measure while the incident was live (ten minutes on 1.1 M
rows).  So this migration adopts that index rather than replacing it — same name, same
column list, same ascending order — and creates it ``IF NOT EXISTS``, which makes the
upgrade an instant no-op on the box that has it and a real build everywhere else.
Renaming it, or declaring it ``DESC`` to match ``ix_calls_started_at``'s house style,
would have meant dropping and rebuilding the one index the production database is
currently leaning on.

``ix_calls_recorded_at`` is new everywhere and is built ``CONCURRENTLY``, because a plain
``CREATE INDEX`` on 1.1 M rows holds a write lock against the live ingest for the duration.
Alembic runs migrations in a transaction by default, so the autocommit block below is what
makes that legal; ``IF NOT EXISTS`` also makes a re-run after an interrupted build a no-op
rather than an error.

Note for what comes after: the index does not make the count free.  Measured on the
production archive once the index was in place, the grouped count is 971 ms — a parallel
index-only scan with 474 k heap fetches, because the visibility map was not yet set.  A
``VACUUM`` fixes the heap fetches, but a second of database work per request is still far
too much for a page that refetches, which is why ``GET /sites`` caches the counts
(:func:`app.services.sites.call_counts`) rather than merely indexing them.

Revision ID: c1f70a4b82d5
Revises: a3d5b7c91e04
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c1f70a4b82d5"
down_revision: str | None = "a3d5b7c91e04"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: The name and column list production already has — do not change either.
SITE_INDEX = "ix_calls_site_id_recorded_at"
RECORDED_INDEX = "ix_calls_recorded_at"


def upgrade() -> None:
    op.execute(f"CREATE INDEX IF NOT EXISTS {SITE_INDEX} ON calls (site_id, recorded_at)")
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {RECORDED_INDEX} ON calls (recorded_at DESC)"
        )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SITE_INDEX}")
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {RECORDED_INDEX}")
