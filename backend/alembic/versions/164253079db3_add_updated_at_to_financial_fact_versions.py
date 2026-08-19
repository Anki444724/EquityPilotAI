"""add updated_at to financial_fact_versions

Revision ID: 164253079db3
Revises: 163142968ca2
"""

from alembic import op
import sqlalchemy as sa


revision = "164253079db3"
down_revision = "163142968ca2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {
        column["name"]
        for column in inspector.get_columns("financial_fact_versions")
    }

    if "updated_at" not in columns:
        op.add_column(
            "financial_fact_versions",
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
        for column in inspector.get_columns("financial_fact_versions")
    }

    if "updated_at" in columns:
        op.drop_column("financial_fact_versions", "updated_at")
