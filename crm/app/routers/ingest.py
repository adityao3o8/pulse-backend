"""Ingestion endpoints. Checkpoint 1 ships /seed; /customers and /orders land later.

/seed generates CRM data (customers + orders) in the CRM DB, then asks the
channel service over HTTP to generate one HIDDEN persona per customer in its own
DB. The CRM never opens the persona DB or sees persona contents — it only learns
how many were created.
"""
from __future__ import annotations

import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
from faker import Faker
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Customer, Order

router = APIRouter(tags=["ingest"])

fake = Faker("en_IN")

CHANNEL_SERVICE_URL = os.getenv("CHANNEL_SERVICE_URL", "http://localhost:8001")
SEED_DEFAULT_COUNT = int(os.getenv("SEED_DEFAULT_COUNT", "500"))

CITIES = [
    "Mumbai", "Delhi", "Bangalore", "Chennai", "Hyderabad", "Pune", "Kolkata",
]

# Curated Indian name pool — a deliberate mix of North/Hindi and South Indian
# given names and surnames so generated customers read like a real CRM book.
FIRST_NAMES = [
    # North / Hindi-belt
    "Rahul", "Priya", "Amit", "Sneha", "Vikram", "Anjali", "Rohan", "Pooja",
    "Karan", "Neha", "Arjun", "Kavita", "Sanjay", "Divya", "Manish", "Ritu",
    "Aditya", "Shreya", "Nikhil", "Aarti", "Vivek", "Meera", "Gaurav", "Isha",
    "Siddharth", "Tanvi", "Harsh", "Simran", "Akash", "Nidhi",
    # South Indian
    "Arjun", "Kavya", "Karthik", "Lakshmi", "Surya", "Deepak", "Ananya",
    "Vignesh", "Swathi", "Harini", "Naveen", "Divya", "Pradeep", "Bhavana",
    "Ramesh", "Sowmya", "Aravind", "Keerthi", "Manoj", "Nithya", "Sandeep",
    "Revathi", "Ashwin", "Pavithra", "Vishnu", "Anusha", "Charan", "Gayathri",
]
SURNAMES = [
    # North / West / East
    "Sharma", "Verma", "Gupta", "Patel", "Singh", "Agarwal", "Kapoor", "Mehta",
    "Joshi", "Malhotra", "Chopra", "Bhatia", "Saxena", "Mishra", "Yadav",
    "Chauhan", "Desai", "Bansal", "Khanna", "Sethi", "Banerjee", "Mukherjee",
    "Das", "Ghosh", "Deshmukh", "Kulkarni",
    # South
    "Nair", "Reddy", "Iyer", "Iyengar", "Menon", "Rao", "Naidu", "Pillai",
    "Krishnan", "Subramanian", "Raman", "Chandran", "Hegde", "Shetty",
    "Gowda", "Murthy", "Varma", "Achar",
]


def _random_name() -> str:
    """A plausible 'First Last' Indian name from the curated pools."""
    return f"{random.choice(FIRST_NAMES)} {random.choice(SURNAMES)}"


def _email_for(name: str, taken: set[str]) -> str:
    """Deterministic-ish, collision-free email derived from a customer's name."""
    base = name.lower().replace(" ", ".")
    candidate = f"{base}@example.com"
    suffix = 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}{suffix}@example.com"
    taken.add(candidate)
    return candidate

# Small product catalog: (sku, name, category, price).
CATALOG = [
    ("APP-TSH-01", "Cotton Crew Tee", "Apparel", 799),
    ("APP-JNS-02", "Slim Fit Jeans", "Apparel", 2499),
    ("APP-JKT-03", "Bomber Jacket", "Apparel", 3999),
    ("FTW-SNK-04", "Running Sneakers", "Footwear", 4299),
    ("FTW-SND-05", "Leather Sandals", "Footwear", 1599),
    ("ELC-EAR-06", "Wireless Earbuds", "Electronics", 5999),
    ("ELC-PWB-07", "20K Power Bank", "Electronics", 1899),
    ("ELC-WCH-08", "Smart Watch", "Electronics", 8999),
    ("HOM-MUG-09", "Ceramic Mug Set", "Home", 999),
    ("HOM-BSH-10", "Bedsheet Combo", "Home", 1799),
    ("BTY-SRM-11", "Vitamin C Serum", "Beauty", 1299),
    ("BTY-PRF-12", "Eau de Parfum", "Beauty", 2799),
    ("GRO-COF-13", "Arabica Coffee 500g", "Grocery", 649),
    ("GRO-NUT-14", "Mixed Nuts 1kg", "Grocery", 1099),
]

DORMANT_FRACTION = 0.18  # buyers whose last order is 90-300 days old
NEVER_ORDERED_FRACTION = 0.08  # customers with zero orders


class SeedSummary(BaseModel):
    customers_created: int
    orders_created: int
    personas_created: int
    total_revenue: float
    dormant_customers: int
    never_ordered: int
    elapsed_seconds: float


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _order_count() -> int:
    """Skewed: many light buyers, few heavy ones (1..12)."""
    return min(12, max(1, int(random.expovariate(1 / 2.5)) + 1))


def _make_items() -> tuple[list[dict], Decimal]:
    items: list[dict] = []
    total = Decimal("0")
    for sku, name, category, price in random.sample(CATALOG, random.randint(1, 3)):
        qty = random.randint(1, 3)
        total += _money(price * qty)
        items.append(
            {"sku": sku, "name": name, "category": category, "qty": qty, "price": float(price)}
        )
    return items, total


def _request_personas(customer_ids: list[str], reset: bool) -> int:
    """Ask the channel service to create hidden personas for these customers."""
    resp = httpx.post(
        f"{CHANNEL_SERVICE_URL}/seed-personas",
        json={"customer_ids": customer_ids, "reset": reset},
        timeout=60.0,
    )
    resp.raise_for_status()
    return int(resp.json()["personas_created"])


@router.post("/seed", response_model=SeedSummary)
def seed(
    count: int = Query(default=None, ge=1, le=5000, description="Customers to generate"),
    reset: bool = Query(default=False, description="Wipe existing customers/orders first"),
    db: Session = Depends(get_db),
) -> SeedSummary:
    """Generate ~`count` realistic customers + orders, plus a hidden persona each.

    Idempotency guard: refuses to seed when customers already exist unless reset=true.
    """
    n = count or SEED_DEFAULT_COUNT
    start = time.perf_counter()

    existing = db.scalar(select(func.count()).select_from(Customer)) or 0
    if existing and not reset:
        raise HTTPException(
            status_code=409,
            detail=f"{existing} customers already exist. Pass ?reset=true to wipe and reseed.",
        )
    if reset:
        db.execute(delete(Order))
        db.execute(delete(Customer))
        db.flush()

    now = datetime.now(timezone.utc)
    customers: list[Customer] = []
    orders: list[Order] = []
    taken_emails: set[str] = set()
    dormant = 0
    never = 0

    for _ in range(n):
        signup = now - timedelta(days=random.randint(1, 730))
        name = _random_name()
        cust = Customer(
            id=uuid.uuid4(),
            name=name,
            email=_email_for(name, taken_emails),
            phone=fake.msisdn()[:10],
            city=random.choice(CITIES),
            signup_date=signup,
            total_orders=0,
            total_spend=Decimal("0.00"),
        )

        roll = random.random()
        if roll < NEVER_ORDERED_FRACTION:
            never += 1
            customers.append(cust)
            continue

        is_dormant = roll < NEVER_ORDERED_FRACTION + DORMANT_FRACTION
        if is_dormant:
            recency_days = random.randint(90, 300)
            dormant += 1
        else:
            recency_days = random.randint(0, 75)
        latest = now - timedelta(days=recency_days)
        earliest = signup + timedelta(days=1)
        if latest < earliest:
            latest = earliest

        span = max(1, int((latest - earliest).total_seconds()))
        order_dates = sorted(
            earliest + timedelta(seconds=random.randint(0, span)) for _ in range(_order_count())
        )
        order_dates[-1] = latest  # pin most recent to the recency window

        spend = Decimal("0.00")
        for od in order_dates:
            items, amount = _make_items()
            orders.append(
                Order(id=uuid.uuid4(), customer_id=cust.id, amount=amount, items=items, order_date=od)
            )
            spend += amount

        cust.total_orders = len(order_dates)
        cust.total_spend = spend
        cust.first_order_date = order_dates[0]
        cust.last_order_date = order_dates[-1]
        customers.append(cust)

    db.add_all(customers)
    db.add_all(orders)
    db.commit()

    # Hand off to the channel service to create hidden personas (separate DB).
    try:
        personas = _request_personas([str(c.id) for c in customers], reset=reset)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Customers seeded, but channel-service persona generation failed: {exc}",
        ) from exc

    total_revenue = float(sum((o.amount for o in orders), Decimal("0.00")))
    return SeedSummary(
        customers_created=len(customers),
        orders_created=len(orders),
        personas_created=personas,
        total_revenue=round(total_revenue, 2),
        dormant_customers=dormant,
        never_ordered=never,
        elapsed_seconds=round(time.perf_counter() - start, 3),
    )


class RenameSummary(BaseModel):
    customers_renamed: int
    elapsed_seconds: float


@router.post("/customers/rename", response_model=RenameSummary)
def rename_customers(
    update_email: bool = Query(
        default=True, description="Also regenerate email to match the new name"
    ),
    db: Session = Depends(get_db),
) -> RenameSummary:
    """Give every EXISTING customer a curated Indian name, in place.

    Non-destructive: only `name` (and optionally `email`) change. All campaigns,
    communications, orders, journeys and enrollments keep their customer_id FKs,
    so existing demo data continues to work — just with real-looking names.
    """
    start = time.perf_counter()
    taken_emails: set[str] = set()
    customers = db.execute(select(Customer)).scalars().all()

    for cust in customers:
        cust.name = _random_name()
        if update_email:
            cust.email = _email_for(cust.name, taken_emails)

    db.commit()
    return RenameSummary(
        customers_renamed=len(customers),
        elapsed_seconds=round(time.perf_counter() - start, 3),
    )
