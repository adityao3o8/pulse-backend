# Pulse — An AI Growth Agent for Reaching Shoppers

> Xeno Engineering Assignment · AI-Native Mini CRM
> Working name: **Pulse** (rename freely)

---

## 1. The Bet

Most candidates will build a campaign *sender*: pick an audience, write a message, blast it,
show a dashboard of opens and clicks. That answers the brief shallowly.

**Pulse is a self-driving growth agent.** A marketer states a goal in plain language —
*"win back customers who stopped buying"* — and the agent:

1. Finds the right shoppers (segmentation),
2. **Designs a multi-step journey** (a branching state machine: send → wait → if no open, switch channel → if clicked, send offer),
3. Runs it through a realistic channel service,
4. **Holds out a control group** and measures **true incremental lift** (not "clicked → converted"),
5. **Narrates every decision** so the marketer can interrogate its reasoning,
6. Learns from outcomes and adapts the next campaign.

The single most important design idea:

> **Customer psychology is hidden from the CRM, just like in real life.**
> Each simulated shopper has a persona (channel preference, price sensitivity, fatigue
> threshold) that lives *only* inside the channel/simulation service. The CRM — and therefore
> the agent — never sees it. The agent must *discover* what works by reading callback events.
> This is what makes the simulation a real test of intelligence and not a coin flip.

This fuses Xeno's separate grading axes into one architecture: the **callback loop** (system
design) *is* the agent's sensory input (AI-native), and the **simulation** is what gives the
agent something real to learn from.

---

## 2. Repo Structure (two repos, per the submission form)

The form requires a **backend** repo link and a **frontend** repo link separately.

### Repo 1 — `pulse-backend` (monorepo of two services)

```
pulse-backend/
├── crm/                      # FastAPI — the marketer's view of the world
│   ├── app/
│   │   ├── main.py
│   │   ├── db.py             # SQLAlchemy engine/session
│   │   ├── models.py         # ORM models (customers, orders, campaigns, journeys, …)
│   │   ├── schemas.py        # Pydantic request/response models
│   │   ├── routers/
│   │   │   ├── ingest.py     # POST /customers, /orders, /seed
│   │   │   ├── segments.py   # segment build (AI + manual)
│   │   │   ├── campaigns.py  # create/run campaigns
│   │   │   ├── journeys.py   # journey CRUD + enrollment
│   │   │   ├── send.py       # POST /send  → calls channel service
│   │   │   ├── receipts.py   # POST /receipts ← channel callbacks (idempotent)
│   │   │   └── analytics.py  # campaign stats + incremental lift
│   │   ├── agent/
│   │   │   ├── llm.py        # Groq client wrapper
│   │   │   ├── planner.py    # goal → segment + journey graph
│   │   │   ├── copywriter.py # message + variant drafting
│   │   │   └── reasoner.py   # logs decisions + reads outcomes to adapt
│   │   ├── engine/
│   │   │   ├── state_machine.py  # journey execution
│   │   │   └── scheduler.py      # advances enrolled customers (wait timers, events)
│   │   └── analytics/
│   │       └── incrementality.py # treatment − control lift, attributed revenue
│   ├── Dockerfile
│   └── requirements.txt
│
├── channel-service/          # FastAPI — "reality": delivery + hidden shopper simulation
│   ├── app/
│   │   ├── main.py
│   │   ├── personas.py       # hidden persona model + generation
│   │   ├── simulator.py      # given (shopper, message, channel) → outcome timeline
│   │   ├── dispatcher.py     # async callbacks → CRM /receipts (with retries, jitter)
│   │   └── routers/send.py   # POST /dispatch  (CRM calls this)
│   ├── Dockerfile
│   └── requirements.txt
│
├── docker-compose.yml        # postgres + crm + channel-service, one command up
├── ARCHITECTURE.md           # this file
└── README.md
```

> **Why CRM and channel-service live together but as two services:** they're the two halves of
> the callback loop. Co-locating them (monorepo) shows cohesion; keeping them as separate
> deployable services with their own Dockerfiles shows you understand the two-service boundary
> the brief is testing. In the interview: *"The form asked for backend/frontend separately, so I
> split those. Inside backend I kept the CRM and channel service together because they're a
> tightly-coupled loop, but ran them as independent services."*

### Repo 2 — `pulse-frontend` (Next.js)

```
pulse-frontend/
├── app/
│   ├── page.tsx              # goal input ("what do you want to achieve?")
│   ├── campaigns/[id]/       # journey visualization + live event stream
│   ├── agent/                # the agent's reasoning log (auditable agent)
│   └── analytics/            # incremental lift dashboard
├── components/
│   ├── JourneyGraph.tsx      # visual state machine (React Flow)
│   ├── ReasoningPanel.tsx    # "why did the agent do this?"
│   └── LiftChart.tsx
├── lib/api.ts
└── README.md
```

---

## 3. Data Model (PostgreSQL)

### CRM-owned tables (the marketer can see these)

**customers**
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| name | text | |
| email | text | |
| phone | text | |
| city | text | attribute for segmentation |
| signup_date | timestamptz | |
| first_order_date | timestamptz | derived |
| last_order_date | timestamptz | derived |
| total_orders | int | derived |
| total_spend | numeric | derived |

**orders**
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| customer_id | uuid fk | |
| amount | numeric | |
| items | jsonb | `[{sku, name, category, qty, price}]` |
| order_date | timestamptz | |
| attributed_communication_id | uuid fk null | set if an order followed a comm within the attribution window |

**segments**
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| name | text | |
| nl_query | text | the marketer's / agent's natural-language intent |
| filter_json | jsonb | compiled, executable filter (the AI compiles NL → this) |
| created_by | text | `'agent'` or `'marketer'` |

**campaigns**
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| name | text | |
| goal | text | the natural-language goal given to the agent |
| segment_id | uuid fk | |
| status | text | `draft / running / completed` |
| created_by_agent | bool | |

**journeys** — the state machine graph
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| campaign_id | uuid fk | |
| graph_json | jsonb | nodes + edges (see §4) |
| status | text | |

**journey_enrollments** — per-customer position in a journey
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| journey_id | uuid fk | |
| customer_id | uuid fk | |
| current_node_id | text | |
| status | text | `active / completed / exited` |
| is_control | bool | **holdout — receives nothing, used for lift** |
| entered_node_at | timestamptz | drives wait timers |

**communications** — one row per message attempt
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| customer_id | uuid fk | |
| campaign_id | uuid fk | |
| journey_node_id | text | which node fired it |
| channel | text | `whatsapp / sms / email / rcs` |
| variant | text | `A / B` for A/B within a node |
| content | text | the rendered message |
| status | text | latest known state (denormalized from events) |
| created_at | timestamptz | |

**communication_events** — the event log (source of truth for stats)
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| communication_id | uuid fk | |
| event_type | text | `sent / delivered / failed / opened / read / clicked / converted` |
| occurred_at | timestamptz | event time (may arrive out of order) |
| sequence | int | monotonic per communication — used to order |
| idempotency_key | text unique | **dedup key; duplicate callbacks are no-ops** |

**agent_decisions** — the auditable-agent log
| column | type | notes |
|---|---|---|
| id | uuid pk | |
| campaign_id | uuid fk | |
| step | text | `segment / journey_design / channel_choice / copy / adaptation` |
| reasoning | text | LLM-generated human-readable justification |
| evidence_json | jsonb | the data the decision was based on |
| created_at | timestamptz | |

### Channel-service-owned tables (HIDDEN from the CRM)

**shopper_personas** — the simulation's hidden truth
| column | type | notes |
|---|---|---|
| customer_id | uuid pk | matches CRM customer id |
| channel_affinity | jsonb | `{whatsapp:0.8, sms:0.3, email:0.5, rcs:0.4}` open-propensity per channel |
| price_sensitivity | float | how much a discount moves them, 0–1 |
| base_buy_propensity | float | baseline conversion likelihood |
| fatigue_threshold | int | messages-per-week before they tune out |
| current_fatigue | int | mutable; decays over time |

> The CRM never queries these. The agent's whole job is to *infer* channel_affinity and
> fatigue from observed events. That's the intelligence test.

---

## 4. The Journey Engine (state machine)

A journey is a directed graph stored as `graph_json`:

```json
{
  "start": "n1",
  "nodes": {
    "n1": { "type": "split",  "control_pct": 0.2, "next": "n2" },
    "n2": { "type": "send",   "channel": "whatsapp", "variant_split": {"A": 0.5, "B": 0.5}, "next": "n3" },
    "n3": { "type": "wait",   "hours": 24, "next": "n4" },
    "n4": { "type": "branch", "on": "opened", "if_true": "END", "if_false": "n5" },
    "n5": { "type": "send",   "channel": "sms", "next": "n6" },
    "n6": { "type": "wait",   "hours": 24, "next": "n7" },
    "n7": { "type": "branch", "on": "clicked", "if_true": "n8", "if_false": "END" },
    "n8": { "type": "send",   "channel": "email", "content_hint": "offer 15% off", "next": "END" }
  }
}
```

**Node types:**
- `split` — randomly assigns `control_pct` of enrollees to holdout (`is_control = true`, receive nothing).
- `send` — creates a `communication`, calls the channel service. Optional `variant_split` for A/B.
- `wait` — pauses the enrollee for N hours (scheduler resumes them).
- `branch` — checks whether an event (`opened`/`clicked`/`converted`) occurred for this customer's last communication; routes accordingly.
- `END` — terminal.

**The scheduler** (`engine/scheduler.py`) runs on a loop (e.g. every few seconds, sped up for
demo). For each active enrollment it: resumes expired `wait` nodes, evaluates `branch` nodes
against the event log, and fires `send` nodes. This is the heart of the system-design story.

> **Demo trick:** make "hours" a configurable time-scale (1 hour = 2 seconds in demo mode) so a
> multi-day journey plays out live in the video.

---

## 5. The Callback Loop (the system-design test)

```
CRM /send ──────────────▶ channel-service /dispatch
                                   │
                                   │ simulator picks an outcome TIMELINE based on the
                                   │ HIDDEN persona, e.g.:
                                   │   t+0.3s delivered
                                   │   t+5s    opened   (prob = channel_affinity × (1−fatigue))
                                   │   t+12s   clicked  (prob conditioned on opened)
                                   │   t+30s   converted(prob = base_buy × price_sensitivity…)
                                   ▼
                       dispatcher schedules async callbacks
                                   │
   CRM /receipts ◀─────────────────┘  (out of order, possibly duplicated, with retries)
        │
        ├─ dedup on idempotency_key      (duplicate → no-op)
        ├─ order by sequence / occurred_at
        ├─ append to communication_events
        └─ update communications.status (only forward transitions)
```

**What you must handle (and narrate):**
- **Idempotency** — `idempotency_key` unique constraint; duplicate callbacks are swallowed.
- **Out-of-order** — never let `clicked` get overwritten by a late `delivered`; status only moves forward via a state-rank map (`sent<delivered<opened<read<clicked<converted`).
- **Retries** — channel service retries callbacks on non-2xx with backoff; CRM idempotency makes this safe.
- **Failures** — some sends `failed`; journey `branch` nodes can route on that.
- **Volume** — batch enrollments; the dispatcher uses a queue so a 500-customer campaign doesn't block.

> Interview-ready line: *"I'd put the callback queue in Redis/SQS at scale; for this scope I used
> an in-process async queue with an idempotency table, which gives the same correctness
> guarantees at lower volume."*

---

## 6. The Self-Narrating Agent

`agent/planner.py` turns a goal into a plan using Groq/LLaMA 3.1:

1. **Segment** — NL goal → `filter_json` (LLM compiles intent to an executable filter; you
   validate it against an allow-list of fields so the model can't emit arbitrary SQL).
2. **Journey design** — LLM proposes a `graph_json` from a constrained node vocabulary.
3. **Copy** — LLM drafts message + A/B variants per `send` node, personalized with customer attributes.
4. **Channel choice** — initially a prior; after data exists, informed by observed lift per channel.

Every step writes an `agent_decisions` row with human-readable `reasoning` + the `evidence_json`
it used. The frontend's **Reasoning Panel** renders these so the marketer (and the interviewer)
can see *why*. This directly serves Xeno's "thought clarity & communication" axis — inside the
product.

**Adaptation loop** (`agent/reasoner.py`): after a campaign completes, the agent reads
incremental lift per segment/channel and proposes the next campaign's adjustments
("SMS produced −2% lift on segment B — likely fatigue; I'll suppress SMS for them next time").

> **Safety rail to mention:** the LLM never executes raw SQL. It emits structured JSON
> (filter / graph / copy) that your code validates and runs. This is a deliberate
> prompt-injection / correctness boundary — a great thing to be asked about.

---

## 7. Incrementality (the attribution story)

Naive attribution (`clicked → ordered`) is what everyone else ships. You do causal:

```
treatment_conversion = conversions_in_treatment / size_treatment
control_conversion   = conversions_in_control   / size_control
incremental_lift      = treatment_conversion − control_conversion
attributed_revenue    = incremental_lift × size_treatment × avg_order_value
```

The `split` node guarantees a clean control group. `analytics/incrementality.py` computes lift
per campaign, per channel, per segment. The dashboard headlines **"+8.2% incremental orders,
₹X attributable revenue"** — not vanity open rates.

---

## 8. Tech Stack

| Layer | Choice |
|---|---|
| CRM service | FastAPI (Python) |
| Channel service | FastAPI (Python), separate deployable |
| DB | PostgreSQL (SQLAlchemy + Alembic) |
| AI | Groq + LLaMA 3.1 (provider wrapped behind `agent/llm.py` so it's swappable) |
| Frontend | Next.js + React Flow (journey graph) + Framer Motion |
| Deploy | Railway (both backend services + Postgres) · Vercel (frontend) |
| Local | `docker-compose up` brings up postgres + both services |

---

## 9. Build Sequence (each step is a deployable checkpoint)

Build in this order so you always have something that runs. If anything stalls, you ship the
last green checkpoint.

1. **Schema + ingestion + seed.** Models, migrations, a `/seed` endpoint that generates ~500
   realistic customers + orders, and (separately) their hidden personas in the channel service.
2. **Single send + idempotent callback loop.** CRM `/send` → channel `/dispatch` → simulator →
   async `/receipts`. Get idempotency, ordering, retries right *here*, on one message, before
   any journey complexity. ← **most important checkpoint**
3. **Simulator with hidden personas.** Make outcomes depend on persona, not coin flips.
4. **Linear journey** (send → wait → send) via the state machine + scheduler.
5. **Branching journeys** (if opened/clicked/converted) + the `split` control group.
6. **Incrementality analytics** + dashboard.
7. **The agent** — goal → segment → journey graph → copy, with the reasoning log.
8. **Adaptation loop** — agent reads lift and adjusts.
9. **Frontend polish** — journey visualization, live event stream, reasoning panel.

> Checkpoints 1–3 are the spine. 4–6 are the differentiated core. 7–8 are the showstopper.
> 9 is what makes the video sing.

---

## 10. What We Consciously Do NOT Build

State these explicitly in the README and video — owning your cuts is graded:

- No auth / multi-tenant / RBAC (single-marketer assumption).
- No real messaging-provider integration (stubbed by design, per brief).
- No real-time websockets at scale (polling for the demo; would use a stream at scale).
- Control-group split is fixed-ratio, not an adaptive bandit (would use Thompson sampling at scale).

---

## 11. How to Feed This to Claude Code

1. Drop this file in `pulse-backend/ARCHITECTURE.md`.
2. Open the `pulse-backend` **folder** in VS Code (not a loose file).
3. In Claude Code, start in **Plan mode** and prompt, one checkpoint at a time:
   > "Read ARCHITECTURE.md. Implement checkpoint 1 only: SQLAlchemy models, Alembic migration,
   > and a /seed endpoint generating ~500 realistic customers and orders. Show me the plan first."
4. Review the plan, correct it, accept the diff. Then move to checkpoint 2.
5. **Keep two notes as you go** for the AI-native video section: one time the AI got the design
   wrong and you caught it, one time it nailed something hard. Those anecdotes are graded.
