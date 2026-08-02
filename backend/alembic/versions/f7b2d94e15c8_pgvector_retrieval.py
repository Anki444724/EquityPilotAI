"""pgvector semantic retrieval

Revision ID: f7b2d94e15c8
Revises: e4a9c62d7b13
Create Date: 2026-08-02

Adds a real vector column and a full-text index to `document_chunks`.

`embedding_v2` is a NEW column rather than a replacement for `embedding`.
The old column holds 384-dimension hashed n-gram vectors and the new one holds
1024-dimension bge-m3 vectors; they are different spaces, and a cosine between
them is arithmetically valid and completely meaningless. Keeping both lets the
corpus re-embed incrementally while retrieval keeps working, and lets the
benchmark compare the two engines on identical data — which is the whole point
of the exercise.

Everything here is guarded and reversible on a database without pgvector, so
the migration still applies on SQLite (where the test suite runs) and on a
Postgres that has not had the extension enabled.

The IVFFlat index is deliberately NOT created here. It requires data to build
its lists from — building it on an empty table produces a useless index — so
it is created by the backfill script once vectors exist.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f7b2d94e15c8"
down_revision: str | None = "e4a9c62d7b13"
branch_labels = None
depends_on = None

VECTOR_DIMENSION = 1024


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite still needs the columns.
        #
        # An earlier draft returned here and created nothing, on the reasoning
        # that `create_all` provides them for the tests. `test_migrations.py`
        # correctly rejected that: it replays every migration into an empty
        # database and diffs against the models, and a column that exists only
        # via `create_all` is exactly the MIG-001 class of defect — invisible
        # locally, `UndefinedColumn` in production.
        #
        # `text_search` is a Postgres generated column with no SQLite
        # equivalent, so the lexical signal degrades there; the engine already
        # treats a failing signal as absent rather than fatal.
        op.add_column("document_chunks",
                      sa.Column("embedding_v2", sa.JSON(), nullable=True))
        op.add_column("document_chunks",
                      sa.Column("embedding_spec_v2", sa.String(length=80),
                                nullable=True))
        return

    bind = op.get_bind()
    bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    op.add_column(
        "document_chunks",
        sa.Column("embedding_v2", sa.dialects.postgresql.ARRAY(sa.Float()),
                  nullable=True),
    )
    # Re-typed to `vector` after creation: Alembic has no native vector type,
    # and declaring it as an array first keeps the migration readable.
    bind.execute(sa.text(
        f"ALTER TABLE document_chunks "
        f"ALTER COLUMN embedding_v2 TYPE vector({VECTOR_DIMENSION}) "
        f"USING embedding_v2::vector({VECTOR_DIMENSION})"
    ))

    op.add_column(
        "document_chunks",
        sa.Column("embedding_spec_v2", sa.String(length=80), nullable=True),
    )

    # Full-text search, generated from the text so it cannot drift out of
    # sync with the content the way an application-maintained column would.
    # 'simple' rather than 'english': the corpus contains Hindi and Hinglish,
    # and English stemming mangles both.
    bind.execute(sa.text(
        "ALTER TABLE document_chunks "
        "ADD COLUMN text_search tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
    ))
    bind.execute(sa.text(
        "CREATE INDEX ix_chunks_text_search "
        "ON document_chunks USING GIN (text_search)"
    ))


def downgrade() -> None:
    if not _is_postgres():
        return
    bind = op.get_bind()
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_chunks_vector"))
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_chunks_text_search"))
    op.drop_column("document_chunks", "text_search")
    op.drop_column("document_chunks", "embedding_spec_v2")
    op.drop_column("document_chunks", "embedding_v2")
