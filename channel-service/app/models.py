"""Channel-service-owned models — the simulation's HIDDEN truth (ARCHITECTURE.md §3).

Only `shopper_personas` lives here, in its own DB. The CRM (and therefore the
agent) never sees these columns; the agent must infer channel affinity and
fatigue from observed events. That hidden boundary is the intelligence test.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class ShopperPersona(Base):
    __tablename__ = "shopper_personas"

    # Matches the CRM customer id, but the CRM never joins against this table.
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Open-propensity per channel, e.g. {whatsapp:0.8, sms:0.3, email:0.5, rcs:0.4}.
    channel_affinity: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # How much a discount moves them, 0-1.
    price_sensitivity: Mapped[float] = mapped_column(Float, nullable=False)
    # Baseline conversion likelihood.
    base_buy_propensity: Mapped[float] = mapped_column(Float, nullable=False)
    # Messages-per-week before they tune out.
    fatigue_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    # Mutable; decays at FATIGUE_DECAY_RATE units/day since last_messaged_at.
    current_fatigue: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Timestamp of the last dispatched message; used to compute fatigue decay.
    last_messaged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
