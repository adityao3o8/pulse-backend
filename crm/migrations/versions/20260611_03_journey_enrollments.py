"""add journey_enrollments table

Revision ID: 20260611_03
Revises: 20260611_01
Create Date: 2026-06-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260611_03"
down_revision: Union[str, None] = "20260611_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "journey_enrollments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "journey_id", UUID,
            sa.ForeignKey("journeys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id", UUID,
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("current_node_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("is_control", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("entered_node_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_je_journey_status", "journey_enrollments", ["journey_id", "status"])
    op.create_index("ix_je_customer", "journey_enrollments", ["customer_id"])
    op.create_unique_constraint(
        "uq_je_journey_customer", "journey_enrollments", ["journey_id", "customer_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_je_journey_customer", "journey_enrollments")
    op.drop_index("ix_je_customer", "journey_enrollments")
    op.drop_index("ix_je_journey_status", "journey_enrollments")
    op.drop_table("journey_enrollments")
