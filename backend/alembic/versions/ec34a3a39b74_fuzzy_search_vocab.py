"""Fuzzy search: ``pg_trgm``, the ``search_vocab`` table and trigram indexes on the aliases.

A street name Whisper spelt its own
way — ``Milrose`` for Melrose, ``Kelguck`` for Kelbuk — could not be found by full-text
search, which matches lexemes exactly.  app/services/fuzzy.py widens a query word to the
spellings the archive holds; this migration gives it something to look them up in.

* ``CREATE EXTENSION IF NOT EXISTS pg_trgm`` — shipped with PostgreSQL (1.6 on the 18.6
  image production and the test container run) and a trusted extension, so the database
  owner may create it.
* ``search_vocab (word, ndoc, nentry)``: every distinct lexeme of ``calls.search_tsv`` over
  the primary calls, from ``ts_stat``, with a trigram GIN index on ``word``.  Measured on
  production 2026-09-23: 181 354 lexemes, ``ts_stat`` over all 5.2 M calls in 5.2 s; the
  table is 8.4 MB, its primary key 7 MB and the trigram index 5 MB.  Filled here because
  that is quick, and the watermark the worker keeps (``settings.search_vocab_state``) is set
  to match, so its first pass is incremental rather than a second rebuild.
* The alternative — a trigram GIN on ``calls.transcript_text`` — was measured on a 5 %
  production sample: 12 MB, so ≈250 MB whole, plus ~40 index entries on every transcript
  write, and it cannot take part in the ``tsquery`` (ranking, websearch syntax,
  highlighting).  Not built.
* Trigram GIN indexes on the alias columns the search box's suggestions read:
  ``talkgroups.alias``, ``radios.alias``, ``radios.unit_id``, ``units.callsign``,
  ``units.name`` and ``personnel.name``.  They serve both the near-spelling suggestions and
  the existing substring ``ILIKE``.  Small tables (3.6 k talkgroups, 13 k radios, empty
  roster), but ``radios`` is written by every ingest, so they are built ``CONCURRENTLY IF
  NOT EXISTS`` in an ``autocommit_block`` like 554fe0551d12 and 1054b8161324, and a failed
  build's INVALID leftover is dropped before the retry.

The downgrade drops the indexes, the table and the watermark row.  It leaves ``pg_trgm``
installed: an extension is harmless on its own, and something else may have come to use it.

Revision ID: ec34a3a39b74
Revises: 1054b8161324
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ec34a3a39b74"
down_revision: str | None = "1054b8161324"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: app/services/fuzzy.py ``STATE_KEY`` — repeated rather than imported, as a migration must.
STATE_KEY = "search_vocab_state"

#: name -> (table, column), each built ``USING gin (column gin_trgm_ops)``.
ALIAS_INDEXES: dict[str, tuple[str, str]] = {
    "ix_talkgroups_alias_trgm": ("talkgroups", "alias"),
    "ix_radios_alias_trgm": ("radios", "alias"),
    "ix_radios_unit_id_trgm": ("radios", "unit_id"),
    "ix_units_callsign_trgm": ("units", "callsign"),
    "ix_units_name_trgm": ("units", "name"),
    "ix_personnel_name_trgm": ("personnel", "name"),
}


def _drop_if_invalid(bind: sa.engine.Connection, name: str) -> None:
    """A concurrent build that failed leaves an INVALID index that ``IF NOT EXISTS`` skips."""
    invalid = bind.exec_driver_sql(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %(name)s AND NOT i.indisvalid",
        {"name": name},
    ).first()
    if invalid is not None:
        bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "search_vocab",
        sa.Column("word", sa.Text(), primary_key=True),
        sa.Column("ndoc", sa.Integer(), nullable=False),
        sa.Column("nentry", sa.Integer(), nullable=False),
    )
    # Filled before the trigram index exists: one bulk build is far cheaper than 181 k
    # inserts into a GIN index.  The watermark is read first, so a transcript that lands
    # during the scan is picked up by the worker's first incremental pass.
    op.execute(
        "INSERT INTO settings (key, value, description) VALUES ("
        f"'{STATE_KEY}', jsonb_build_object("
        "'transcript_id', (SELECT coalesce(max(id), 0) FROM transcripts), "
        # jsonb renders a timestamptz as ISO 8601 with its offset: fromisoformat reads it.
        "'full_at', now()), "
        "'Fuzzy-search vocabulary watermark') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    op.execute(
        "INSERT INTO search_vocab (word, ndoc, nentry) SELECT word, ndoc, nentry FROM ts_stat("
        "'SELECT search_tsv FROM calls WHERE duplicate_of IS NULL AND search_tsv IS NOT NULL')"
    )
    op.execute("CREATE INDEX ix_search_vocab_word_trgm ON search_vocab USING gin (word gin_trgm_ops)")
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name, (table, column) in ALIAS_INDEXES.items():
            _drop_if_invalid(bind, name)
            bind.exec_driver_sql(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} USING gin ({column} gin_trgm_ops)"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        for name in reversed(list(ALIAS_INDEXES)):
            bind.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    op.drop_index("ix_search_vocab_word_trgm", table_name="search_vocab")
    op.drop_table("search_vocab")
    op.execute(f"DELETE FROM settings WHERE key = '{STATE_KEY}'")
