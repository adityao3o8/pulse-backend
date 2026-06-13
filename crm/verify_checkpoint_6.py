#!/usr/bin/env python3
"""Checkpoint 6 verification — analytics endpoints + regression test.

Run from the crm/ directory:
    python verify_checkpoint_6.py

Requires: CRM on http://localhost:8000, Postgres reachable (same DATABASE_URL as the app).
"""
from __future__ import annotations

import sys
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select, text

sys.path.insert(0, ".")
from app.db import SessionLocal
from app.models import Campaign, Journey, JourneyEnrollment

BASE = "http://localhost:8000"
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results: list[bool] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    tag = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{tag}] {name}{suffix}")
    results.append(condition)
    return condition


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def get(path: str, **kwargs) -> dict | list:
    r = httpx.get(f"{BASE}{path}", **kwargs)
    r.raise_for_status()
    return r.json()


def post(path: str, **kwargs) -> dict:
    r = httpx.post(f"{BASE}{path}", **kwargs)
    r.raise_for_status()
    return r.json()


# ── Step 0: find most recent campaign ────────────────────────────────────────

section("Setup: finding most recent campaign with enrollments")

with SessionLocal() as db:
    row = db.execute(
        text("""
            SELECT j.campaign_id, COUNT(je.id) AS n
            FROM journeys j
            JOIN journey_enrollments je ON je.journey_id = j.id
            GROUP BY j.campaign_id
            HAVING COUNT(je.id) FILTER (WHERE je.is_control = TRUE) > 0
            ORDER BY n DESC
            LIMIT 1
        """)
    ).mappings().first()

if row is None:
    print(f"  [{FAIL}] No campaigns with enrollments found — run checkpoint 4/5 first")
    sys.exit(1)

campaign_id = str(row["campaign_id"])
enrollment_count = row["n"]
print(f"  Using campaign {campaign_id} ({enrollment_count} enrollments)")


# ── Test 1: Funnel monotonicity ───────────────────────────────────────────────

section("Test 1: Funnel monotonicity")

funnel_data: list[dict] = get(f"/analytics/campaigns/{campaign_id}/funnel")
funnel = {f["stage"]: f["count"] for f in funnel_data}
print(f"  Funnel stages: {funnel}")

ORDERED = ["total", "sent", "delivered", "opened", "clicked", "converted"]
present = [s for s in ORDERED if s in funnel]

all_mono = True
for i in range(len(present) - 1):
    a, b = present[i], present[i + 1]
    ok = funnel[a] >= funnel[b]
    check(f"{a} ({funnel[a]}) >= {b} ({funnel[b]})", ok)
    if not ok:
        all_mono = False

if len(present) < 2:
    check("At least two funnel stages present", False, f"only got {present}")
    all_mono = False

check("Funnel is monotonically non-increasing", all_mono)


# ── Test 2: Campaign summary ──────────────────────────────────────────────────

section("Test 2: Campaign summary")

summary: dict[str, Any] = get(f"/analytics/campaigns/{campaign_id}/summary")
print(f"  Summary: {summary}")

check("treatment_count > 0", summary["treatment_count"] > 0,
      str(summary["treatment_count"]))
check("control_count > 0", summary["control_count"] > 0,
      str(summary["control_count"]))
check("treatment_conversions <= treatment_count",
      summary["treatment_conversions"] <= summary["treatment_count"],
      f"{summary['treatment_conversions']} <= {summary['treatment_count']}")
check("control_conversions <= control_count",
      summary["control_conversions"] <= summary["control_count"],
      f"{summary['control_conversions']} <= {summary['control_count']}")

# Verify lift arithmetic
tc = summary["treatment_count"]
cc = summary["control_count"]
tv = summary["treatment_conversions"]
cv = summary["control_conversions"]
tr = tv / tc if tc else 0.0
cr = cv / cc if cc else 0.0
expected_lift = round(tr - cr, 4)
check("incremental_lift == treatment_rate - control_rate",
      abs(summary["incremental_lift"] - expected_lift) < 0.0001,
      f"got {summary['incremental_lift']}, expected {expected_lift}")

# Verify attributed_revenue arithmetic.
# Use unrounded lift (tr - cr) to avoid rounding compounding; allow 1-unit
# tolerance since avg_order_value in the response is already rounded to 2dp.
aov = summary["avg_order_value"]
expected_rev = round((tr - cr) * tc * aov, 2)
check("attributed_revenue ≈ lift × treatment_count × avg_order_value (±1.0)",
      abs(summary["attributed_revenue"] - expected_rev) < 1.0,
      f"got {summary['attributed_revenue']}, expected ≈{expected_rev}")

check("treatment_conversion_rate > control_conversion_rate (personas drive engagement)",
      summary["treatment_conversion_rate"] >= summary["control_conversion_rate"],
      f"{summary['treatment_conversion_rate']} vs {summary['control_conversion_rate']}")


# ── Test 3: Channel breakdown ─────────────────────────────────────────────────

section("Test 3: Channel breakdown")

channels: list[dict] = get(f"/analytics/campaigns/{campaign_id}/channels")
print(f"  Channels: {channels}")

check("At least one channel row returned", len(channels) > 0, str(len(channels)))

total_treatment_in_channels = sum(c["treatment_count"] for c in channels)
check("Sum of channel treatment_counts >= campaign treatment_count",
      total_treatment_in_channels >= summary["treatment_count"],
      f"{total_treatment_in_channels} >= {summary['treatment_count']}")

for ch in channels:
    check(f"{ch['channel']}: treatment_conversions <= treatment_count",
          ch["treatment_conversions"] <= ch["treatment_count"],
          f"{ch['treatment_conversions']} <= {ch['treatment_count']}")
    tr_ch = ch["treatment_conversions"] / ch["treatment_count"] if ch["treatment_count"] else 0.0
    check(f"{ch['channel']}: treatment_conversion_rate matches conversions/count",
          abs(ch["treatment_conversion_rate"] - round(tr_ch, 4)) < 0.0001,
          f"{ch['treatment_conversion_rate']} vs {round(tr_ch, 4)}")


# ── Test 4: Zero-enrollment campaign returns zeros ────────────────────────────

section("Test 4: Zero-enrollment campaign returns zeros (no 500)")

with SessionLocal() as db:
    empty_seg = post("/segments", json={"name": "ckpt6-empty-seg"})
    empty_camp = post("/campaigns", json={"name": "ckpt6-empty", "segment_id": empty_seg["id"]})
empty_campaign_id = empty_camp["id"]
print(f"  Empty campaign: {empty_campaign_id}")

empty_summary: dict = get(f"/analytics/campaigns/{empty_campaign_id}/summary")
check("treatment_count == 0", empty_summary["treatment_count"] == 0,
      str(empty_summary["treatment_count"]))
check("control_count == 0", empty_summary["control_count"] == 0,
      str(empty_summary["control_count"]))
check("incremental_lift == 0.0", empty_summary["incremental_lift"] == 0.0,
      str(empty_summary["incremental_lift"]))
check("attributed_revenue == 0.0", empty_summary["attributed_revenue"] == 0.0,
      str(empty_summary["attributed_revenue"]))

empty_channels: list = get(f"/analytics/campaigns/{empty_campaign_id}/channels")
check("channels list is empty []", empty_channels == [], str(empty_channels))

empty_funnel: list = get(f"/analytics/campaigns/{empty_campaign_id}/funnel")
total_in_funnel = sum(f["count"] for f in empty_funnel)
check("funnel total count == 0", total_in_funnel == 0, str(total_in_funnel))


# ── Test 5: Regression — scheduler still advances new journey ─────────────────

section("Test 5: Regression — scheduler advances a new linear journey")

# Create a tiny journey: send(email) → wait(0.01h × TIME_SCALE=10 → 0.1s) → END
seg5 = post("/segments", json={"name": "ckpt6-regression-seg",
                                "filter_json": {"total_orders": {"gt": 0}}})
camp5 = post("/campaigns", json={"name": "ckpt6-regression", "segment_id": seg5["id"]})
journey5 = post("/journeys", json={
    "campaign_id": camp5["id"],
    "graph_json": {
        "start": "n1",
        "nodes": {
            "n1": {"type": "send", "channel": "email", "content": "regression test", "next": "n2"},
            "n2": {"type": "wait", "hours": 0.005, "next": "END"},
            "END": {"type": "END"},
        },
    },
})
journey_id = journey5["id"]
print(f"  Test journey: {journey_id}")

start_result = post(f"/journeys/{journey_id}/start", json={"control_pct": 0.0})
print(f"  Enrolled: {start_result['enrolled']} (all treatment, 0% control)")
check("At least 1 customer enrolled", start_result["enrolled"] > 0,
      str(start_result["enrolled"]))

# Wait for scheduler ticks: wait(0.005h × 10 = 0.05s) — should complete within 15s
print("  Waiting up to 30s for scheduler to complete the journey…", end="", flush=True)
deadline = time.time() + 30
completed = 0
while time.time() < deadline:
    time.sleep(2)
    print(".", end="", flush=True)
    state = get(f"/journeys/{journey_id}")
    completed = state.get("enrollment_summary", {}).get("completed", 0)
    if completed >= start_result["enrolled"]:
        break
print()

check("All enrollments completed", completed >= start_result["enrolled"],
      f"completed={completed}, enrolled={start_result['enrolled']}")

# Analytics should now reflect the regression journey
reg_summary: dict = get(f"/analytics/campaigns/{camp5['id']}/summary")
check("Regression campaign: treatment_count matches enrolled",
      reg_summary["treatment_count"] == start_result["enrolled"],
      f"{reg_summary['treatment_count']} == {start_result['enrolled']}")


# ── Final result ──────────────────────────────────────────────────────────────

section("Results")
passed = sum(results)
total = len(results)
print(f"  {passed}/{total} checks passed")

if passed == total:
    print(f"\n  All checks passed.")
    sys.exit(0)
else:
    print(f"\n  {total - passed} check(s) FAILED.")
    sys.exit(1)
