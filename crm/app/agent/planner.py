"""The forward agent: goal -> segment + journey + copy (ARCHITECTURE.md §6).

`plan_campaign(goal, db)` runs four reasoning steps, each of which:
  - asks the LLM for a constrained JSON artifact (or uses a deterministic
    fallback when no GEMINI_API_KEY / on parse/validation failure),
  - validates the artifact against an allowlist (the model never emits SQL),
  - persists draft records and writes an auditable `agent_decisions` row.

Steps: segment -> journey_design -> copy -> channel_choice.

This is the forward pass only; the adaptation loop (reasoner.py) is checkpoint 8.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session

from ..engine.state_machine import (
    _ALLOWED_FIELDS as ALLOWED_FILTER_FIELDS,
    _NUMERIC_OPS as NUMERIC_OPS,
    evaluate_segment_filter,
)
from ..models import AgentDecision, Campaign, Journey, Segment
from .llm import LLMParseError, LLMQuotaError, NoLLMKey, extract_json, query_llm

logger = logging.getLogger(__name__)

VALID_CHANNELS = ("whatsapp", "sms", "email", "rcs")
VALID_BRANCH_EVENTS = ("opened", "clicked", "converted", "failed", "read", "delivered", "sent")
VALID_NODE_TYPES = ("send", "wait", "branch", "END")
COPY_PLACEHOLDERS = ("{customer_name}", "{discount}", "{city}", "{last_product}")

_SYSTEM = (
    "You are a CRM growth strategist. You translate a marketer's plain-language goal into a "
    "structured campaign plan. Respond with ONLY a single JSON object — no prose, no markdown "
    "code fences, no commentary."
)

# Per-step system prompts. The descriptive guidance lands in the `reasoning`
# field of the JSON the model returns. These flow through query_llm to BOTH
# providers (Gemini primary, Groq fallback) so analysis quality is equally
# specific whichever one answers.
_SYSTEM_SEGMENT = _SYSTEM + (
    "\n\nBe specific and descriptive. Explain WHY you chose this segment, "
    "what customer behavior signals you detected, and what the business impact "
    "will be. Minimum 3 sentences of reasoning. Never give generic one-line answers."
)
_SYSTEM_JOURNEY = _SYSTEM + (
    "\n\nExplain the psychological reasoning behind each channel choice. "
    "Why WhatsApp first? What customer mindset are you targeting? How does the "
    "wait time affect conversion? Be specific, minimum 4 sentences. Never give "
    "generic answers."
)
_SYSTEM_COPY = _SYSTEM + (
    "\n\nExplain the copywriting strategy. What emotional triggers are you using? "
    "Why this specific offer? How does personalization affect open rates? "
    "Minimum 3 sentences of strategic reasoning. Reference the specific goal and "
    "customer segment."
)


class PlanValidationError(ValueError):
    """Raised when a (validated) agent artifact violates the structural contract."""


@dataclass
class CampaignPlan:
    segment_id: uuid.UUID
    campaign_id: uuid.UUID
    journey_id: uuid.UUID
    filter_json: dict
    graph_json: dict
    messages: dict
    decisions: list[dict] = field(default_factory=list)


# ── Public entry point ────────────────────────────────────────────────────────

def plan_campaign(goal: str, db: Session) -> CampaignPlan:
    """Compile a goal into draft segment + campaign + journey + copy, logging each step."""
    decisions: list[dict] = []

    # 1. Segment ----------------------------------------------------------------
    filter_json, seg_reasoning, seg_source = _compile_filter(goal)
    segment = Segment(
        name=_short_name(goal, "Segment"),
        nl_query=goal,
        filter_json=filter_json,
        created_by="agent",
    )
    db.add(segment)
    db.flush()  # materialise segment.id

    matched = evaluate_segment_filter(db, filter_json)
    matched_count = len(matched)

    # 2. Campaign (needed before logging — agent_decisions FKs to campaigns) ----
    campaign = Campaign(
        name=_short_name(goal, "Campaign"),
        goal=goal,
        segment_id=segment.id,
        status="draft",
        created_by_agent=True,
    )
    db.add(campaign)
    db.flush()  # materialise campaign.id

    decisions.append(_log_decision(
        db, campaign.id, "segment", seg_reasoning,
        {"goal": goal, "filter_json": filter_json,
         "matched_count": matched_count, "source": seg_source},
    ))

    # 3. Journey design ---------------------------------------------------------
    graph_json, jrn_reasoning, jrn_source = _design_journey(goal)
    decisions.append(_log_decision(
        db, campaign.id, "journey_design", jrn_reasoning,
        {"graph_json": graph_json,
         "node_count": len(graph_json.get("nodes", {})), "source": jrn_source},
    ))

    # 4. Copy -------------------------------------------------------------------
    send_nodes = {
        nid: node for nid, node in graph_json.get("nodes", {}).items()
        if isinstance(node, dict) and node.get("type") == "send"
    }
    messages, copy_reasoning, copy_source = _draft_copy(goal, send_nodes)
    # Inject copy into the graph: content = variant A (engine reads single content
    # today); content_variants + variant_split preserved for a later checkpoint.
    for nid, variants in messages.items():
        node = graph_json["nodes"].get(nid)
        if node:
            node["content"] = variants["A"]
            node["content_variants"] = variants
            node.setdefault("variant_split", {"A": 0.5, "B": 0.5})
    decisions.append(_log_decision(
        db, campaign.id, "copy", copy_reasoning,
        {"messages": messages, "placeholders": list(COPY_PLACEHOLDERS), "source": copy_source},
    ))

    # 5. Channel choice (prior — no lift data yet) ------------------------------
    channels_used = sorted({n["channel"] for n in send_nodes.values() if n.get("channel")})
    decisions.append(_log_decision(
        db, campaign.id, "channel_choice",
        ("No historical lift data exists for this segment yet, so channels are chosen from a "
         f"prior. This plan uses: {', '.join(channels_used) or 'none'}. Future campaigns will "
         "adapt channel choice from observed per-channel incremental lift."),
        {"channels": channels_used, "source": "prior"},
    ))

    # 6. Journey record ---------------------------------------------------------
    journey = Journey(campaign_id=campaign.id, graph_json=graph_json, status="draft")
    db.add(journey)
    db.flush()

    db.commit()

    return CampaignPlan(
        segment_id=segment.id,
        campaign_id=campaign.id,
        journey_id=journey.id,
        filter_json=filter_json,
        graph_json=graph_json,
        messages=messages,
        decisions=decisions,
    )


# ── Step 1: filter compilation ────────────────────────────────────────────────

def _compile_filter(goal: str) -> tuple[dict, str, str]:
    """Return (filter_json, reasoning, source) — LLM if available, else fallback."""
    field_doc = (
        "Allowed fields and grammar (use ONLY these):\n"
        '  "last_order_days_ago": {"gt"|"lt"|"gte"|"lte"|"eq": <number of days>}\n'
        '  "total_orders":        {"gt"|"lt"|"gte"|"lte"|"eq": <integer>}\n'
        '  "total_spend":         {"gt"|"lt"|"gte"|"lte"|"eq": <number>}\n'
        '  "city_in":             ["City", ...]\n'
        '  "never_ordered":       true|false\n'
    )
    prompt = (
        f'Marketer goal: "{goal}"\n\n'
        f"{field_doc}\n"
        "Compile the goal into a customer filter. Respond as JSON:\n"
        '{"reasoning": "<one sentence why these criteria match the goal>", "filter": {...}}\n\n'
        "Examples:\n"
        'Goal "win back lapsed buyers" -> '
        '{"reasoning":"Target customers who have not ordered in over 30 days.",'
        '"filter":{"last_order_days_ago":{"gt":30}}}\n'
        'Goal "reward big spenders" -> '
        '{"reasoning":"Target customers whose lifetime spend exceeds 10000.",'
        '"filter":{"total_spend":{"gt":10000}}}'
    )
    try:
        text, source = query_llm(prompt, system=_SYSTEM_SEGMENT)
        raw = extract_json(text)
        filter_json = sanitize_filter(raw.get("filter", {}))
        if not filter_json:  # model emitted nothing usable — fall back
            raise LLMParseError("empty filter after sanitization")
        reasoning = str(raw.get("reasoning") or "Compiled from the stated goal.")
        return filter_json, reasoning, source
    except LLMQuotaError as exc:
        logger.warning("Filter: quota exceeded, using fallback (%s)", exc)
        f, r, _ = _fallback_filter(goal)
        return f, r, "quota_exceeded_fallback"
    except (NoLLMKey, LLMParseError, httpx.HTTPError) as exc:
        logger.info("Filter: using fallback (%s)", exc)
        return _fallback_filter(goal)


def sanitize_filter(raw: dict) -> dict:
    """Drop anything not on the allowlist; coerce types. The model output is never trusted."""
    if not isinstance(raw, dict):
        return {}
    clean: dict = {}
    for fieldname, expr in raw.items():
        if fieldname not in ALLOWED_FILTER_FIELDS:
            logger.info("sanitize_filter: dropping disallowed field %r", fieldname)
            continue
        if fieldname == "never_ordered":
            clean[fieldname] = bool(expr)
        elif fieldname == "city_in":
            if isinstance(expr, list):
                cities = [str(c) for c in expr if isinstance(c, (str, int))]
                if cities:
                    clean[fieldname] = cities
        elif isinstance(expr, dict):  # numeric op fields
            ops = {}
            for op, val in expr.items():
                if op not in NUMERIC_OPS:
                    continue
                try:
                    ops[op] = float(val) if "." in str(val) else int(val)
                except (TypeError, ValueError):
                    continue
            if ops:
                clean[fieldname] = ops
    return clean


def _fallback_filter(goal: str) -> tuple[dict, str, str]:
    g = goal.lower()
    if any(k in g for k in ("win back", "winback", "lapsed", "dormant", "churn", "stopped", "inactive")):
        return ({"last_order_days_ago": {"gt": 30}},
                "Goal targets lapsed customers, so select those with no order in over 30 days.",
                "fallback")
    if any(k in g for k in ("vip", "big spender", "high value", "loyal", "best customer", "top")):
        return ({"total_spend": {"gt": 10000}},
                "Goal targets high-value customers, so select lifetime spend over 10000.",
                "fallback")
    if any(k in g for k in ("never ordered", "new customer", "sign up", "signed up", "registered")):
        return ({"never_ordered": True},
                "Goal targets prospects, so select customers who have never placed an order.",
                "fallback")
    return ({"total_orders": {"gte": 1}},
            "No specific cohort detected; default to all customers with at least one order.",
            "fallback")


# ── Step 2: journey design ────────────────────────────────────────────────────

def _design_journey(goal: str) -> tuple[dict, str, str]:
    prompt = (
        f'Marketer goal: "{goal}"\n\n'
        "Design a messaging journey as a directed graph. Use ONLY these node types:\n"
        '  send:   {"type":"send","channel":"whatsapp|sms|email|rcs","next":"<node_id>"}\n'
        '  wait:   {"type":"wait","hours":<positive integer>,"next":"<node_id>"}\n'
        '  branch: {"type":"branch","on":"opened|clicked|converted|failed",'
        '"if_true":"<node_id>","if_false":"<node_id>"}\n'
        '  END:    {"type":"END"}\n\n'
        "Every next/if_true/if_false must reference a defined node id or \"END\". "
        "Keep it to 3-8 nodes.\n\n"
        "Prefer a MULTI-CHANNEL CASCADE ordered by expected engagement: open on "
        "WhatsApp (highest-affinity customers engage first), fall back to SMS for "
        "medium engagement, and rescue with email last. Branch on 'opened' after "
        "each touch so engaged customers exit early and only the unengaged escalate. "
        "Avoid sending everyone on a single channel. Respond as JSON:\n"
        '{"reasoning":"<one sentence strategy>","graph":{"start":"n1","nodes":{...}}}\n\n'
        "Example (the recommended cascade):\n"
        '{"reasoning":"WhatsApp first; escalate to SMS then email for the unengaged.",'
        '"graph":{"start":"n1","nodes":{'
        '"n1":{"type":"send","channel":"whatsapp","next":"n2"},'
        '"n2":{"type":"wait","hours":24,"next":"n3"},'
        '"n3":{"type":"branch","on":"opened","if_true":"END","if_false":"n4"},'
        '"n4":{"type":"send","channel":"sms","next":"n5"},'
        '"n5":{"type":"wait","hours":24,"next":"n6"},'
        '"n6":{"type":"branch","on":"opened","if_true":"END","if_false":"n7"},'
        '"n7":{"type":"send","channel":"email","next":"END"},'
        '"END":{"type":"END"}}}}'
    )
    try:
        text, source = query_llm(prompt, system=_SYSTEM_JOURNEY)
        raw = extract_json(text)
        graph = raw.get("graph", {})
        validate_graph(graph)
        reasoning = str(raw.get("reasoning") or "Multi-step journey compiled from the goal.")
        return graph, reasoning, source
    except LLMQuotaError as exc:
        logger.warning("Journey: quota exceeded, using fallback (%s)", exc)
        g, r, _ = _fallback_graph()
        return g, r, "quota_exceeded_fallback"
    except (NoLLMKey, LLMParseError, PlanValidationError, httpx.HTTPError) as exc:
        logger.info("Journey: using fallback (%s)", exc)
        return _fallback_graph()


def validate_graph(graph: dict) -> None:
    """Structural allowlist for the journey graph. Raises PlanValidationError on any violation."""
    if not isinstance(graph, dict):
        raise PlanValidationError("graph is not an object")
    nodes = graph.get("nodes")
    start = graph.get("start")
    if not isinstance(nodes, dict) or not nodes:
        raise PlanValidationError("graph.nodes must be a non-empty object")
    if start not in nodes:
        raise PlanValidationError(f"graph.start {start!r} is not a defined node")

    def _ref_ok(ref) -> bool:
        return ref == "END" or ref in nodes

    for nid, node in nodes.items():
        if not isinstance(node, dict):
            raise PlanValidationError(f"node {nid!r} is not an object")
        ntype = node.get("type")
        if ntype not in VALID_NODE_TYPES:
            raise PlanValidationError(f"node {nid!r} has invalid type {ntype!r}")
        if ntype == "END":
            continue
        if ntype == "send":
            if node.get("channel") not in VALID_CHANNELS:
                raise PlanValidationError(f"send node {nid!r} has invalid channel {node.get('channel')!r}")
            if not _ref_ok(node.get("next")):
                raise PlanValidationError(f"send node {nid!r} next points nowhere: {node.get('next')!r}")
        elif ntype == "wait":
            hours = node.get("hours")
            if not isinstance(hours, (int, float)) or hours <= 0:
                raise PlanValidationError(f"wait node {nid!r} has invalid hours {hours!r}")
            if not _ref_ok(node.get("next")):
                raise PlanValidationError(f"wait node {nid!r} next points nowhere: {node.get('next')!r}")
        elif ntype == "branch":
            if node.get("on") not in VALID_BRANCH_EVENTS:
                raise PlanValidationError(f"branch node {nid!r} has invalid event {node.get('on')!r}")
            if not _ref_ok(node.get("if_true")) or not _ref_ok(node.get("if_false")):
                raise PlanValidationError(f"branch node {nid!r} has a dangling edge")


def _fallback_graph() -> tuple[dict, str, str]:
    # Optimal multi-channel cascade. Channel affinity lives in the channel
    # service (hidden from the CRM), so the agent can't read per-customer scores
    # directly. Instead it orders channels by expected engagement and lets each
    # customer self-select: high-affinity customers open the WhatsApp first touch
    # and exit early; medium-affinity customers fall through to SMS; the rest are
    # rescued on email. One branch per hop keeps low-affinity reach without
    # over-messaging the engaged.
    graph = {
        "start": "n1",
        "nodes": {
            "n1": {"type": "send", "channel": "whatsapp", "next": "n2"},
            "n2": {"type": "wait", "hours": 24, "next": "n3"},
            "n3": {"type": "branch", "on": "opened", "if_true": "END", "if_false": "n4"},
            "n4": {"type": "send", "channel": "sms", "next": "n5"},
            "n5": {"type": "wait", "hours": 24, "next": "n6"},
            "n6": {"type": "branch", "on": "opened", "if_true": "END", "if_false": "n7"},
            "n7": {"type": "send", "channel": "email", "next": "END"},
            "END": {"type": "END"},
        },
    }
    reasoning = (
        "Cascade across channels by expected engagement: open on WhatsApp for high-affinity "
        "customers; if unopened after a day, retry on SMS for medium engagement; if still "
        "unopened, rescue on email. Each customer exits as soon as they engage."
    )
    return graph, reasoning, "fallback"


# ── Step 3: copy drafting ─────────────────────────────────────────────────────

def _draft_copy(goal: str, send_nodes: dict) -> tuple[dict, str, str]:
    if not send_nodes:
        return {}, "No send nodes in the journey, so no copy was drafted.", "fallback"

    node_lines = "\n".join(
        f'  "{nid}": channel={node.get("channel")}' for nid, node in send_nodes.items()
    )
    placeholders = ", ".join(COPY_PLACEHOLDERS)
    prompt = (
        f'Marketer goal: "{goal}"\n\n'
        "Draft two distinct message variants (A and B) for each send node below. "
        f"You may use these placeholders: {placeholders}. "
        "Keep SMS/WhatsApp short (<160 chars); email can be longer.\n"
        f"Send nodes:\n{node_lines}\n\n"
        "Respond as JSON:\n"
        '{"reasoning":"<one sentence on the copy angle>",'
        '"messages":{"<node_id>":{"A":"<variant A>","B":"<variant B>"}}}'
    )
    try:
        text, source = query_llm(prompt, system=_SYSTEM_COPY)
        raw = extract_json(text)
        messages = validate_copy(raw.get("messages", {}), send_nodes)
        reasoning = str(raw.get("reasoning") or "Two variants per send node for A/B testing.")
        return messages, reasoning, source
    except LLMQuotaError as exc:
        logger.warning("Copy: quota exceeded, using fallback (%s)", exc)
        m, r, _ = _fallback_copy(goal, send_nodes)
        return m, r, "quota_exceeded_fallback"
    except (NoLLMKey, LLMParseError, httpx.HTTPError) as exc:
        logger.info("Copy: using fallback (%s)", exc)
        return _fallback_copy(goal, send_nodes)


def validate_copy(raw: dict, send_nodes: dict) -> dict:
    """Ensure every send node has non-empty A and B; fill gaps from the fallback."""
    fallback, _, _ = _fallback_copy("", send_nodes)
    messages: dict = {}
    raw = raw if isinstance(raw, dict) else {}
    for nid in send_nodes:
        entry = raw.get(nid) if isinstance(raw.get(nid), dict) else {}
        a = str(entry.get("A") or "").strip()
        b = str(entry.get("B") or "").strip()
        messages[nid] = {
            "A": a or fallback[nid]["A"],
            "B": b or fallback[nid]["B"],
        }
    return messages


def _fallback_copy(goal: str, send_nodes: dict) -> tuple[dict, str, str]:
    messages: dict = {}
    for nid, node in send_nodes.items():
        channel = node.get("channel", "email")
        if channel in ("sms", "whatsapp"):
            messages[nid] = {
                "A": "Hi {customer_name}, we miss you! Here's {discount} off your next order.",
                "B": "{customer_name}, your favourites are waiting — {discount} off, today only.",
            }
        else:
            messages[nid] = {
                "A": ("Hi {customer_name}, it's been a while! Come back and enjoy {discount} off "
                      "your next order in {city}."),
                "B": ("{customer_name}, we saved your spot. Rediscover {last_product} and take "
                      "{discount} off when you return."),
            }
    return messages, "Two variants per send node, personalized with customer attributes.", "fallback"


# ── Decision logging ──────────────────────────────────────────────────────────

def _log_decision(db: Session, campaign_id: uuid.UUID, step: str,
                  reasoning: str, evidence: dict) -> dict:
    decision = AgentDecision(
        campaign_id=campaign_id,
        step=step,
        reasoning=reasoning,
        evidence_json=evidence,
    )
    db.add(decision)
    db.flush()
    return {
        "id": str(decision.id),
        "step": step,
        "reasoning": reasoning,
        "evidence_json": evidence,
    }


def _short_name(goal: str, prefix: str) -> str:
    trimmed = goal.strip()
    if len(trimmed) > 48:
        trimmed = trimmed[:45] + "..."
    return f"{prefix}: {trimmed}" if trimmed else prefix
