"""Pulse channel-service — "reality": delivery + hidden shopper simulation.

Checkpoint 1 only exposes persona seeding; /dispatch + the simulator arrive in
checkpoint 2/3.
"""
from __future__ import annotations

from fastapi import FastAPI

from .routers import dispatch, seed

app = FastAPI(title="Pulse Channel Service", version="0.1.0")

app.include_router(seed.router)
app.include_router(dispatch.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
