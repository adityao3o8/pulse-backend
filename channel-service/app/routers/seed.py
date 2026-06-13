"""Persona seeding endpoint. Called by the CRM /seed over HTTP (not by users)."""
from __future__ import annotations

import random
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ShopperPersona

router = APIRouter(tags=["seed"])

CHANNELS = ("whatsapp", "sms", "email", "rcs")


class SeedPersonasRequest(BaseModel):
    customer_ids: list[uuid.UUID]
    reset: bool = False


class SeedPersonasResponse(BaseModel):
    personas_created: int


def _make_persona(customer_id: uuid.UUID) -> ShopperPersona:
    """Randomize a hidden persona. Affinities are intentionally varied per channel
    so the agent has something real (not a coin flip) to discover later."""
    affinity = {ch: round(random.uniform(0.05, 0.95), 2) for ch in CHANNELS}
    return ShopperPersona(
        customer_id=customer_id,
        channel_affinity=affinity,
        price_sensitivity=round(random.uniform(0.0, 1.0), 2),
        base_buy_propensity=round(random.uniform(0.02, 0.30), 3),
        fatigue_threshold=random.randint(2, 7),
        current_fatigue=0,
    )


@router.post("/seed-personas", response_model=SeedPersonasResponse)
def seed_personas(
    payload: SeedPersonasRequest, db: Session = Depends(get_db)
) -> SeedPersonasResponse:
    """Create one hidden persona per provided customer id."""
    if payload.reset:
        db.execute(delete(ShopperPersona))
        db.flush()

    personas = [_make_persona(cid) for cid in payload.customer_ids]
    db.add_all(personas)
    db.commit()
    return SeedPersonasResponse(personas_created=len(personas))
