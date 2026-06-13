"""add last_messaged_at to shopper_personas

Needed for fatigue decay: the simulator computes how many days have elapsed
since the last send and reduces current_fatigue accordingly.

Revision ID: 20260611_02
Revises: 20260611_01
Create Date: 2026-06-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260611_02"
down_revision: Union[str, None] = "20260611_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shopper_personas",
        sa.Column("last_messaged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shopper_personas", "last_messaged_at")
