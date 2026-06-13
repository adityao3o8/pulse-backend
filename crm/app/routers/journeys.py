"""Journey lifecycle endpoints.

POST /segments                           — create a segment (with filter_json)
POST /campaigns                          — create a campaign
POST /journeys                           — create a journey (graph_json)
GET  /journeys/{id}                      — journey + enrollment status summary
POST /journeys/{id}/start                — enroll segment customers, assign control
GET  /journeys/{id}/enrollments          — paginated enrollment list
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..db import get_db
from ..engine.state_machine import evaluate_segment_filter
from ..models import Campaign, Journey, JourneyEnrollment, Segment

router = APIRouter(tags=["journeys"])


# ── Segment ───────────────────────────────────────────────────────────────────

class SegmentCreate(BaseModel):
    name: str
    nl_query: str | None = None
    filter_json: dict | None = None


class SegmentOut(BaseModel):
    id: uuid.UUID
    name: str
    filter_json: dict | None = None


@router.post("/segments", response_model=SegmentOut, status_code=201)
def create_segment(payload: SegmentCreate, db: Session = Depends(get_db)) -> SegmentOut:
    seg = Segment(
        name=payload.name,
        nl_query=payload.nl_query,
        filter_json=payload.filter_json,
        created_by="marketer",
    )
    db.add(seg)
    db.commit()
    return SegmentOut(id=seg.id, name=seg.name, filter_json=seg.filter_json)


# ── Campaign ──────────────────────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    name: str
    goal: str | None = None
    segment_id: uuid.UUID | None = None


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    goal: str | None = None
    segment_id: uuid.UUID | None = None
    status: str


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)) -> CampaignOut:
    if payload.segment_id and db.get(Segment, payload.segment_id) is None:
        raise HTTPException(status_code=404, detail="segment not found")
    campaign = Campaign(
        name=payload.name,
        goal=payload.goal,
        segment_id=payload.segment_id,
    )
    db.add(campaign)
    db.commit()
    return CampaignOut(
        id=campaign.id, name=campaign.name, goal=campaign.goal,
        segment_id=campaign.segment_id, status=campaign.status,
    )


# ── Journey ───────────────────────────────────────────────────────────────────

class JourneyCreate(BaseModel):
    campaign_id: uuid.UUID
    graph_json: dict


class JourneyOut(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    status: str
    graph_json: dict | None = None
    enrollment_summary: dict | None = None


@router.post("/journeys", response_model=JourneyOut, status_code=201)
def create_journey(payload: JourneyCreate, db: Session = Depends(get_db)) -> JourneyOut:
    if db.get(Campaign, payload.campaign_id) is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if "start" not in payload.graph_json or "nodes" not in payload.graph_json:
        raise HTTPException(status_code=422, detail="graph_json must contain 'start' and 'nodes'")
    journey = Journey(campaign_id=payload.campaign_id, graph_json=payload.graph_json)
    db.add(journey)
    db.commit()
    return JourneyOut(
        id=journey.id, campaign_id=journey.campaign_id,
        status=journey.status, graph_json=journey.graph_json,
    )


@router.get("/journeys/{journey_id}", response_model=JourneyOut)
def get_journey(journey_id: uuid.UUID, db: Session = Depends(get_db)) -> JourneyOut:
    journey = db.get(Journey, journey_id)
    if journey is None:
        raise HTTPException(status_code=404, detail="journey not found")
    rows = db.execute(
        select(JourneyEnrollment.status, func.count().label("n"))
        .where(JourneyEnrollment.journey_id == journey_id)
        .group_by(JourneyEnrollment.status)
    ).all()
    summary = {row.status: row.n for row in rows}
    return JourneyOut(
        id=journey.id, campaign_id=journey.campaign_id,
        status=journey.status, graph_json=journey.graph_json,
        enrollment_summary=summary,
    )


# ── Start ─────────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    control_pct: float = 0.20


class StartResult(BaseModel):
    journey_id: uuid.UUID
    enrolled: int
    treatment: int
    control: int
    skipped: int  # already-enrolled customers (idempotent restart)


@router.post("/journeys/{journey_id}/start", response_model=StartResult)
def start_journey(
    journey_id: uuid.UUID,
    payload: StartRequest = StartRequest(),
    db: Session = Depends(get_db),
) -> StartResult:
    journey = db.get(Journey, journey_id)
    if journey is None:
        raise HTTPException(status_code=404, detail="journey not found")
    if journey.status == "completed":
        raise HTTPException(status_code=409, detail="journey already completed")
    if not journey.graph_json:
        raise HTTPException(status_code=422, detail="journey has no graph_json")

    campaign = db.get(Campaign, journey.campaign_id) if journey.campaign_id else None
    segment = db.get(Segment, campaign.segment_id) if campaign and campaign.segment_id else None
    customers = evaluate_segment_filter(db, segment.filter_json if segment else None)

    start_node = journey.graph_json.get("start", "END")
    now = datetime.now(timezone.utc)
    enrolled = treatment = control = skipped = 0

    for customer in customers:
        is_control = random.random() < payload.control_pct
        result = db.execute(
            pg_insert(JourneyEnrollment)
            .values(
                id=uuid.uuid4(),
                journey_id=journey_id,
                customer_id=customer.id,
                current_node_id=start_node,
                status="active",
                is_control=is_control,
                entered_node_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_je_journey_customer")
        )
        if result.rowcount == 1:
            enrolled += 1
            if is_control:
                control += 1
            else:
                treatment += 1
        else:
            skipped += 1

    journey.status = "running"
    db.commit()

    return StartResult(
        journey_id=journey_id,
        enrolled=enrolled, treatment=treatment, control=control, skipped=skipped,
    )


# ── Enrollment list ───────────────────────────────────────────────────────────

class EnrollmentOut(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    current_node_id: str
    status: str
    is_control: bool
    entered_node_at: datetime


@router.get("/journeys/{journey_id}/enrollments", response_model=list[EnrollmentOut])
def list_enrollments(
    journey_id: uuid.UUID,
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> list[EnrollmentOut]:
    if db.get(Journey, journey_id) is None:
        raise HTTPException(status_code=404, detail="journey not found")
    stmt = (
        select(JourneyEnrollment)
        .where(JourneyEnrollment.journey_id == journey_id)
        .order_by(JourneyEnrollment.entered_node_at)
        .limit(limit)
    )
    if status:
        stmt = stmt.where(JourneyEnrollment.status == status)
    rows = db.execute(stmt).scalars().all()
    return [
        EnrollmentOut(
            id=e.id,
            customer_id=e.customer_id,
            current_node_id=e.current_node_id,
            status=e.status,
            is_control=e.is_control,
            entered_node_at=e.entered_node_at,
        )
        for e in rows
    ]
