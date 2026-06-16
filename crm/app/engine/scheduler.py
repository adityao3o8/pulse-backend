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
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from ..db import SessionLocal
from ..models import Communication, Journey, JourneyEnrollment
from .state_machine import PendingDispatch, advance_enrollment

logger = logging.getLogger(__name__)

CHANNEL_SERVICE_URL = os.getenv("CHANNEL_SERVICE_URL", "http://localhost:8001")
SCHEDULER_INTERVAL  = float(os.getenv("SCHEDULER_INTERVAL", "5.0"))   # seconds between ticks
BATCH_SIZE          = int(os.getenv("SCHEDULER_BATCH", "200"))         # enrollments per tick
# Render free-tier cold starts take 30-60s; the dispatch timeout must outlast one
# so the request that *wakes* the channel service isn't the one we drop.
DISPATCH_TIMEOUT    = float(os.getenv("DISPATCH_TIMEOUT", "60.0"))     # seconds per /dispatch
# Re-dispatch comms left in 'queued' this long — self-heals sends stranded by a
# transient channel outage (e.g. a cold start that blew past DISPATCH_TIMEOUT).
REQUEUE_AFTER       = float(os.getenv("SCHEDULER_REQUEUE_AFTER", "30.0"))  # seconds


async def scheduler_loop() -> None:
    """Runs forever; started as an asyncio task from the FastAPI lifespan."""
    logger.info(
        "Journey scheduler started — interval=%.1fs batch=%d TIME_SCALE=%s",
        SCHEDULER_INTERVAL, BATCH_SIZE, os.getenv("TIME_SCALE", "10.0"),
    )
    while True:
        try:
            pending = _tick_db() + _tick_requeue()
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


def _tick_requeue() -> list[PendingDispatch]:
    """Re-dispatch communications stranded in 'queued' past REQUEUE_AFTER.

    A send whose original /dispatch failed (e.g. the channel service was cold and
    the request timed out) leaves a comm row at 'queued' with no callbacks ever
    arriving. This sweep retries it. It is self-limiting: the first 'sent' callback
    advances the comm off 'queued', so a comm is only ever re-dispatched while it
    has genuinely received nothing. created_at < cutoff avoids racing rows that
    _tick_db just created and is dispatching this same tick.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=REQUEUE_AFTER)
    with SessionLocal() as db:
        comms = db.execute(
            select(Communication)
            .where(Communication.status == "queued", Communication.created_at < cutoff)
            .limit(BATCH_SIZE)
        ).scalars().all()

        if comms:
            logger.info("Re-dispatching %d stranded queued communication(s)", len(comms))

        return [
            PendingDispatch(
                comm_id=c.id,
                customer_id=c.customer_id,
                channel=c.channel,
                content=c.content,
                campaign_id=c.campaign_id,
            )
            for c in comms
        ]


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
    logger.info(f"Dispatching to channel service: {CHANNEL_SERVICE_URL}")
    resp = await client.post(
        f"{CHANNEL_SERVICE_URL}/dispatch",
        json={
            "communication_id": str(p.comm_id),
            "customer_id": str(p.customer_id),
            "channel": p.channel,
            "content": p.content,
        },
        timeout=DISPATCH_TIMEOUT,
    )
    logger.info(f"Dispatch response status: {resp.status_code}")
    logger.info(f"Dispatch response body: {resp.text}")
    resp.raise_for_status()
