"""Configuration the operator can edit: ``stt_backends`` and ``ingest_sources``.

Two things this deployment could only be told through the environment now have tables, and
therefore a GUI, an audit trail and a way to change without a restart.

``stt_backends`` is ``STT_BACKENDS`` with a primary key.  The seeding rule is the whole of
the migration's judgement and is worth stating once: the environment is copied in **only
when the table is empty**, and from then on the database wins and ``STT_BACKENDS`` is never
consulted again while a row exists (:mod:`app.stt.config_store`).  That way an upgrade is
invisible — the deployment comes back on exactly the backends it was running — and a later
edit in the GUI is not silently reverted by whatever is still sitting in the host's
``.env``.  The rows are stamped ``source='env'`` so the operator can see which ones they
did not write.

``stt_backend_stats.config_version`` is the other half of making an edit visible: the
worker stamps every row it publishes with the configuration generation its router was built
from, so the page can say "picked up" rather than leaving the operator watching a counter.

``ingest_sources`` does the same for where recordings come from.  ``AUDIO_ROOT`` seeds one
``watch_dir`` row — it remains the archive root that ``calls.file_path`` is relative to, it
is simply no longer the *only* directory the watcher may look at.  The seed is skipped when
the table already has a row, for the same reason as above.

Revision ID: b3e91c47d5a2
Revises: e7b3d9a24c18
Create Date: 2026-09-01
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3e91c47d5a2"
down_revision: str | None = "e7b3d9a24c18"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

log = logging.getLogger("alembic.gui_config")

STT_KINDS = ("openai_compatible", "ovms", "faster_whisper_local", "openvino_genai")


def _seed_stt_backends(bind: sa.engine.Connection) -> None:
    """Copy ``STT_BACKENDS`` into an empty ``stt_backends``.

    Reads the setting through :class:`app.config.Settings` rather than parsing the raw
    environment, so the JSON is validated by the same model the application uses and a
    malformed value fails here, loudly, instead of at the worker's next start.
    """
    existing = bind.execute(sa.text("SELECT count(*) FROM stt_backends")).scalar()
    if existing:
        log.info("stt_backends already has %s row(s); not seeding from STT_BACKENDS", existing)
        return
    try:
        from app.config import get_settings  # noqa: PLC0415 - migrations must not import at module level

        backends = list(get_settings().stt_backends)
    except Exception:  # noqa: BLE001 - a schema upgrade must not fail on a config read
        log.warning("could not read STT_BACKENDS; leaving stt_backends empty", exc_info=True)
        return
    for backend in backends:
        values = backend.model_dump()
        bind.execute(
            sa.text(
                """
                INSERT INTO stt_backends (
                    name, kind, base_url, api_key, model, device, compute_type, cpu_threads,
                    cache_dir, use_prompt, use_hotwords, concurrency, priority, timeout,
                    enabled, source
                ) VALUES (
                    :name, :kind, :base_url, :api_key, :model, :device, :compute_type,
                    :cpu_threads, :cache_dir, :use_prompt, :use_hotwords, :concurrency,
                    :priority, :timeout, :enabled, 'env'
                )
                ON CONFLICT (name) DO NOTHING
                """
            ),
            values,
        )
    log.info("seeded %d backend(s) from STT_BACKENDS", len(backends))


def _seed_ingest_sources(bind: sa.engine.Connection) -> None:
    """Make ``AUDIO_ROOT`` the first watched directory."""
    existing = bind.execute(sa.text("SELECT count(*) FROM ingest_sources")).scalar()
    if existing:
        return
    try:
        from app.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        root = str(settings.audio_root)
        prefer = settings.tr_prefer_audio
    except Exception:  # noqa: BLE001
        log.warning("could not read AUDIO_ROOT; leaving ingest_sources empty", exc_info=True)
        return
    bind.execute(
        sa.text(
            """
            INSERT INTO ingest_sources (kind, label, enabled, path, recursive, tr_prefer_audio, notes)
            VALUES ('watch_dir', :label, true, :path, true, :prefer,
                    'Seeded from AUDIO_ROOT. This is the archive root every calls.file_path is relative to.')
            ON CONFLICT (label) DO NOTHING
            """
        ),
        {"label": "AUDIO_ROOT", "path": root, "prefer": prefer},
    )


def upgrade() -> None:
    op.create_table(
        "stt_backends",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'openai_compatible'")),
        sa.Column("base_url", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("api_key", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("model", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("device", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("compute_type", sa.Text(), nullable=False, server_default=sa.text("'int8'")),
        sa.Column("cpu_threads", sa.Integer(), nullable=False, server_default=sa.text("4")),
        sa.Column("cache_dir", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("use_prompt", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("use_hotwords", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("timeout", sa.Float(), nullable=False, server_default=sa.text("300")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'db'")),
        sa.Column("notes", sa.Text()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.Text()),
        sa.PrimaryKeyConstraint("name"),
        sa.CheckConstraint(
            "kind in ('openai_compatible', 'ovms', 'faster_whisper_local', 'openvino_genai')",
            name="ck_stt_backends_kind",
        ),
    )
    op.create_index("ix_stt_backends_live", "stt_backends", ["deleted_at", "priority"])

    op.add_column("stt_backend_stats", sa.Column("config_version", sa.Integer()))

    # ``create_type=False``: the type is created once, here, and the column below must not
    # try again inside the same transaction.
    ingest_source_kind = sa.dialects.postgresql.ENUM(
        "watch_dir", "push_token", "migration", name="ingest_source_kind", create_type=False
    )
    sa.Enum("watch_dir", "push_token", "migration", name="ingest_source_kind").create(
        op.get_bind(), checkfirst=True
    )
    op.create_table(
        "ingest_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", ingest_source_kind, nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("path", sa.Text()),
        sa.Column("recursive", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("tr_prefer_audio", sa.Text()),
        sa.Column("tr_sidecar_wait_seconds", sa.Float()),
        sa.Column("token_id", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("meta", sa.dialects.postgresql.JSONB()),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("label"),
        sa.UniqueConstraint("kind", "path", name="uq_ingest_sources_kind_path"),
        sa.ForeignKeyConstraint(["token_id"], ["api_tokens.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "kind <> 'watch_dir' or path is not null", name="ck_ingest_sources_watch_dir_path"
        ),
        sa.CheckConstraint(
            "tr_prefer_audio is null or tr_prefer_audio in ('m4a', 'wav')",
            name="ck_ingest_sources_tr_prefer_audio",
        ),
    )
    op.create_index("ix_ingest_sources_kind_enabled", "ingest_sources", ["kind", "enabled"])

    bind = op.get_bind()
    _seed_stt_backends(bind)
    _seed_ingest_sources(bind)


def downgrade() -> None:
    op.drop_index("ix_ingest_sources_kind_enabled", table_name="ingest_sources")
    op.drop_table("ingest_sources")
    sa.Enum(name="ingest_source_kind").drop(op.get_bind(), checkfirst=True)
    op.drop_column("stt_backend_stats", "config_version")
    op.drop_index("ix_stt_backends_live", table_name="stt_backends")
    op.drop_table("stt_backends")
    op.execute("DELETE FROM settings WHERE key = 'stt_config_version'")
    op.execute("DELETE FROM settings WHERE key = 'ingest_config_version'")
