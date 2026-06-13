"""CRM /send — create a communication and hand it to the channel for delivery (§5).

The communication_id is CRM-owned (generated here); the channel never invents it.
"""
from __future__ import annotations

import os
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Communication, Customer

router = APIRouter(tags=["send"])

CHANNEL_SERVICE_URL = os.getenv("CHANNEL_SERVICE_URL", "http://localhost:8001")


class SendRequest(BaseModel):
    customer_id: uuid.UUID
    channel: str  # whatsapp / sms / email / rcs
    content: str | None = None
    campaign_id: uuid.UUID | None = None
    variant: str | None = None


class SendResult(BaseModel):
    communication_id: uuid.UUID
    status: str
    dispatched: bool


@router.post("/send", response_model=SendResult)
def send(
    payload: SendRequest,
    dispatch: bool = Query(default=True, description="Call the channel service to fire callbacks"),
    db: Session = Depends(get_db),
) -> SendResult:
    """Create a queued communication and (optionally) dispatch it to the channel."""
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=404, detail="unknown customer_id")

    comm = Communication(
        id=uuid.uuid4(),
        customer_id=payload.customer_id,
        campaign_id=payload.campaign_id,
        channel=payload.channel,
        variant=payload.variant,
        content=payload.content,
        status="queued",
    )
    db.add(comm)
    db.commit()

    if not dispatch:
        return SendResult(communication_id=comm.id, status=comm.status, dispatched=False)

    try:
        resp = httpx.post(
            f"{CHANNEL_SERVICE_URL}/dispatch",
            json={
                "communication_id": str(comm.id),
                "customer_id": str(comm.customer_id),
                "channel": comm.channel,
                "content": comm.content,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Row persists as `queued`; surface the failure to the caller.
        raise HTTPException(
            status_code=502, detail=f"channel dispatch failed: {exc}"
        ) from exc

    return SendResult(communication_id=comm.id, status=comm.status, dispatched=True)
