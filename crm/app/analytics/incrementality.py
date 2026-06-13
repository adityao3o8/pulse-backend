"""Pure-SQL incrementality analytics (ARCHITECTURE.md §7).

Three functions, one per endpoint. All use sqlalchemy.text() with named
parameters — no ORM, no string interpolation.

Conversion is defined as:
  treatment: has a 'converted' communication_event OR an order in the attribution window
  control:   has an order in the attribution window
Attribution window = [enrollment.entered_node_at, MAX(comms.created_at) + 7 days]
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

_FUNNEL_ORDER = ["total", "sent", "delivered", "opened", "read", "clicked", "converted", "failed"]

# ── Shared CTE fragment ───────────────────────────────────────────────────────

_SHARED_CTES = """
enrollments AS (
    SELECT je.customer_id, je.is_control, je.entered_node_at
    FROM journey_enrollments je
    JOIN journeys j ON j.id = je.journey_id
    WHERE j.campaign_id = :campaign_id
),
window_end AS (
    SELECT COALESCE(MAX(created_at), NOW()) + INTERVAL '7 days' AS ts
    FROM communications
    WHERE campaign_id = :campaign_id
),
"""


def get_campaign_stats(db: Session, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Treatment vs control conversion rates and incremental lift for a campaign."""
    sql = text(f"""
        WITH
        {_SHARED_CTES}
        treatment_converted AS (
            SELECT DISTINCT e.customer_id
            FROM enrollments e
            WHERE e.is_control = FALSE AND (
                EXISTS (
                    SELECT 1 FROM communications c
                    JOIN communication_events ce ON ce.communication_id = c.id
                    WHERE c.campaign_id = :campaign_id
                      AND c.customer_id = e.customer_id
                      AND ce.event_type = 'converted'
                )
                OR EXISTS (
                    SELECT 1 FROM orders o, window_end w
                    WHERE o.customer_id = e.customer_id
                      AND o.order_date >= e.entered_node_at
                      AND o.order_date <= w.ts
                )
            )
        ),
        control_converted AS (
            SELECT DISTINCT e.customer_id
            FROM enrollments e
            WHERE e.is_control = TRUE AND EXISTS (
                SELECT 1 FROM orders o, window_end w
                WHERE o.customer_id = e.customer_id
                  AND o.order_date >= e.entered_node_at
                  AND o.order_date <= w.ts
            )
        )
        SELECT
            COUNT(*) FILTER (WHERE NOT is_control)     AS treatment_count,
            COUNT(*) FILTER (WHERE is_control)         AS control_count,
            (SELECT COUNT(*) FROM treatment_converted) AS treatment_conversions,
            (SELECT COUNT(*) FROM control_converted)   AS control_conversions,
            COALESCE(
                (SELECT AVG(o.amount)
                 FROM orders o
                 JOIN communications c ON c.id = o.attributed_communication_id
                 WHERE c.campaign_id = :campaign_id),
                (SELECT AVG(amount) FROM orders),
                0
            ) AS avg_order_value
        FROM enrollments
    """)
    row = db.execute(sql, {"campaign_id": campaign_id}).mappings().one()

    treatment_count = int(row["treatment_count"])
    control_count = int(row["control_count"])
    treatment_conversions = int(row["treatment_conversions"])
    control_conversions = int(row["control_conversions"])
    avg_order_value = float(row["avg_order_value"] or 0)

    treatment_rate = treatment_conversions / treatment_count if treatment_count else 0.0
    control_rate = control_conversions / control_count if control_count else 0.0
    lift = treatment_rate - control_rate
    attributed_revenue = lift * treatment_count * avg_order_value

    return {
        "campaign_id": campaign_id,
        "treatment_count": treatment_count,
        "treatment_conversions": treatment_conversions,
        "treatment_conversion_rate": round(treatment_rate, 4),
        "control_count": control_count,
        "control_conversions": control_conversions,
        "control_conversion_rate": round(control_rate, 4),
        "incremental_lift": round(lift, 4),
        "avg_order_value": round(avg_order_value, 2),
        "attributed_revenue": round(attributed_revenue, 2),
    }


def get_channel_stats(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    """Per-channel treatment conversion rates with campaign-level control rate for comparison."""
    sql = text(f"""
        WITH
        {_SHARED_CTES}
        channel_customers AS (
            SELECT DISTINCT c.channel, c.customer_id, e.entered_node_at
            FROM communications c
            JOIN enrollments e ON e.customer_id = c.customer_id AND e.is_control = FALSE
            WHERE c.campaign_id = :campaign_id
        ),
        channel_converted AS (
            SELECT DISTINCT c.channel, c.customer_id
            FROM communications c
            JOIN enrollments e ON e.customer_id = c.customer_id AND e.is_control = FALSE
            WHERE c.campaign_id = :campaign_id AND (
                EXISTS (
                    SELECT 1 FROM communication_events ce
                    WHERE ce.communication_id = c.id AND ce.event_type = 'converted'
                )
                OR EXISTS (
                    SELECT 1 FROM orders o, window_end w
                    WHERE o.customer_id = c.customer_id
                      AND o.order_date >= e.entered_node_at
                      AND o.order_date <= w.ts
                )
            )
        ),
        control_stats AS (
            SELECT
                COUNT(*) FILTER (WHERE is_control)   AS control_count,
                COUNT(*) FILTER (
                    WHERE is_control AND EXISTS (
                        SELECT 1 FROM orders o2, window_end w
                        WHERE o2.customer_id = enrollments.customer_id
                          AND o2.order_date >= enrollments.entered_node_at
                          AND o2.order_date <= w.ts
                    )
                )                                    AS control_conversions
            FROM enrollments
        )
        SELECT
            cc.channel,
            COUNT(DISTINCT cc.customer_id)   AS treatment_count,
            COUNT(DISTINCT ccv.customer_id)  AS treatment_conversions,
            cs.control_count,
            cs.control_conversions
        FROM channel_customers cc
        LEFT JOIN channel_converted ccv
            ON ccv.channel = cc.channel AND ccv.customer_id = cc.customer_id
        CROSS JOIN control_stats cs
        GROUP BY cc.channel, cs.control_count, cs.control_conversions
        ORDER BY cc.channel
    """)
    rows = db.execute(sql, {"campaign_id": campaign_id}).mappings().all()

    result = []
    for row in rows:
        treatment_count = int(row["treatment_count"])
        treatment_conversions = int(row["treatment_conversions"])
        control_count = int(row["control_count"])
        control_conversions = int(row["control_conversions"])

        treatment_rate = treatment_conversions / treatment_count if treatment_count else 0.0
        control_rate = control_conversions / control_count if control_count else 0.0

        result.append({
            "channel": row["channel"],
            "treatment_count": treatment_count,
            "treatment_conversions": treatment_conversions,
            "treatment_conversion_rate": round(treatment_rate, 4),
            "control_conversion_rate": round(control_rate, 4),
            "incremental_lift": round(treatment_rate - control_rate, 4),
        })
    return result


def get_funnel(db: Session, campaign_id: uuid.UUID) -> list[dict[str, Any]]:
    """Count of communications at each event stage for funnel visualization."""
    sql = text("""
        SELECT ce.event_type AS stage, COUNT(DISTINCT ce.communication_id) AS count
        FROM communication_events ce
        JOIN communications c ON c.id = ce.communication_id
        WHERE c.campaign_id = :campaign_id
        GROUP BY ce.event_type

        UNION ALL

        SELECT 'total' AS stage, COUNT(*) AS count
        FROM communications
        WHERE campaign_id = :campaign_id
    """)
    rows = db.execute(sql, {"campaign_id": campaign_id}).mappings().all()
    counts: dict[str, int] = {row["stage"]: int(row["count"]) for row in rows}

    return [
        {"stage": stage, "count": counts.get(stage, 0)}
        for stage in _FUNNEL_ORDER
        if stage in counts or stage == "total"
    ]
