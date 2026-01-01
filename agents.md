# IB_MCP Overlay - Agents & Architecture Documentation

## Project Overview

This overlay extends the [IB_MCP](https://github.com/rcontesti/IB_MCP) Model Context Protocol (MCP) server with intelligent trading agents and workflow orchestration. The design follows a layered architecture that keeps our customizations separate from the upstream IB_MCP codebase.

## Architecture Principles

### Separation of Concerns
- **Upstream Layer**: IB_MCP provides core Interactive Brokers connectivity and MCP protocol implementation
- **Overlay Layer**: Our customizations add intelligent decision-making, monitoring, and orchestration
- **No Forking**: We extend via Docker Compose overrides, not code duplication

### Communication Pattern
```
┌─────────────────────────────────────────────────────────────┐
│                     Claude Desktop / Client                  │
└──────────────────────┬──────────────────────────────────────┘
                       │ MCP Protocol
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                  IB_MCP Server (Upstream)                    │
│  - Account info, positions, orders                          │
│  - Market data queries                                       │
│  - Trade execution                                           │
└──────┬───────────────────────────────────┬──────────────────┘
       │                                   │
       │ REST/Internal APIs                │
       │                                   │
       ▼                                   ▼
┌──────────────────┐            ┌──────────────────────────────┐
│  Price Poller    │◄───────────┤   LangGraph API Server       │
│  Agent           │            │   (Workflow Orchestrator)    │
└──────────────────┘            └──────────────────────────────┘
       │                                   ▲
       │ Triggers & Alerts                 │
       └───────────────────────────────────┘
                 Market Events
```

## Agent Components

### 1. IB_MCP Server (Upstream)

**Source**: https://github.com/rcontesti/IB_MCP
**Purpose**: Core MCP server providing Interactive Brokers integration

**Capabilities**:
- Implements Model Context Protocol for Claude Desktop integration
- Provides tools/functions for:
  - Account information retrieval
  - Position and order management
  - Market data queries (quotes, historical data)
  - Trade execution (buy, sell, modify orders)
- Connects to IB Gateway or TWS (Trader Workstation)

**Our Interaction**:
- We consume IB_MCP as-is, no modifications
- Our agents call IB_MCP's API for trading operations
- LangGraph workflows use IB_MCP tools via MCP protocol

---

### 2. Price Poller Agent

**Location**: `poller/`
**Type**: Autonomous monitoring agent
**Status**: Planned

**Purpose**:
Continuously monitors market prices for configured symbols and triggers events based on predefined conditions.

**Key Responsibilities**:
1. **Real-time Price Monitoring**
   - Poll market data at configurable intervals (e.g., every 1-5 seconds)
   - Track multiple symbols simultaneously
   - Cache recent price history for trend analysis

2. **Trigger Logic**
   - Price threshold alerts (e.g., "alert if AAPL > $200")
   - Percentage change triggers (e.g., "alert on 2% move")
   - Technical indicator signals (moving averages, RSI, etc.)
   - Custom Python-based trigger functions

3. **Event Publishing**
   - Emit events to LangGraph API when triggers fire
   - Include context: symbol, current price, trigger condition, historical context
   - Support multiple event channels (urgent vs. informational)

**Configuration Example**:
```yaml
# poller/config.yaml
polling:
  interval_seconds: 2
  symbols:
    - AAPL
    - TSLA
    - SPY

triggers:
  - name: "AAPL price alert"
    symbol: AAPL
    condition: "price > 200"
    action: "notify_langgraph"

  - name: "TSLA momentum"
    symbol: TSLA
    condition: "percent_change_5min > 1.5"
    action: "execute_workflow:momentum_trade"
```

**Technical Stack**:
- Python 3.11+
- `ib_insync` or direct TWS API for market data
- Redis or in-memory state for price caching
- Event emitter (RabbitMQ, Redis Pub/Sub, or HTTP webhooks)

**Docker Integration**:
```yaml
# docker-compose.override.yml
services:
  poller:
    build: ./ib_mcp_overlay/poller
    environment:
      - IB_GATEWAY_HOST=ib-gateway
      - IB_GATEWAY_PORT=4002
      - LANGGRAPH_URL=http://langgraph-api:8000
      - POLL_INTERVAL=2
    depends_on:
      - ib-gateway
      - langgraph-api
```

---

### 3. LangGraph API Server

**Location**: `langgraph_api/`
**Type**: Workflow orchestration agent
**Status**: Planned

**Purpose**:
Provides AI-powered trading workflows using LangGraph for multi-step decision-making and execution.

**Key Responsibilities**:

1. **Workflow Orchestration**
   - Define and execute multi-step trading workflows
   - Maintain state across workflow steps
   - Handle branching logic based on market conditions
   - Coordinate between poller triggers and IB_MCP execution

2. **AI-Powered Decision Making**
   - Integrate with Claude API for market analysis
   - Natural language interpretation of market conditions
   - Risk assessment and position sizing
   - Multi-factor decision trees (technical + fundamental + sentiment)

3. **Workflow Types**

   **A. Reactive Workflows** (triggered by poller):
   ```
   Trigger → Analyze → Decide → Execute → Monitor → Close
   ```
   - Example: "When AAPL drops 2%, analyze market sentiment, decide if it's a buying opportunity, execute small position, monitor for exit"

   **B. Scheduled Workflows**:
   ```
   Daily Market Open → Scan Watchlist → Rank Opportunities → Present to User
   ```
   - Example: Pre-market analysis and opportunity identification

   **C. Interactive Workflows** (user-initiated via Claude Desktop):
   ```
   User Request → Parse Intent → Gather Context → Execute Strategy → Report
   ```
   - Example: "Buy $1000 of AAPL using VWAP strategy"

4. **Context Management**
   - Maintain trading context across sessions
   - Track open positions and their entry logic
   - Remember risk limits and user preferences
   - Store workflow execution history for learning

**LangGraph Workflow Example**:
```python
# langgraph_api/workflows/momentum_trade.py
from langgraph.graph import StateGraph, END

def analyze_trigger(state):
    """Analyze the market trigger event"""
    # Call Claude to analyze market conditions
    # Query additional data from IB_MCP
    return {"analysis": analysis_result}

def assess_risk(state):
    """Evaluate if trade meets risk criteria"""
    # Check account balance, existing positions
    # Calculate position size
    return {"risk_ok": True, "size": 100}

def execute_trade(state):
    """Execute via IB_MCP"""
    # Call IB_MCP to place order
    return {"order_id": "12345"}

workflow = StateGraph()
workflow.add_node("analyze", analyze_trigger)
workflow.add_node("risk", assess_risk)
workflow.add_node("execute", execute_trade)
workflow.add_edge("analyze", "risk")
workflow.add_conditional_edges("risk",
    lambda s: "execute" if s["risk_ok"] else END)
```

**API Endpoints**:
```
POST /workflows/trigger
  - Accept events from poller
  - Start appropriate workflow

POST /workflows/execute
  - User-initiated workflow execution
  - Called from Claude Desktop via MCP

GET /workflows/{id}/status
  - Check workflow execution status

GET /context
  - Retrieve current trading context for Claude
```

**Technical Stack**:
- Python 3.11+
- LangGraph for workflow orchestration
- FastAPI for REST endpoints
- Claude API (Anthropic SDK)
- PostgreSQL or SQLite for state persistence
- Redis for workflow state caching

**Docker Integration**:
```yaml
# docker-compose.override.yml
services:
  langgraph-api:
    build: ./ib_mcp_overlay/langgraph_api
    ports:
      - "8000:8000"
    environment:
      - ANTHROPIC_API_KEY=${CLAUDE_API_KEY}
      - IB_MCP_URL=http://ib-mcp-server:3000
      - DATABASE_URL=sqlite:///data/workflows.db
    volumes:
      - ./data/langgraph:/data
    depends_on:
      - ib-mcp-server
```

---

## Integration Workflows

### Workflow 1: Automated Price Alert → Analysis → Trade

```
1. Price Poller detects AAPL > $200
   ↓
2. Poller sends event to LangGraph API:
   POST /workflows/trigger
   {
     "trigger": "price_threshold",
     "symbol": "AAPL",
     "price": 201.50,
     "condition": "price > 200"
   }
   ↓
3. LangGraph starts "price_alert" workflow:
   a. Query IB_MCP for account info, existing AAPL position
   b. Call Claude API: "AAPL just crossed $200. Current position: 50 shares.
      Should we add to position or take profit?"
   c. Claude analyzes market conditions, news, technicals
   d. Returns recommendation: "Take partial profit - sell 25 shares"
   ↓
4. LangGraph validates recommendation against risk rules
   ↓
5. LangGraph calls IB_MCP to execute sell order
   ↓
6. LangGraph stores execution record and updates context
   ↓
7. LangGraph notifies user via MCP (shows in Claude Desktop)
```

### Workflow 2: User-Initiated Strategy via Claude Desktop

```
1. User in Claude Desktop: "Set up a covered call strategy on my TSLA position"
   ↓
2. MCP routes to LangGraph API via IB_MCP server
   ↓
3. LangGraph "covered_call" workflow:
   a. Query IB_MCP for TSLA position (must own 100+ shares)
   b. Query IB_MCP for option chain data
   c. Call Claude: "Recommend strike and expiration for covered call.
      Current position: 200 TSLA @ avg $250"
   d. Claude suggests: "Sell 2 contracts of TSLA $270 call, 30 DTE"
   e. Calculate premium and present to user for approval
   ↓
4. User approves in Claude Desktop
   ↓
5. LangGraph executes option order via IB_MCP
   ↓
6. LangGraph sets up monitoring:
   - Track option position
   - Alert if TSLA approaches strike
   - Auto-close before expiration if desired
```

### Workflow 3: Daily Market Scan

```
1. Scheduled trigger (cron) at 9:00 AM ET
   ↓
2. LangGraph "market_scan" workflow:
   a. Query IB_MCP for watchlist symbols
   b. Query IB_MCP for overnight price changes
   c. Retrieve news headlines for movers
   d. Call Claude: "Analyze these 10 stocks for trading opportunities today.
      Consider: gaps, volume, news catalysts, technical setups"
   e. Claude returns ranked list with rationale
   ↓
3. LangGraph presents summary in Claude Desktop:
   "Top 3 opportunities today:
   1. NVDA - bullish gap on earnings, watching for $950 breakout
   2. SPY - range-bound, potential iron condor opportunity
   3. AAPL - oversold bounce candidate at $195 support"
   ↓
4. User can ask follow-up questions or initiate trades via Claude Desktop
```

---

## Data Flow & State Management

### Persistent State
- **Trading Context**: User preferences, risk limits, active strategies
- **Workflow History**: Past decisions, outcomes, P&L attribution
- **Position Memory**: Entry logic, exit criteria for each position
- **Market Events**: Historical triggers and responses

### Ephemeral State
- **Real-time Prices**: Cached in poller, expires quickly
- **Workflow Execution**: LangGraph state during active workflow
- **API Sessions**: Authentication tokens, WebSocket connections

### State Storage
```
ib_mcp_overlay/
└── data/
    ├── poller/
    │   └── price_cache.db          # Recent price history
    ├── langgraph/
    │   ├── workflows.db            # Workflow execution logs
    │   ├── context.db              # Trading context & preferences
    │   └── checkpoints/            # LangGraph state checkpoints
    └── logs/                       # Application logs
```

---

## Security & Risk Considerations

### Risk Controls
1. **Position Limits**
   - Maximum position size per symbol
   - Total portfolio exposure limits
   - Prevent duplicate orders (idempotency)

2. **Rate Limiting**
   - Max orders per minute/hour
   - Cooling-off periods after losses
   - Circuit breaker on rapid market moves

3. **Approval Flows**
   - Critical actions require user approval
   - High-value trades need confirmation
   - New strategy types need whitelisting

### Security Measures
1. **Secrets Management**
   - Store API keys in environment variables or secrets manager
   - Never commit credentials to Git
   - Rotate IB Gateway passwords regularly

2. **Network Security**
   - Internal Docker network for service-to-service communication
   - Expose only necessary ports
   - Use authentication for LangGraph API

3. **Audit Logging**
   - Log all trade executions with timestamps
   - Record Claude API calls and responses
   - Track workflow decisions for analysis

---

## Development Roadmap

### Phase 1: Foundation (Current)
- [x] Repository structure and documentation
- [ ] Basic docker-compose.override.yml setup
- [ ] Local development environment validation

### Phase 2: Price Poller
- [ ] Implement basic polling service
- [ ] Add simple threshold triggers
- [ ] Test with IB_MCP integration
- [ ] Deploy to Raspberry Pi 5

### Phase 3: LangGraph Server
- [ ] Set up FastAPI server
- [ ] Implement first workflow (price alert handler)
- [ ] Integrate Claude API
- [ ] Test end-to-end: Trigger → LangGraph → IB_MCP

### Phase 4: Advanced Workflows
- [ ] User-initiated strategy workflows
- [ ] Scheduled analysis workflows
- [ ] Position monitoring and auto-exit logic
- [ ] Multi-step strategy orchestration

### Phase 5: Intelligence & Learning
- [ ] Workflow outcome tracking
- [ ] Performance attribution by strategy
- [ ] Claude-powered strategy refinement
- [ ] Backtesting framework integration

---

## Monitoring & Observability

### Key Metrics
- Poller: Poll latency, trigger frequency, missed polls
- LangGraph: Workflow success rate, execution time, Claude API costs
- Trading: Order fill rate, slippage, P&L by workflow

### Logging Strategy
```python
# Structured logging format
{
  "timestamp": "2025-01-01T10:30:00Z",
  "service": "langgraph-api",
  "workflow": "momentum_trade",
  "event": "trade_executed",
  "symbol": "AAPL",
  "action": "BUY",
  "quantity": 100,
  "price": 201.50,
  "order_id": "12345",
  "claude_reasoning": "Bullish breakout above resistance..."
}
```

### Health Checks
- IB Gateway connectivity
- MCP server availability
- LangGraph API responsiveness
- Claude API quota remaining

---

## Raspberry Pi 5 Deployment Notes

### Resource Considerations
- **CPU**: ARM64 architecture, sufficient for Python agents
- **Memory**: 8GB recommended for multiple services + Claude API caching
- **Storage**: Use SSD for Docker volumes (SQLite databases, logs)
- **Network**: Stable connection critical for real-time trading

### Optimization Tips
1. Use Alpine-based Docker images where possible
2. Set resource limits in docker-compose.override.yml
3. Implement log rotation to prevent disk fill
4. Use Redis for caching instead of in-memory (survives restarts)
5. Run heavy computations (backtesting) on AWS, not Pi

### Example Resource Limits
```yaml
services:
  langgraph-api:
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 2G
        reservations:
          memory: 1G
```

---

## Testing Strategy

### Unit Tests
- Trigger logic in poller
- Workflow step functions in LangGraph
- Risk calculation functions
- Mock IB_MCP and Claude API responses

### Integration Tests
- End-to-end workflow execution
- Poller → LangGraph → IB_MCP pipeline
- Error handling and retry logic
- State persistence and recovery

### Simulation Testing
- Paper trading mode via IB Gateway
- Replay historical price data through poller
- Test workflow decisions without real money
- Validate P&L tracking accuracy

---

## Contributing

This is a personal overlay project, but contributions welcome:
1. Fork this overlay repo (not IB_MCP upstream)
2. Create feature branch
3. Add tests for new workflows or triggers
4. Submit PR with clear description

---

## References

- **IB_MCP**: https://github.com/rcontesti/IB_MCP
- **LangGraph**: https://github.com/langchain-ai/langgraph
- **Interactive Brokers API**: https://interactivebrokers.github.io/
- **Model Context Protocol**: https://modelcontextprotocol.io/
- **Claude API**: https://docs.anthropic.com/

---

## License

This overlay follows the same license as IB_MCP upstream. See https://github.com/rcontesti/IB_MCP for details.
