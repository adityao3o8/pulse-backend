"""Agent endpoints — goal -> campaign plan (ARCHITECTURE.md §6).

POST /agent/plan-campaign   — compile a plain-language goal into draft segment +
                              campaign + journey + A/B copy, with an auditable
                              agent_decisions log. Create-only; starting/enrolling
                              the journey remains POST /journeys/{id}/start.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent.planner import PlanValidationError, plan_campaign
from ..agent.reasoner import CampaignNotFound, analyze_and_propose
from ..db import get_db
from ..models import AgentDecision

router = APIRouter(prefix="/agent", tags=["agent"])


class PlanCampaignRequest(BaseModel):
    goal: str
    name: str | None = None


class DecisionOut(BaseModel):
    id: uuid.UUID
    step: str
    reasoning: str | None = None
    evidence_json: dict | None = None
    created_at: datetime


class PlanCampaignResponse(BaseModel):
    campaign_id: uuid.UUID
    segment_id: uuid.UUID
    journey_id: uuid.UUID
    filter_json: dict
    graph_json: dict
    messages: dict
    decisions: list[DecisionOut]


@router.post("/plan-campaign", response_model=PlanCampaignResponse, status_code=201)
def plan_campaign_endpoint(
    payload: PlanCampaignRequest, db: Session = Depends(get_db)
) -> PlanCampaignResponse:
    if not payload.goal or not payload.goal.strip():
        raise HTTPException(status_code=422, detail="goal is required")

    try:
        plan = plan_campaign(payload.goal, db)
    except PlanValidationError as exc:
        raise HTTPException(status_code=422, detail=f"agent produced an invalid plan: {exc}")

    # Re-read the persisted decisions so created_at / id come straight from the DB.
    # All rows share one transaction timestamp, so sort by the logical step order
    # (the sequence the agent reasoned in) for the Reasoning Panel.
    _STEP_ORDER = {"segment": 0, "journey_design": 1, "copy": 2, "channel_choice": 3}
    rows = db.execute(
        select(AgentDecision).where(AgentDecision.campaign_id == plan.campaign_id)
    ).scalars().all()
    rows.sort(key=lambda r: (_STEP_ORDER.get(r.step, 99), r.created_at))

    return PlanCampaignResponse(
        campaign_id=plan.campaign_id,
        segment_id=plan.segment_id,
        journey_id=plan.journey_id,
        filter_json=plan.filter_json,
        graph_json=plan.graph_json,
        messages=plan.messages,
        decisions=[
            DecisionOut(
                id=r.id, step=r.step, reasoning=r.reasoning,
                evidence_json=r.evidence_json, created_at=r.created_at,
            )
            for r in rows
        ],
    )


# ── Adaptation (checkpoint 8) ──────────────────────────────────────────────────

class AdaptationProposalOut(BaseModel):
    campaign_id: uuid.UUID
    campaign_status: str
    reasoning: str
    recommended_filters: dict
    recommended_channels: list[str]
    recommended_graph_shape: dict
    channel_verdicts: list[dict]
    evidence: dict
    decision_id: uuid.UUID


@router.get("/campaigns/{campaign_id}/adaptation", response_model=AdaptationProposalOut)
def campaign_adaptation(
    campaign_id: uuid.UUID, db: Session = Depends(get_db)
) -> AdaptationProposalOut:
    """Analyze a campaign's results and propose next-campaign adjustments (no auto-apply)."""
    try:
        proposal = analyze_and_propose(campaign_id, db)
    except CampaignNotFound:
        raise HTTPException(status_code=404, detail="campaign not found")
    return AdaptationProposalOut(**asdict(proposal))
