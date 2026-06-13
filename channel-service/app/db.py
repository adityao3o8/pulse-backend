"""SQLAlchemy engine/session/Base for the channel-service DB (pulse_channel).

This DB holds the HIDDEN shopper personas. The CRM never connects here — that
separation is the whole point of the architecture (ARCHITECTURE.md §1, §3).
"""
from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://pulse:pulse@localhost:5432/pulse_channel"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base for channel-service models and Alembic's metadata."""


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
