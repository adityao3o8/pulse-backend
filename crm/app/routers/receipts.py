"""CRM /receipts — the idempotent callback sink (ARCHITECTURE.md §5).

Channel callbacks arrive out of order, possibly duplicated, with retries. This
endpoint deduplicates on `idempotency_key`, appends every distinct event to the
log, and advances the denormalized `communications.status` forward-only.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Communication, CommunicationEvent
from ..status import ALLOWED_EVENTS, forward_status

router = APIRouter(tags=["callbacks"])


class Receipt(BaseModel):
    communication_id: uuid.UUID
    event_type: str
    occurred_at: datetime
    sequence: int
    idempotency_key: str


class ReceiptResult(BaseModel):
    communication_id: uuid.UUID
    status: str
    event_type: str
    sequence: int
    deduplicated: bool


class EventOut(BaseModel):
    event_type: str
    occurred_at: datetime
    sequence: int
    idempotency_key: str


class CommunicationOut(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    channel: str
    variant: str | None
    content: str | None
    status: str
    events: list[EventOut]


@router.post("/receipts", response_model=ReceiptResult)
def receipts(payload: Receipt, db: Session = Depends(get_db)) -> ReceiptResult:
    """Idempotently record one channel callback and advance status forward-only."""
    if payload.event_type not in ALLOWED_EVENTS:
        raise HTTPException(status_code=422, detail=f"unknown event_type {payload.event_type!r}")

    # Lock the communication row: serializes concurrent/out-of-order callbacks for
    # THIS communication so the read-modify-write of status is atomic. Different
    # communications proceed in parallel.
    comm = db.execute(
        select(Communication).where(Communication.id == payload.communication_id).with_for_update()
    ).scalar_one_or_none()
    if comm is None:
        raise HTTPException(status_code=404, detail="unknown communication_id")

    # Idempotency guard: duplicate idempotency_key inserts nothing (no-op).
    result = db.execute(
        pg_insert(CommunicationEvent)
        .values(
            id=uuid.uuid4(),
            communication_id=payload.communication_id,
            event_type=payload.event_type,
            occurred_at=payload.occurred_at,
            sequence=payload.sequence,
            idempotency_key=payload.idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    inserted = result.rowcount == 1

    # Only a genuinely new event can move status — and only ever forward.
    if inserted:
        new_status = forward_status(comm.status, payload.event_type)
        if new_status != comm.status:
            comm.status = new_status

    db.commit()
    return ReceiptResult(
        communication_id=payload.communication_id,
        status=comm.status,
        event_type=payload.event_type,
        sequence=payload.sequence,
        deduplicated=not inserted,
    )


@router.get("/communications/{communication_id}", response_model=CommunicationOut)
def get_communication(communication_id: uuid.UUID, db: Session = Depends(get_db)) -> CommunicationOut:
    """Read a communication and its event log, ordered by sequence then occurred_at."""
    comm = db.get(Communication, communication_id)
    if comm is None:
        raise HTTPException(status_code=404, detail="unknown communication_id")

    events = db.execute(
        select(CommunicationEvent)
        .where(CommunicationEvent.communication_id == communication_id)
        .order_by(CommunicationEvent.sequence, CommunicationEvent.occurred_at)
    ).scalars().all()

    return CommunicationOut(
        id=comm.id,
        customer_id=comm.customer_id,
        channel=comm.channel,
        variant=comm.variant,
        content=comm.content,
        status=comm.status,
        events=[
            EventOut(
                event_type=e.event_type,
                occurred_at=e.occurred_at,
                sequence=e.sequence,
                idempotency_key=e.idempotency_key,
            )
            for e in events
        ],
    )
