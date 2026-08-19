"""add updated_at to company_versions

Revision ID: 163142968ca2
Revises: 7f8091a2b3c4
"""

from alembic import op
import sqlalchemy as sa


revision = "163142968ca2"
down_revision = "7f8091a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {
        column["name"]
        for column in inspector.get_columns("company_versions")
    }

    if "updated_at" not in columns:
        op.add_column(
            "company_versions",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {
        column["name"]
        for column in inspector.get_columns("company_versions")
    }

    if "updated_at" in columns:
        op.drop_column("company_versions", "updated_at")
