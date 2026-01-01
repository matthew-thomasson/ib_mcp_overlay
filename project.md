# IB_MCP Overlay — LLM-Driven Trading Assistant (Slack Integrated)

runs on a pi5 192.168.0.202

## Overview

This project is a **private overlay** that extends the public `IB_MCP` stack **without forking or modifying it**.  
All custom logic is layered on via `docker-compose.override.yml`.

The overlay adds:
- continuous market monitoring
- trend detection
- LLM-based reasoning
- Slack-based human confirmation
- draft (non-executing) trade preparation

Upstream `IB_MCP` remains untouched and updateable.

---

## Primary Use Case

Build an **AI-assisted trading workflow** that watches markets, reasons about trends, and involves the user before any action.

The system behaves like a **trading analyst + assistant**, not an automated trader.

---

## End-to-End Flow

### 1. User Intent Definition
- User specifies (via Slack or config):
  - symbols to watch (stocks, ETFs, futures)
  - types of trends to identify (breakout, reversal, momentum, range break, etc.)
  - sensitivity or thresholds
- The system must support configuring and inspecting current intent (symbols/thresholds) so you can verify what is actively being monitored.
- The LLM may help interpret vague intent (e.g. *“watch QQQ for weakness”*).

---

### 2. Market Polling & Trend Detection (WHEN)
- A **Poller service** connects to the already-authenticated IBKR Gateway.
- Continuously polls or streams prices.
- Tracks short price history and computes simple signals.
- Identifies events such as:
  - trend start
  - trend confirmation
  - invalidation
- Applies debouncing and cooldowns.
- Emits **events only** (no decisions).

---

### 3. LLM Decisioning (WHAT)
- Events are sent to a **LangGraph-based decision engine**.
- The LLM:
  - interprets the event
  - aligns it with user intent
  - evaluates confidence and relevance
- Output is a **reasoned assessment**, not an order.

---

### 4. Slack Notification
- The system posts a structured message to Slack:
  - what happened
  - why it matters
  - confidence level
  - proposed next step
- Example:
  > “QQQ has broken below its recent range with increasing momentum.  
  > This aligns with your ‘downside momentum’ watch.”

---

### 5. Human Confirmation (Required)
- Slack message includes actions:
  - ✅ Approve
  - ❌ Ignore
  - 🔄 Adjust parameters
- **No trade proceeds without explicit user confirmation.**

---

### 6. Draft Trade Creation
- Upon approval:
  - a **draft trade** is created (not executed)
  - includes:
    - instrument
    - size suggestion
    - order type
    - rationale
    - risk notes
- User may review, edit, or execute manually later.

---

## Architecture (Overlay Only)

### Components

#### Poller — *WHEN*
- Talks to IBKR Gateway (via upstream stack).
- Polls / streams prices.
- Detects trends and signals.
- Maintains minimal state.
- Emits structured events.

No LLMs. No trading logic.

---

#### LangGraph Decision Engine — *WHAT*
- Receives events from the poller.
- Uses an LLM to reason about significance.
- Models multi-step logic:
  - detect → confirm → propose → wait
- Produces explanations and proposed actions.

---

#### Slack Integration — *HUMAN-IN-THE-LOOP*
- Primary user interface.
- Delivers notifications.
- Collects confirmations.
- Ensures user control and auditability.

---

## Separation of Concerns
IB_MCP (upstream)

IBKR authentication

market data access

Overlay

Poller: detects trends

LangGraph: reasons about meaning

Slack: confirms intent


---

## Non-Goals
- No automatic trade execution
- No modification of upstream code
- No hidden or silent actions
- No fully autonomous trading

---

## Design Principles
- Human-in-the-loop by default
- LLM explains, user decides
- Clear ownership boundaries
- Safe upstream updates
- Composable via Docker Compose override

---

## Outcome

The system functions as an **LLM-powered trading assistant**:
- watches markets continuously
- identifies meaningful trends
- explains opportunities clearly
- seeks explicit user confirmation
- prepares structured, reviewable trade drafts

Safe, auditable, and extensible by design.



