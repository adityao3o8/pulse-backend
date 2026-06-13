"""Journey state machine — advances a single enrollment through its graph_json.

Node types: send, wait, branch, split, END.

No HTTP calls are made here. send nodes create Communication rows and return
PendingDispatch items; the scheduler fires them asynchronously after commit.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import Communication, CommunicationEvent, Customer, Segment

if TYPE_CHECKING:
    from ..models import Journey, JourneyEnrollment

logger = logging.getLogger(__name__)

# 1 hour = TIME_SCALE seconds. Default 10: 24h wait → 240s (4 min) for demo.
# Set TIME_SCALE=3600 for real-time; TIME_SCALE=1 for fast unit tests.
TIME_SCALE = float(os.getenv("TIME_SCALE", "10.0"))


@dataclass
class PendingDispatch:
    """Communication row created during a tick, awaiting async channel dispatch."""
    comm_id: uuid.UUID
    customer_id: uuid.UUID
    channel: str
    content: str | None
    campaign_id: uuid.UUID | None


def advance_enrollment(
    db: Session,
    enrollment: "JourneyEnrollment",
    journey: "Journey",
    now: datetime,
) -> list[PendingDispatch]:
    """Advance enrollment through the graph; return communications to dispatch.

    Processes nodes until blocked on a wait or reaching END. Mutates enrollment
    fields (current_node_id, entered_node_at, status) in-place; caller commits.
    """
    graph = journey.graph_json or {}
    nodes = graph.get("nodes", {})
    pending: list[PendingDispatch] = []

    for _ in range(50):  # guard against malformed cycles
        node_id = enrollment.current_node_id

        if node_id == "END" or node_id not in nodes:
            enrollment.status = "completed"
            return pending

        node = nodes[node_id]
        node_type = node.get("type", "")

        if node_type == "END":
            enrollment.status = "completed"
            return pending

        elif node_type == "send":
            if not enrollment.is_control:
                comm = Communication(
                    id=uuid.uuid4(),
                    customer_id=enrollment.customer_id,
                    campaign_id=journey.campaign_id,
                    journey_node_id=node_id,
                    channel=node.get("channel", "email"),
                    variant=node.get("variant"),
                    content=node.get("content"),
                    status="queued",
                )
                db.add(comm)
                db.flush()  # materialise comm.id before commit
                pending.append(PendingDispatch(
                    comm_id=comm.id,
                    customer_id=enrollment.customer_id,
                    channel=comm.channel,
                    content=comm.content,
                    campaign_id=journey.campaign_id,
                ))
            enrollment.current_node_id = node.get("next", "END")
            enrollment.entered_node_at = now

        elif node_type == "wait":
            required = float(node.get("hours", 0)) * TIME_SCALE
            elapsed = (now - enrollment.entered_node_at).total_seconds()
            if elapsed >= required:
                enrollment.current_node_id = node.get("next", "END")
                enrollment.entered_node_at = now
            else:
                return pending  # blocked until next tick

        elif node_type == "branch":
            event_name = node.get("on", "")
            last_comm = db.execute(
                select(Communication)
                .where(
                    Communication.customer_id == enrollment.customer_id,
                    Communication.campaign_id == journey.campaign_id,
                )
                .order_by(Communication.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            event_found = last_comm is not None and db.execute(
                select(CommunicationEvent)
                .where(
                    CommunicationEvent.communication_id == last_comm.id,
                    CommunicationEvent.event_type == event_name,
                )
                .limit(1)
            ).scalar_one_or_none() is not None

            enrollment.current_node_id = node["if_true"] if event_found else node["if_false"]
            enrollment.entered_node_at = now

        elif node_type == "split":
            # is_control already assigned at enrollment; just advance past this node
            enrollment.current_node_id = node.get("next", "END")
            enrollment.entered_node_at = now

        else:
            logger.warning(
                "Unknown node type %r at node %s (enrollment %s) — skipping",
                node_type, node_id, enrollment.id,
            )
            enrollment.current_node_id = node.get("next", "END")
            enrollment.entered_node_at = now

    logger.error(
        "MAX_STEPS exceeded for enrollment %s — possible cycle in graph", enrollment.id
    )
    return pending


# ── Segment filter evaluator ─────────────────────────────────────────────────

_ALLOWED_FIELDS = frozenset({
    "last_order_days_ago", "total_orders", "total_spend", "city_in", "never_ordered"
})
_NUMERIC_OPS: dict[str, str] = {
    "gt": "__gt__", "lt": "__lt__",
    "gte": "__ge__", "lte": "__le__",
    "eq": "__eq__",
}


def evaluate_segment_filter(db: Session, filter_json: dict | None) -> list[Customer]:
    """Return customers matching filter_json using an allowlist of safe conditions.

    Supported keys:
      last_order_days_ago.{gt,lt,gte,lte}  — days since last_order_date
      total_orders.{gt,lt,gte,lte,eq}
      total_spend.{gt,lt,gte,lte}
      city_in                               — list of city strings
      never_ordered                         — bool

    Unknown keys are silently ignored. Empty / null filter returns ALL customers.
    """
    if not filter_json:
        return list(db.execute(select(Customer)).scalars().all())

    conditions = []
    now = datetime.now(timezone.utc)

    for field, expr in filter_json.items():
        if field not in _ALLOWED_FIELDS:
            logger.warning("Ignoring unknown segment filter field %r", field)
            continue

        if field == "last_order_days_ago" and isinstance(expr, dict):
            for op, value in expr.items():
                if op not in _NUMERIC_OPS:
                    continue
                threshold = now - timedelta(days=float(value))
                if op in ("gt", "gte"):
                    # "ordered more than N days ago" → last_order_date < threshold
                    conditions.append(and_(
                        Customer.last_order_date.isnot(None),
                        Customer.last_order_date < threshold,
                    ))
                elif op in ("lt", "lte"):
                    # "ordered less than N days ago" → last_order_date > threshold
                    conditions.append(and_(
                        Customer.last_order_date.isnot(None),
                        Customer.last_order_date > threshold,
                    ))

        elif field == "total_orders" and isinstance(expr, dict):
            for op, value in expr.items():
                sa_op = _NUMERIC_OPS.get(op)
                if sa_op:
                    conditions.append(getattr(Customer.total_orders, sa_op)(int(value)))

        elif field == "total_spend" and isinstance(expr, dict):
            for op, value in expr.items():
                sa_op = _NUMERIC_OPS.get(op)
                if sa_op:
                    conditions.append(getattr(Customer.total_spend, sa_op)(float(value)))

        elif field == "city_in" and isinstance(expr, list):
            conditions.append(Customer.city.in_(expr))

        elif field == "never_ordered":
            if expr:
                conditions.append(Customer.total_orders == 0)
            else:
                conditions.append(Customer.total_orders > 0)

    stmt = select(Customer)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return list(db.execute(stmt).scalars().all())
