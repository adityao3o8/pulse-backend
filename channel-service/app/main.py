"""Pulse channel-service — "reality": delivery + hidden shopper simulation.

Checkpoint 1 only exposes persona seeding; /dispatch + the simulator arrive in
checkpoint 2/3.
"""
from __future__ import annotations

from fastapi import FastAPI

from . import models  # noqa: F401 — registers ShopperPersona on Base.metadata
from .db import Base, engine
from .routers import dispatch, seed

app = FastAPI(title="Pulse Channel Service", version="0.1.0")


@app.on_event("startup")
async def startup() -> None:
    # CRM and channel-service share one Render Postgres DB, so the channel
    # tables (shopper_personas) must be created here. create_all is idempotent —
    # it only creates tables that don't already exist.
    Base.metadata.create_all(bind=engine)


app.include_router(seed.router)
app.include_router(dispatch.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
