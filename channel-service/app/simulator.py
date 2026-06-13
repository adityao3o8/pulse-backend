"""Outcome simulator — builds an event timeline for a dispatched message.

Checkpoint 3 wires hidden ShopperPersona attributes into outcome probabilities:
  P(open|delivered)  = channel_affinity[channel] × max(1 − fatigue_ratio, 0)
  P(click|read)      = clamp(base_buy_propensity × 4, 0.05, 0.85)
  P(convert|clicked) = clamp(base_buy_propensity × (1 + price_sensitivity × 0.5 × has_offer), 0.02, 0.95)
Delivery (0.90) and read (0.70) remain persona-agnostic.
persona=None falls back to the original coin-flip constants.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ShopperPersona

# Coin-flip fallback constants used when persona=None or channel key is missing.
_DELIVER_PROB = 0.90
_OPEN_PROB = 0.60
_READ_PROB = 0.70
_CLICK_PROB = 0.40
_CONVERT_PROB = 0.50

_OFFER_KEYWORDS = ("%", "discount", "offer", "deal", "promo", "sale", "coupon", " off")


@dataclass(frozen=True)
class TimelineEvent:
    sequence: int  # monotonic per communication
    event_type: str
    offset_seconds: float  # cumulative simulated time since send (drives occurred_at)


def _gap(mean: float) -> float:
    """A jittered positive gap around `mean` seconds."""
    return max(0.05, random.uniform(0.5 * mean, 1.5 * mean))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _has_offer(content: str | None) -> bool:
    if not content:
        return False
    lower = content.lower()
    return any(kw in lower for kw in _OFFER_KEYWORDS)


def build_timeline(
    channel: str = "email",
    persona: "ShopperPersona | None" = None,
    content: str | None = None,
) -> list[TimelineEvent]:
    """Return an ordered, monotonic timeline ending in conversion, drop-off, or failure.

    When persona is supplied, outcome probabilities are conditioned on their
    channel_affinity, base_buy_propensity, price_sensitivity, and current fatigue.
    persona=None falls back to the original coin-flip constants.
    """
    events: list[TimelineEvent] = []
    seq = 0
    t = 0.0

    has_offer = _has_offer(content)

    def add(event_type: str, mean_gap: float) -> None:
        nonlocal seq, t
        seq += 1
        t += _gap(mean_gap)
        events.append(TimelineEvent(sequence=seq, event_type=event_type, offset_seconds=round(t, 3)))

    # --- Delivery (persona-agnostic: channel noise, not individual preference) ---
    add("sent", 0.3)
    if random.random() > _DELIVER_PROB:
        add("failed", 0.7)
        return events
    add("delivered", 0.7)

    # --- Open ---
    if persona is not None:
        fatigue_ratio = min(persona.current_fatigue / max(persona.fatigue_threshold, 1), 1.0)
        p_open = persona.channel_affinity.get(channel, _OPEN_PROB) * max(1.0 - fatigue_ratio, 0.0)
    else:
        p_open = _OPEN_PROB
    if random.random() > p_open:
        return events
    add("opened", 3.0)

    # --- Read (persona-agnostic: no attribute maps to reading depth) ---
    if random.random() > _READ_PROB:
        return events
    add("read", 2.0)

    # --- Click ---
    if persona is not None:
        p_click = _clamp(persona.base_buy_propensity * 4.0, 0.05, 0.85)
    else:
        p_click = _CLICK_PROB
    if random.random() > p_click:
        return events
    add("clicked", 4.0)

    # --- Convert ---
    if persona is not None:
        discount_lift = persona.price_sensitivity * 0.5 if has_offer else 0.0
        p_convert = _clamp(persona.base_buy_propensity * (1.0 + discount_lift), 0.02, 0.95)
    else:
        p_convert = _CONVERT_PROB
    if random.random() > p_convert:
        return events
    add("converted", 6.0)

    return events
