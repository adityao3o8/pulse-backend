"""SQLAlchemy 2.0 engine, session factory, and declarative Base for the CRM DB.

Reads DATABASE_URL from the environment (default: the compose `pulse_crm` DB).
The CRM only ever connects to its own database — persona data lives in a
separate channel-service DB the CRM never opens.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://pulse:pulse@localhost:5432/pulse_crm"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base shared by all CRM ORM models and Alembic's metadata."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
