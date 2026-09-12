"""Add the Angel One broker tables (broker_accounts, broker_orders).

Revision ID: b10a01b0a101
Revises: 164253079db3
Create Date: 2026-08-26

Adds the two tables backing the Angel One SmartAPI broker integration:
``broker_accounts`` (one enveloped Angel One session per user) and
``broker_orders`` (orders placed through the platform, keyed by the Angel
One order id so postbacks can only ever update an order this platform
created for the calling user).

Uniqueness is enforced with unique *indexes* rather than table-level
UniqueConstraints: SQLite (the test dialect) does not support
``ALTER TABLE ... ADD CONSTRAINT``, and a table-level UniqueConstraint on a
column that also carries a foreign key makes Alembic emit exactly that
unsupported ALTER. Unique indexes are portable across SQLite and Postgres.

Additive and reversible: downgrade drops the two tables and their indexes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b10a01b0a101"
down_revision = "164253079db3"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("broker_accounts"):
        op.create_table(
            "broker_accounts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(length=36),
                      sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("client_code", sa.String(length=20), nullable=False),
            sa.Column("exchange", sa.String(length=10), nullable=False,
                      server_default="NSE"),
            sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=True),
            sa.Column("access_token_key_version", sa.Integer(), nullable=True),
            sa.Column("trading_password_encrypted", sa.LargeBinary(), nullable=True),
            sa.Column("trading_password_key_version", sa.Integer(), nullable=True),
            sa.Column("angel_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("angel_valid_upto", sa.DateTime(timezone=True), nullable=True),
            sa.Column("connected", sa.Boolean(), nullable=False,
                      server_default=sa.text("1")),
            sa.Column("last_postback_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_broker_accounts_user_id", "broker_accounts",
                        ["user_id"], unique=True)
        op.create_index("ix_broker_accounts_tenant_id", "broker_accounts",
                        ["tenant_id"])

    if not _has_table("broker_orders"):
        op.create_table(
            "broker_orders",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(length=36),
                      sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=True),
            sa.Column("angel_order_id", sa.String(length=40), nullable=True),
            sa.Column("exchange_order_id", sa.String(length=40), nullable=True),
            sa.Column("symbol", sa.String(length=30), nullable=False),
            sa.Column("exchange", sa.String(length=10), nullable=False,
                      server_default="NSE"),
            sa.Column("market", sa.String(length=10), nullable=False,
                      server_default="NSE"),
            sa.Column("transak_type", sa.String(length=1), nullable=False),
            sa.Column("product_type", sa.String(length=10), nullable=False,
                      server_default="CNC"),
            sa.Column("order_type", sa.String(length=10), nullable=False,
                      server_default="LIMIT"),
            sa.Column("validity", sa.String(length=10), nullable=False,
                      server_default="DAY"),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("price", sa.Float(), nullable=False),
            sa.Column("trigger_price", sa.Float(), nullable=False,
                      server_default="0"),
            sa.Column("status", sa.String(length=20), nullable=False,
                      server_default="OPEN"),
            sa.Column("fill_quantity", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("average_price", sa.Float(), nullable=False,
                      server_default="0"),
            sa.Column("remark", sa.String(length=200), nullable=True),
            sa.Column("placed_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("last_postback_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_broker_orders_user_id", "broker_orders", ["user_id"])
        op.create_index("ix_broker_orders_tenant_id", "broker_orders", ["tenant_id"])
        op.create_index("ix_broker_orders_angel_order_id", "broker_orders",
                        ["angel_order_id"])
        op.create_index("ix_broker_orders_status", "broker_orders", ["status"])
        op.create_index("uq_broker_order_user_angel", "broker_orders",
                        ["user_id", "angel_order_id"], unique=True)


def downgrade() -> None:
    if _has_table("broker_orders"):
        op.drop_index("uq_broker_order_user_angel", table_name="broker_orders")
        op.drop_index("ix_broker_orders_status", table_name="broker_orders")
        op.drop_index("ix_broker_orders_angel_order_id", table_name="broker_orders")
        op.drop_index("ix_broker_orders_tenant_id", table_name="broker_orders")
        op.drop_index("ix_broker_orders_user_id", table_name="broker_orders")
        op.drop_table("broker_orders")
    if _has_table("broker_accounts"):
        op.drop_index("ix_broker_accounts_tenant_id", table_name="broker_accounts")
        op.drop_index("ix_broker_accounts_user_id", table_name="broker_accounts")
        op.drop_table("broker_accounts")
