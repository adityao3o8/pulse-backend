"""The adaptation loop: after-action analysis (ARCHITECTURE.md §6).

`analyze_and_propose(campaign_id, db)` reads a campaign's incremental lift
(per-campaign and per-channel) plus its funnel, then proposes how the *next*
campaign should differ:

  - keep channels with positive lift, suppress/switch the ones without,
  - react to the opened-vs-failed branching signal (fatigue warning),
  - hand back a structural hint for the next journey.

The recommendations are computed deterministically from the lift numbers (that
is the intelligence). Only the natural-language summary is LLM-phrased, with a
deterministic template fallback when GEMINI_API_KEY is absent.

Analyze-and-propose only — nothing is auto-applied. Each call logs one
`agent_decisions` row (step="adaptation").
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.incrementality import get_campaign_stats, get_channel_stats, get_funnel
from ..models import AgentDecision, Campaign, Journey, Segment
from .llm import LLMParseError, LLMQuotaError, NoLLMKey, query_llm

logger = logging.getLogger(__name__)

ALL_CHANNELS = ("whatsapp", "sms", "email", "rcs")
# Preference order when suggesting an untried replacement channel.
_SWITCH_PREFERENCE = ("whatsapp", "rcs", "email", "sms")
LIFT_EPSILON = 0.0  # lift > 0 outperformed; <= 0 underperformed
FATIGUE_FAILURE_RATIO = 0.25  # failed/sent above this => fatigue/deliverability warning

_SUMMARY_SYSTEM = (
    "You are a growth analyst. Given the campaign results, write a detailed post-campaign "
    "analysis in plain prose. What worked and why? What failed and what does that tell us about "
    "customer behavior? What specific changes will improve the next campaign? Reference actual "
    "numbers from the evidence. Minimum 5 sentences of analysis. "
    "No JSON, no markdown, no bullet points."
)


class CampaignNotFound(LookupError):
    """Raised when the campaign id does not exist."""


@dataclass
class AdaptationProposal:
    campaign_id: uuid.UUID
    campaign_status: str
    reasoning: str
    recommended_filters: dict
    recommended_channels: list[str]
    recommended_graph_shape: dict
    evidence: dict
    decision_id: uuid.UUID
    # Convenience echo of per-channel verdicts (also inside evidence).
    channel_verdicts: list[dict] = field(default_factory=list)


# ── Public entry point ────────────────────────────────────────────────────────

def analyze_and_propose(campaign_id: uuid.UUID, db: Session) -> AdaptationProposal:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise CampaignNotFound(str(campaign_id))

    segment = db.get(Segment, campaign.segment_id) if campaign.segment_id else None
    base_filter = (segment.filter_json if segment and segment.filter_json else {}) or {}

    journey = db.execute(
        select(Journey).where(Journey.campaign_id == campaign_id)
    ).scalars().first()
    graph = (journey.graph_json if journey else None) or {}

    stats = get_campaign_stats(db, campaign_id)
    # get_campaign_stats returns campaign_id as a UUID object; stringify it so the
    # stats dict is safe to embed in the JSONB evidence_json column.
    stats["campaign_id"] = str(stats["campaign_id"])
    channels = get_channel_stats(db, campaign_id)
    funnel = {s["stage"]: s["count"] for s in get_funnel(db, campaign_id)}

    # ── Insufficient-data guard ────────────────────────────────────────────────
    if stats["treatment_count"] == 0:
        reasoning = (
            "No customers have been enrolled or messaged for this campaign yet, so there is no "
            "lift data to learn from. Start the journey and let it run before requesting an "
            "adaptation proposal."
        )
        evidence = {
            "campaign_stats": stats, "channel_stats": channels, "funnel": funnel,
            "channel_verdicts": [], "branching_signal": {}, "source": "insufficient_data",
        }
        decision_id = _log_decision(db, campaign_id, reasoning, evidence)
        return AdaptationProposal(
            campaign_id=campaign_id, campaign_status=campaign.status, reasoning=reasoning,
            recommended_filters=base_filter, recommended_channels=[],
            recommended_graph_shape={}, evidence=evidence, decision_id=decision_id,
            channel_verdicts=[],
        )

    # ── Channel verdicts (deterministic) ───────────────────────────────────────
    used_channels = sorted({
        n.get("channel") for n in graph.get("nodes", {}).values()
        if isinstance(n, dict) and n.get("type") == "send" and n.get("channel")
    })
    verdicts: list[dict] = []
    for row in channels:
        lift = row["incremental_lift"]
        verdicts.append({
            "channel": row["channel"],
            "lift": lift,
            "verdict": "keep" if lift > LIFT_EPSILON else "suppress",
        })

    kept = sorted((v for v in verdicts if v["verdict"] == "keep"),
                  key=lambda v: v["lift"], reverse=True)
    suppressed = [v for v in verdicts if v["verdict"] == "suppress"]

    # Switch suggestion: prefer an untried channel when something underperformed.
    tried = set(used_channels) | {v["channel"] for v in verdicts}
    untried = [c for c in _SWITCH_PREFERENCE if c not in tried]
    switch_suggestion = untried[0] if (suppressed and untried) else None

    recommended_channels = [v["channel"] for v in kept]
    if switch_suggestion:
        recommended_channels.append(switch_suggestion)
    if not recommended_channels:
        # Everything underperformed and nothing untried — keep the least-bad channel
        # so the next campaign still has a channel to send on.
        least_bad = max(verdicts, key=lambda v: v["lift"], default=None)
        if least_bad:
            recommended_channels = [least_bad["channel"]]

    # ── Branching / fatigue signal (deterministic, from funnel) ─────────────────
    opened = funnel.get("opened", 0)
    failed = funnel.get("failed", 0)
    clicked = funnel.get("clicked", 0)
    converted = funnel.get("converted", 0)
    sent = funnel.get("sent", 0)
    branch_events = sorted({
        n.get("on") for n in graph.get("nodes", {}).values()
        if isinstance(n, dict) and n.get("type") == "branch" and n.get("on")
    })
    failure_ratio = (failed / sent) if sent else 0.0
    fatigue = failed >= max(opened, 1) or failure_ratio > FATIGUE_FAILURE_RATIO
    opened_path_worked = opened > failed and opened > 0
    branching_signal = {
        "opened": opened, "failed": failed, "clicked": clicked, "converted": converted,
        "sent": sent, "failure_ratio": round(failure_ratio, 4),
        "branch_events_used": branch_events,
        "opened_path_worked": opened_path_worked,
        "fatigue_warning": fatigue,
    }

    primary_branch_event = branch_events[0] if branch_events else "opened"
    notes_parts = []
    if opened_path_worked:
        notes_parts.append("keep branching on opened")
    if fatigue:
        notes_parts.append("add longer waits / fewer sends to reduce fatigue")
    if switch_suggestion:
        notes_parts.append(f"try {switch_suggestion} in place of underperformers")
    recommended_graph_shape = {
        "channels_in_order": recommended_channels,
        "drop_channels": [v["channel"] for v in suppressed],
        "branch_on": primary_branch_event,
        "fatigue_mitigation": fatigue,
        "notes": "; ".join(notes_parts) or "repeat the structure that produced positive lift",
    }

    # ── Evidence + NL summary ───────────────────────────────────────────────────
    evidence = {
        "campaign_stats": stats,
        "channel_stats": channels,
        "funnel": funnel,
        "channel_verdicts": verdicts,
        "branching_signal": branching_signal,
    }
    reasoning, source = _summarize(stats, verdicts, branching_signal, switch_suggestion)
    evidence["source"] = source

    decision_id = _log_decision(db, campaign_id, reasoning, evidence)

    return AdaptationProposal(
        campaign_id=campaign_id,
        campaign_status=campaign.status,
        reasoning=reasoning,
        recommended_filters=base_filter,
        recommended_channels=recommended_channels,
        recommended_graph_shape=recommended_graph_shape,
        evidence=evidence,
        decision_id=decision_id,
        channel_verdicts=verdicts,
    )


# ── Summary (LLM-phrased, deterministic fallback) ─────────────────────────────

def _pp(value: float) -> str:
    """Format a lift decimal as signed percentage points, e.g. 0.082 -> '+8.2pp'."""
    return f"{value * 100:+.1f}pp"


def _fallback_summary(stats, verdicts, signal, switch_suggestion) -> str:
    parts = [f"Overall incremental lift was {_pp(stats['incremental_lift'])} "
             f"across {stats['treatment_count']} treated customers."]
    for v in sorted(verdicts, key=lambda x: x["lift"], reverse=True):
        if v["verdict"] == "keep":
            parts.append(f"{v['channel'].upper()} had {_pp(v['lift'])} lift — use it again.")
        else:
            tail = f" try {switch_suggestion} instead." if switch_suggestion else " suppress it."
            parts.append(f"{v['channel'].upper()} underperformed ({_pp(v['lift'])}) —{tail}")
    if signal.get("opened_path_worked"):
        parts.append(f"The opened path ({signal['opened']}) beat failures "
                     f"({signal['failed']}); branching on opened worked well.")
    if signal.get("fatigue_warning"):
        parts.append(f"High failure rate ({signal['failed']}/{signal['sent']}) suggests fatigue "
                     "— space out sends and add longer waits next time.")
    return " ".join(parts)


def _summarize(stats, verdicts, signal, switch_suggestion) -> tuple[str, str]:
    """Return (summary, source). LLM-phrased from the facts, else deterministic template."""
    fallback = _fallback_summary(stats, verdicts, signal, switch_suggestion)

    facts_lines = [f"overall_lift={_pp(stats['incremental_lift'])}",
                   f"treated={stats['treatment_count']}",
                   f"attributed_revenue={stats['attributed_revenue']}"]
    for v in verdicts:
        facts_lines.append(f"channel {v['channel']}: lift={_pp(v['lift'])} -> {v['verdict']}")
    facts_lines.append(
        f"funnel opened={signal['opened']} failed={signal['failed']} "
        f"clicked={signal['clicked']} converted={signal['converted']} sent={signal['sent']}")
    if switch_suggestion:
        facts_lines.append(f"untried_replacement={switch_suggestion}")
    facts = "\n".join(facts_lines)

    prompt = (
        "Campaign results:\n" + facts + "\n\n"
        "Write the adaptation summary for the next campaign."
    )
    try:
        raw, source = query_llm(prompt, system=_SUMMARY_SYSTEM, json_mode=False)
        text = raw.strip()
        if text:
            return text, source
        return fallback, "fallback"
    except LLMQuotaError as exc:
        logger.warning("Adaptation summary: quota exceeded, using fallback (%s)", exc)
        return fallback, "quota_exceeded_fallback"
    except (NoLLMKey, LLMParseError) as exc:
        logger.info("Adaptation summary: using fallback (%s)", exc)
        return fallback, "fallback"
    except Exception as exc:  # transport/HTTP errors shouldn't break analysis
        logger.warning("Adaptation summary LLM call failed (%s) — using fallback", exc)
        return fallback, "fallback"


# ── Decision logging ──────────────────────────────────────────────────────────

def _log_decision(db: Session, campaign_id: uuid.UUID, reasoning: str, evidence: dict) -> uuid.UUID:
    decision = AgentDecision(
        campaign_id=campaign_id,
        step="adaptation",
        reasoning=reasoning,
        evidence_json=evidence,
    )
    db.add(decision)
    db.flush()
    db.commit()
    return decision.id
