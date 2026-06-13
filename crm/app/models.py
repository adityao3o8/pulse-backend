"""CRM-owned ORM models (ARCHITECTURE.md §3).

9 CRM tables. Only `customers` and `orders` are populated in checkpoint 1; the
rest are used by later checkpoints (callback loop, journeys, analytics, agent).

Boundary note: `shopper_personas` deliberately lives ONLY in the channel
service (its own DB) and is NOT defined here — the CRM must never see persona
psychology.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime

from .db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# Reusable timestamptz column type.
TZDateTime = DateTime(timezone=True)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    signup_date: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    # Derived aggregates (computed at ingest/seed time).
    first_order_date: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_order_date: Mapped[datetime | None] = mapped_column(TZDateTime)
    total_orders: Mapped[int] = mapped_column(default=0, nullable=False)
    total_spend: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    orders: Mapped[list["Order"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    items: Mapped[list | None] = mapped_column(JSONB)  # [{sku, name, category, qty, price}]
    order_date: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    # Set if an order followed a comm within the attribution window (later checkpoints).
    attributed_communication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("communications.id", ondelete="SET NULL")
    )

    customer: Mapped["Customer"] = relationship(back_populates="orders")


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    nl_query: Mapped[str | None] = mapped_column(Text)
    filter_json: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(16), nullable=False)  # 'agent' | 'marketer'


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("segments.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    created_by_agent: Mapped[bool] = mapped_column(default=False, nullable=False)


class Journey(Base):
    __tablename__ = "journeys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    graph_json: Mapped[dict | None] = mapped_column(JSONB)  # nodes + edges (§4)
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)


class Communication(Base):
    __tablename__ = "communications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    journey_node_id: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16), nullable=False)  # whatsapp/sms/email/rcs
    variant: Mapped[str | None] = mapped_column(String(8))  # A / B
    content: Mapped[str | None] = mapped_column(Text)
    # Latest known state, denormalized from events (forward-only transitions, §5).
    status: Mapped[str] = mapped_column(String(16), default="created", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )

    events: Mapped[list["CommunicationEvent"]] = relationship(
        back_populates="communication", cascade="all, delete-orphan"
    )


class CommunicationEvent(Base):
    __tablename__ = "communication_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    communication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("communications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)  # monotonic per communication
    # Dedup key for the callback loop; duplicate callbacks are no-ops (§5).
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    communication: Mapped["Communication"] = relationship(back_populates="events")


class AgentDecision(Base):
    __tablename__ = "agent_decisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    # segment / journey_design / channel_choice / copy / adaptation
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )


class JourneyEnrollment(Base):
    __tablename__ = "journey_enrollments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    journey_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journeys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    current_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    # active / completed / exited
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    # True = holdout group; receives no messages but traverses the same wait timers
    is_control: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Time we entered the current node; drives wait-timer expiry
    entered_node_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("journey_id", "customer_id", name="uq_je_journey_customer"),
    )
