"""Journey scheduler — background loop that advances active enrollments.

Two-phase design per tick:
  1. DB phase  (sync)  — advance enrollments, create Communication rows, commit
  2. Dispatch  (async) — fire channel /dispatch for each new communication

Keeping the phases separate means no sync HTTP calls inside a DB transaction
and no event-loop blocking from SQLAlchemy. Each phase is independent: if
dispatch fails for a comm, the enrollment has already advanced and the comm
row persists as 'queued' for visibility.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Journey, JourneyEnrollment
from .state_machine import PendingDispatch, advance_enrollment

logger = logging.getLogger(__name__)

CHANNEL_SERVICE_URL = os.getenv("CHANNEL_SERVICE_URL", "http://localhost:8001")
SCHEDULER_INTERVAL  = float(os.getenv("SCHEDULER_INTERVAL", "5.0"))   # seconds between ticks
BATCH_SIZE          = int(os.getenv("SCHEDULER_BATCH", "200"))         # enrollments per tick


async def scheduler_loop() -> None:
    """Runs forever; started as an asyncio task from the FastAPI lifespan."""
    logger.info(
        "Journey scheduler started — interval=%.1fs batch=%d TIME_SCALE=%s",
        SCHEDULER_INTERVAL, BATCH_SIZE, os.getenv("TIME_SCALE", "10.0"),
    )
    while True:
        try:
            pending = _tick_db()
            if pending:
                await _dispatch_all(pending)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(SCHEDULER_INTERVAL)


def _tick_db() -> list[PendingDispatch]:
    """DB phase: advance every active enrollment and return comms to dispatch.

    SELECT … FOR UPDATE SKIP LOCKED ensures a second scheduler instance (or a
    future scale-out deployment) skips rows already held by this instance.
    """
    with SessionLocal() as db:
        enrollments = db.execute(
            select(JourneyEnrollment)
            .where(JourneyEnrollment.status == "active")
            .with_for_update(skip_locked=True)
            .limit(BATCH_SIZE)
        ).scalars().all()

        if not enrollments:
            return []

        pending: list[PendingDispatch] = []
        now = datetime.now(timezone.utc)

        for enrollment in enrollments:
            journey = db.get(Journey, enrollment.journey_id)
            if journey is None:
                logger.warning(
                    "Journey %s missing for enrollment %s — skipping",
                    enrollment.journey_id, enrollment.id,
                )
                continue
            pending.extend(advance_enrollment(db, enrollment, journey, now))

        db.commit()
        return pending


async def _dispatch_all(pending: list[PendingDispatch]) -> None:
    """Async dispatch: POST channel /dispatch for every new communication."""
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_fire_one(client, p) for p in pending],
            return_exceptions=True,
        )
    for p, result in zip(pending, results):
        if isinstance(result, Exception):
            logger.warning("Dispatch failed for comm %s: %s", p.comm_id, result)


async def _fire_one(client: httpx.AsyncClient, p: PendingDispatch) -> None:
    resp = await client.post(
        f"{CHANNEL_SERVICE_URL}/dispatch",
        json={
            "communication_id": str(p.comm_id),
            "customer_id": str(p.customer_id),
            "channel": p.channel,
            "content": p.content,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
