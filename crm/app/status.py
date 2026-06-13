"""Communication status: the state-rank map and forward-only transition rule (§5).

The denormalized `communications.status` is derived from the event log. Callbacks
arrive out of order, possibly duplicated and retried, so status must only ever
move *forward* through the funnel — a late `delivered` can never clobber a real
`opened`. Status is governed by RANK, not by callback arrival time.
"""
from __future__ import annotations

# Linear funnel ranks. `queued`/`created` are the pre-send initial states (rank 0).
STATUS_RANK: dict[str, int] = {
    "queued": 0,
    "created": 0,
    "sent": 1,
    "delivered": 2,
    "opened": 3,
    "read": 4,
    "clicked": 5,
    "converted": 6,
}

FAILED = "failed"

# Event types the channel may report. `failed` is off the linear scale.
ALLOWED_EVENTS: frozenset[str] = frozenset(
    {"sent", "delivered", "opened", "read", "clicked", "converted", FAILED}
)

# A late `failed` may only win before the recipient has demonstrably engaged.
_OPENED_RANK = STATUS_RANK["opened"]


def forward_status(current: str, incoming: str) -> str:
    """Return the status after applying `incoming`, never moving backward.

    - `failed` is terminal (a sink): once failed, nothing overrides it.
    - `failed` only *wins* from the early funnel (rank < opened) — a late failure
      callback can't clobber a real open/click. Deliberate reading of "failed at
      any point" (§5), defensive against out-of-order delivery.
    - linear events advance only when strictly higher-ranked than the current state.
    """
    if current == FAILED:
        return FAILED
    if incoming == FAILED:
        return FAILED if STATUS_RANK.get(current, 0) < _OPENED_RANK else current
    if STATUS_RANK.get(incoming, 0) > STATUS_RANK.get(current, 0):
        return incoming
    return current
