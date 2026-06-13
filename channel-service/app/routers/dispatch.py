"""Channel /dispatch — CRM calls this to deliver a message (§5).

Looks up the customer's hidden ShopperPersona, applies fatigue decay, increments
fatigue for this send, then builds a persona-conditioned outcome timeline and
schedules async callbacks, returning immediately (202).
"""
from __future__ import annotations

import logging
import math
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import dispatcher
from ..db import get_db
from ..models import ShopperPersona
from ..simulator import build_timeline

router = APIRouter(tags=["dispatch"])
logger = logging.getLogger(__name__)

FATIGUE_DECAY_RATE = float(os.getenv("FATIGUE_DECAY_RATE", "1.0"))


class DispatchRequest(BaseModel):
    communication_id: uuid.UUID
    customer_id: uuid.UUID
    channel: str
    content: str | None = None


class DispatchResult(BaseModel):
    communication_id: uuid.UUID
    scheduled_events: int


def _apply_decay_and_increment(persona: ShopperPersona, db: Session) -> None:
    """Decay stale fatigue then increment for this send. Commits the row."""
    now = datetime.now(timezone.utc)
    if persona.last_messaged_at is not None:
        days_since = (now - persona.last_messaged_at).total_seconds() / 86400.0
        decay = math.floor(days_since * FATIGUE_DECAY_RATE)
        persona.current_fatigue = max(0, persona.current_fatigue - decay)
    # Cap at 2× threshold so the counter stays bounded even under continuous sends.
    persona.current_fatigue = min(persona.current_fatigue + 1, persona.fatigue_threshold * 2)
    persona.last_messaged_at = now
    db.commit()


@router.post("/dispatch", response_model=DispatchResult, status_code=status.HTTP_202_ACCEPTED)
async def dispatch_message(
    payload: DispatchRequest,
    db: Session = Depends(get_db),
) -> DispatchResult:
    """Schedule the simulated callback timeline for a communication.

    Async so it runs on the event loop — `dispatcher.dispatch` uses
    `asyncio.create_task`, which requires a running loop (a sync handler would run
    in a threadpool with no loop).
    """
    persona = db.get(ShopperPersona, payload.customer_id)
    if persona is not None:
        _apply_decay_and_increment(persona, db)
    else:
        logger.warning("No persona for customer %s — using coin-flip defaults", payload.customer_id)

    timeline = build_timeline(payload.channel, persona, payload.content)
    dispatcher.dispatch(str(payload.communication_id), timeline)
    return DispatchResult(
        communication_id=payload.communication_id, scheduled_events=len(timeline)
    )
