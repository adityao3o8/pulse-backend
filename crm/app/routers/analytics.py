"""Analytics endpoints — campaign-level lift, per-channel breakdown, funnel."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..analytics.incrementality import get_campaign_stats, get_channel_stats, get_funnel
from ..db import get_db
from ..models import Campaign

router = APIRouter(prefix="/analytics", tags=["analytics"])


class CampaignStats(BaseModel):
    campaign_id: uuid.UUID
    treatment_count: int
    treatment_conversions: int
    treatment_conversion_rate: float
    control_count: int
    control_conversions: int
    control_conversion_rate: float
    incremental_lift: float
    avg_order_value: float
    attributed_revenue: float


class ChannelStats(BaseModel):
    channel: str
    treatment_count: int
    treatment_conversions: int
    treatment_conversion_rate: float
    control_conversion_rate: float
    incremental_lift: float


class FunnelStage(BaseModel):
    stage: str
    count: int


def _get_campaign_or_404(campaign_id: uuid.UUID, db: Session) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


@router.get("/campaigns/{campaign_id}/summary", response_model=CampaignStats)
def campaign_summary(campaign_id: uuid.UUID, db: Session = Depends(get_db)) -> CampaignStats:
    _get_campaign_or_404(campaign_id, db)
    return CampaignStats(**get_campaign_stats(db, campaign_id))


@router.get("/campaigns/{campaign_id}/channels", response_model=list[ChannelStats])
def campaign_channels(campaign_id: uuid.UUID, db: Session = Depends(get_db)) -> list[ChannelStats]:
    _get_campaign_or_404(campaign_id, db)
    return [ChannelStats(**row) for row in get_channel_stats(db, campaign_id)]


@router.get("/campaigns/{campaign_id}/funnel", response_model=list[FunnelStage])
def campaign_funnel(campaign_id: uuid.UUID, db: Session = Depends(get_db)) -> list[FunnelStage]:
    _get_campaign_or_404(campaign_id, db)
    return [FunnelStage(**row) for row in get_funnel(db, campaign_id)]
