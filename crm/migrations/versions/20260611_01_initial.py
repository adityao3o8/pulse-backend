"""initial CRM schema (8 tables, no personas)

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
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text()),
        sa.Column("city", sa.Text()),
        sa.Column("signup_date", TZ, nullable=False),
        sa.Column("first_order_date", TZ),
        sa.Column("last_order_date", TZ),
        sa.Column("total_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_spend", sa.Numeric(12, 2), nullable=False, server_default="0"),
    )

    op.create_table(
        "segments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("nl_query", sa.Text()),
        sa.Column("filter_json", JSONB),
        sa.Column("created_by", sa.String(16), nullable=False),
    )

    op.create_table(
        "campaigns",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text()),
        sa.Column("segment_id", UUID, sa.ForeignKey("segments.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("created_by_agent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "communications",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "customer_id", UUID,
            sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("campaign_id", UUID, sa.ForeignKey("campaigns.id", ondelete="SET NULL")),
        sa.Column("journey_node_id", sa.String(64)),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("variant", sa.String(8)),
        sa.Column("content", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_communications_customer_id", "communications", ["customer_id"])
    op.create_index("ix_communications_campaign_id", "communications", ["campaign_id"])

    op.create_table(
        "orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "customer_id", UUID,
            sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("items", JSONB),
        sa.Column("order_date", TZ, nullable=False),
        sa.Column(
            "attributed_communication_id", UUID,
            sa.ForeignKey("communications.id", ondelete="SET NULL"),
        ),
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])

    op.create_table(
        "journeys",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "campaign_id", UUID,
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("graph_json", JSONB),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
    )
    op.create_index("ix_journeys_campaign_id", "journeys", ["campaign_id"])

    op.create_table(
        "communication_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "communication_id", UUID,
            sa.ForeignKey("communications.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
    )
    op.create_index(
        "ix_communication_events_communication_id",
        "communication_events", ["communication_id"],
    )

    op.create_table(
        "agent_decisions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("campaign_id", UUID, sa.ForeignKey("campaigns.id", ondelete="CASCADE")),
        sa.Column("step", sa.String(32), nullable=False),
        sa.Column("reasoning", sa.Text()),
        sa.Column("evidence_json", JSONB),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_decisions_campaign_id", "agent_decisions", ["campaign_id"])


def downgrade() -> None:
    op.drop_table("agent_decisions")
    op.drop_table("communication_events")
    op.drop_table("journeys")
    op.drop_table("orders")
    op.drop_table("communications")
    op.drop_table("campaigns")
    op.drop_table("segments")
    op.drop_table("customers")
