"""Pulse CRM — FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .engine import scheduler
from .routers import agent, analytics, campaigns, ingest, journeys, receipts, send

# Ensure application INFO logs (scheduler start, dispatch URL/response) reach
# Render's log stream. Without this, the root logger defaults to WARNING and
# every app-level logger.info(...) is silently dropped — which makes a healthy
# scheduler look dead.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _on_scheduler_exit(task: asyncio.Task) -> None:
    """Surface a crashed scheduler task. A bare asyncio task that raises is
    otherwise swallowed silently, leaving communications stuck on QUEUED with
    no clue in the logs."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Scheduler task exited unexpectedly", exc_info=exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler.scheduler_loop())
    task.add_done_callback(_on_scheduler_exit)
    logger.info("Scheduler started successfully")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Pulse CRM", version="0.2.0", lifespan=lifespan)

# Allow all origins so the deployed frontend can call the API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(send.router)
app.include_router(receipts.router)
app.include_router(journeys.router)
app.include_router(analytics.router)
app.include_router(campaigns.router)
app.include_router(agent.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
