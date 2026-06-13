"""initial channel schema (shopper_personas)

Revision ID: 20260611_01
Revises:
Create Date: 2026-06-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260611_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB


def upgrade() -> None:
    op.create_table(
        "shopper_personas",
        sa.Column("customer_id", UUID, primary_key=True),
        sa.Column("channel_affinity", JSONB, nullable=False),
        sa.Column("price_sensitivity", sa.Float(), nullable=False),
        sa.Column("base_buy_propensity", sa.Float(), nullable=False),
        sa.Column("fatigue_threshold", sa.Integer(), nullable=False),
        sa.Column("current_fatigue", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("shopper_personas")
