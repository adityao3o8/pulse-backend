"""Async callback dispatcher — the in-process queue half of the callback loop (§5).

Each timeline event is fired as an independent, jittered asyncio task that POSTs
to CRM /receipts with retries + backoff. Because the tasks are scheduled
independently with jitter, callbacks can genuinely arrive **out of order** — which
is exactly what the CRM's idempotent, forward-only /receipts must tolerate.

At scale this queue would be Redis/SQS; in-process gives the same correctness
guarantees (paired with the CRM idempotency table) at this volume.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import httpx

from .simulator import TimelineEvent

logger = logging.getLogger(__name__)

# Callbacks go to the CRM's /receipts endpoint. Render's blueprint provisions
# this as CRM_CALLBACK_URL; CRM_BASE_URL is kept as a fallback for local/dev so
# an unset/renamed var doesn't silently send callbacks to localhost in prod.
CRM_CALLBACK_URL = os.getenv("CRM_CALLBACK_URL") or os.getenv(
    "CRM_BASE_URL", "http://localhost:8000"
)
TIME_SCALE = float(os.getenv("TIME_SCALE", "1.0"))  # compress simulated time for demos
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
DISPATCH_JITTER = float(os.getenv("DISPATCH_JITTER", "0.4"))  # seconds of arrival jitter
DUPLICATE_PROB = float(os.getenv("DUPLICATE_PROB", "0.15"))  # chance to resend (prove dedup)

# Keep references so fire-and-forget tasks aren't garbage-collected mid-flight.
_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _idempotency_key(communication_id: str, ev: TimelineEvent) -> str:
    """Deterministic per logical event — reused across retries AND duplicates so
    the CRM dedups them to a single row."""
    return f"{communication_id}:{ev.sequence}:{ev.event_type}"


async def _post_with_retries(client: httpx.AsyncClient, payload: dict) -> None:
    """POST one callback to CRM /receipts, retrying non-2xx / errors with backoff + jitter."""
    url = f"{CRM_CALLBACK_URL}/receipts"
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(url, json=payload, timeout=10.0)
            logger.info(f"Callback response: {resp.status_code} {resp.text[:100]}")
            if resp.status_code < 300:
                return
        except httpx.HTTPError as e:
            logger.error(f"Callback FAILED: {e}")
        backoff = 0.2 * (2 ** attempt) + random.uniform(0, 0.2)
        await asyncio.sleep(backoff)
    logger.error(
        f"Callback gave up after {MAX_RETRIES} attempts to {url} "
        f"(key={payload.get('idempotency_key')})"
    )


async def _fire_event(communication_id: str, ev: TimelineEvent, base_time: datetime) -> None:
    # Whole body wrapped so a fire-and-forget task can never fail silently — any
    # error here would otherwise be swallowed by asyncio and leave the comm stuck.
    try:
        # Wait until this event's (scaled) moment, plus jitter so arrival order can differ
        # from sequence order.
        delay = ev.offset_seconds * TIME_SCALE + random.uniform(0, DISPATCH_JITTER)
        await asyncio.sleep(delay)

        payload = {
            "communication_id": communication_id,
            "event_type": ev.event_type,
            # occurred_at is the true (monotonic) event time, independent of arrival jitter.
            "occurred_at": (base_time + timedelta(seconds=ev.offset_seconds)).isoformat(),
            "sequence": ev.sequence,
            "idempotency_key": _idempotency_key(communication_id, ev),
        }

        logger.info(
            f"Sending callback to {CRM_CALLBACK_URL}/receipts for comm "
            f"{communication_id} event {ev.event_type}"
        )
        async with httpx.AsyncClient() as client:
            await _post_with_retries(client, payload)
            # Occasionally resend the identical callback to exercise CRM dedup live.
            if random.random() < DUPLICATE_PROB:
                await _post_with_retries(client, payload)
    except Exception as e:
        logger.error(f"Callback FAILED: {e}")


def dispatch(communication_id: str, timeline: list[TimelineEvent]) -> None:
    """Schedule async callbacks for every event in the timeline (non-blocking)."""
    base_time = datetime.now(timezone.utc)
    for ev in timeline:
        _track(asyncio.create_task(_fire_event(communication_id, ev, base_time)))
