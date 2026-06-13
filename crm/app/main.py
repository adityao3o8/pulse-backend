"""Pulse CRM — FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .engine import scheduler
from .routers import agent, analytics, campaigns, ingest, journeys, receipts, send


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler.scheduler_loop())
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
