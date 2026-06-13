"""Campaign drill-down endpoints — live per-customer views for the UI.

GET /campaigns/{id}/communications  — recent message log joined to customers,
                                      with the latest known status per comm.
GET /campaigns/{id}/converters      — customers who converted, with the journey
                                      path they took and attributed revenue.

Both are read-only and use sqlalchemy.text() with named parameters (no ORM
fetch loops), matching the analytics module's style. Status is read from the
denormalized communications.status (already forward-only, §5) rather than
re-derived from the event log.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Campaign

router = APIRouter(tags=["campaigns"])


def _get_campaign_or_404(campaign_id: uuid.UUID, db: Session) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


# ── Communications log ─────────────────────────────────────────────────────────

class CommunicationOut(BaseModel):
    id: uuid.UUID
    customer_name: str
    customer_city: str | None = None
    channel: str
    status: str
    content: str | None = None
    created_at: datetime
    updated_at: datetime


@router.get("/campaigns/{campaign_id}/communications", response_model=list[CommunicationOut])
def campaign_communications(
    campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[CommunicationOut]:
    """50 most recent communications for a campaign, joined to the customer."""
    _get_campaign_or_404(campaign_id, db)
    sql = text("""
        SELECT
            c.id,
            cu.name AS customer_name,
            cu.city AS customer_city,
            c.channel,
            c.status,
            c.content,
            c.created_at,
            COALESCE(MAX(ce.occurred_at), c.created_at) AS updated_at
        FROM communications c
        JOIN customers cu ON cu.id = c.customer_id
        LEFT JOIN communication_events ce ON ce.communication_id = c.id
        WHERE c.campaign_id = :campaign_id
        GROUP BY c.id, cu.name, cu.city, c.channel, c.status, c.content, c.created_at
        ORDER BY c.created_at DESC
        LIMIT 50
    """)
    rows = db.execute(sql, {"campaign_id": campaign_id}).mappings().all()
    return [CommunicationOut(**row) for row in rows]


# ── Top converters ─────────────────────────────────────────────────────────────

class ConverterOut(BaseModel):
    customer_id: uuid.UUID
    name: str
    city: str | None = None
    channel: str
    journey_path: str
    revenue: float
    converted_at: datetime


@router.get("/campaigns/{campaign_id}/converters", response_model=list[ConverterOut])
def campaign_converters(
    campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[ConverterOut]:
    """Customers who converted, ranked by attributed revenue (top 20).

    A converter has a 'converted' communication_event in this campaign. The
    journey path is that converting message's event lifecycle; revenue is the
    sum of their orders inside the attribution window [enrolled, last_comm+7d],
    matching analytics' incrementality definition.
    """
    _get_campaign_or_404(campaign_id, db)
    sql = text("""
        WITH
        enr AS (
            SELECT je.customer_id, MIN(je.entered_node_at) AS entered_at
            FROM journey_enrollments je
            JOIN journeys j ON j.id = je.journey_id
            WHERE j.campaign_id = :campaign_id
            GROUP BY je.customer_id
        ),
        win AS (
            SELECT COALESCE(MAX(created_at), NOW()) + INTERVAL '7 days' AS ts
            FROM communications
            WHERE campaign_id = :campaign_id
        ),
        conv AS (
            SELECT
                c.customer_id,
                (array_agg(c.channel ORDER BY ce.occurred_at DESC))[1] AS channel,
                (array_agg(c.id ORDER BY ce.occurred_at DESC))[1]      AS comm_id,
                MAX(ce.occurred_at)                                     AS converted_at
            FROM communications c
            JOIN communication_events ce ON ce.communication_id = c.id
            WHERE c.campaign_id = :campaign_id AND ce.event_type = 'converted'
            GROUP BY c.customer_id
        )
        SELECT
            conv.customer_id,
            cu.name,
            cu.city,
            conv.channel,
            conv.converted_at,
            COALESCE((
                SELECT string_agg(ce2.event_type, ' → ' ORDER BY ce2.sequence)
                FROM communication_events ce2
                WHERE ce2.communication_id = conv.comm_id
            ), 'converted') AS journey_path,
            -- Revenue from orders placed inside the attribution window; if the
            -- conversion produced no new order (simulated demo events), fall back
            -- to the customer's typical basket (avg order value) as an estimate.
            COALESCE(
                NULLIF((
                    SELECT SUM(o.amount)
                    FROM orders o
                    WHERE o.customer_id = conv.customer_id
                      AND o.order_date >= COALESCE(enr.entered_at, conv.converted_at - INTERVAL '30 days')
                      AND o.order_date <= win.ts
                ), 0),
                cu.total_spend / NULLIF(cu.total_orders, 0),
                0
            ) AS revenue
        FROM conv
        JOIN customers cu ON cu.id = conv.customer_id
        LEFT JOIN enr ON enr.customer_id = conv.customer_id
        CROSS JOIN win
        ORDER BY revenue DESC, conv.converted_at DESC
        LIMIT 20
    """)
    rows = db.execute(sql, {"campaign_id": campaign_id}).mappings().all()
    return [
        ConverterOut(
            customer_id=row["customer_id"],
            name=row["name"],
            city=row["city"],
            channel=row["channel"],
            journey_path=row["journey_path"],
            revenue=float(row["revenue"] or 0),
            converted_at=row["converted_at"],
        )
        for row in rows
    ]
