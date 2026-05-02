# IB MCP Overlay

This overlay contains local additions for running Interactive Brokers MCP services without forking upstream MCP projects.

## Current Status

- Docker image builds and runs locally.
- MCP endpoint: `http://localhost:8010/mcp`
- Tested against TWS live API port `7496`.
- Confirmed read-only connection to account `U12024249`.
- Confirmed portfolio snapshot returns positions and cash balances.
- Confirmed option-chain metadata and cash-secured put scanner return structured JSON.
- Option bid/ask/delta/IV fields depend on IBKR market-data permissions and market/session availability.

## Option Chain MCP

`option_chain_mcp` is a read-only MCP server that connects to IB Gateway or TWS through the TWS API using `ib_async`. It exposes tools for AI clients to query option-chain metadata and option price snapshots for stock tickers.

### Tools

- `get_option_chain_summary`: returns available expirations, strike count, strike range, chain exchange, trading class, and multiplier.
- `get_option_chain_prices`: returns capped option quote snapshots with bid, ask, last, close, mark, midpoint, implied volatility, and model greeks when IBKR provides them.
- `find_cash_secured_put_opportunities`: returns PUT contracts near a target expiry that are 10-20% OTM by default, with quote fields and cash-secured put calculations grouped by ticker.
- `get_portfolio_snapshot`: returns open positions, cash balances, and key account values grouped by account.
- `preview_cash_secured_put_order`: calculates capital required, premium, and effective entry for a staging check without interacting with TWS.
- `create_draft_cash_secured_put_order`: creates an untransmitted (staged) SELL PUT limit order directly in TWS for manual review and approval.

The server does not expose auto-trading tools. All orders are strictly staged as drafts.

## Prerequisites

- Docker and Docker Compose.
- Interactive Brokers TWS or IB Gateway installed.
- TWS/Gateway must be open, logged in, and not session-expired.
- IBKR API socket access enabled in TWS/Gateway.
- Market-data permissions for the symbols/options you want live quotes for.

### IBKR Setup

In TWS or IB Gateway:

1. Enable `ActiveX and Socket Clients`.
2. Add `127.0.0.1` as a trusted IP if connecting locally.
3. Use the correct API port:
   - TWS paper: `7497`
   - TWS live: `7496`
   - IB Gateway paper: often `4002`
   - IB Gateway live: often `4001`

You must have the relevant options and market-data permissions in your IBKR account. If live prices are unavailable, call `get_option_chain_prices` with `market_data_type=3` for delayed data.

If TWS reports `session expired`, log back in. The MCP does not authenticate to IBKR by itself; it connects to the already-authenticated local TWS/Gateway API socket.

### Run With Docker Compose

Copy the example environment file and edit ports if needed:

```bash
cp .env.example .env
```

Your current local TWS setup was verified on live TWS port `7496`, so set:

```env
IB_PORT=7496
```

Start the MCP server:

```bash
docker compose -f docker-compose.override.yml up -d --build
```

If using the local Colima setup created during development, this also works:

```bash
IB_PORT=7496 /opt/homebrew/bin/docker compose -f docker-compose.override.yml up -d --build
```

The HTTP MCP endpoint is:

```text
http://localhost:8010/mcp
```

If TWS or IB Gateway is running on the same macOS/Windows host as Docker, keep:

```env
IB_HOST=host.docker.internal
```

If running the MCP server directly on the same machine without Docker, use:

```env
IB_HOST=127.0.0.1
```

### Auto Start At Login

This repo includes a launchd plist and startup script that start Colima and the MCP container when you log in:

- `scripts/start_mcp_stack.sh`
- `launchd/com.matthewthomasson.ib_mcp_overlay.plist`

Install them with:

```bash
mkdir -p ~/Library/LaunchAgents ~/Library/Logs/ib_mcp_overlay
cp launchd/com.matthewthomasson.ib_mcp_overlay.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.matthewthomasson.ib_mcp_overlay.plist
launchctl enable gui/$(id -u)/com.matthewthomasson.ib_mcp_overlay
launchctl kickstart -k gui/$(id -u)/com.matthewthomasson.ib_mcp_overlay
```

The startup script:

- starts Colima if it is not already running,
- waits for the Docker daemon,
- runs `docker compose -f docker-compose.override.yml up -d --build`,
- uses `IB_PORT=7496` by default.

### Health Checks

Check that TWS is listening locally:

```bash
nc -vz 127.0.0.1 7496
```

Check the container:

```bash
docker ps --filter name=option_chain_mcp
docker logs --tail 80 option_chain_mcp
```

Call the MCP tool `check_ibkr_connection`. A healthy response looks like:

```json
{
  "connected": true,
  "host": "host.docker.internal",
  "port": 7496,
  "readonly": true,
  "managed_accounts": ["U12024249"]
}
```

## Tool Examples

### Portfolio Snapshot

Tool:

```json
{
  "name": "get_portfolio_snapshot",
  "arguments": {
    "include_zero_positions": false
  }
}
```

Returns positions, cash balances, net liquidation, buying power, available funds, excess liquidity, gross position value, and raw account values grouped by account.

### Draft Cash-Secured Put Order

Tool:

```json
{
  "name": "create_draft_cash_secured_put_order",
  "arguments": {
    "ticker": "LRCX",
    "expiry": "2026-06-12",
    "strike": 230,
    "quantity": 1,
    "limit_price": 9.05,
    "confirm_stage_only": true
  }
}
```

Returns draft order status with the generated `ib_order_id`, and confirmation that `transmit: false` was used so the order requires manual approval in TWS.

### Cash-Secured Put Scanner

Tool:

```json
{
  "name": "find_cash_secured_put_opportunities",
  "arguments": {
    "tickers": ["AAPL", "MSFT", "TSLA"],
    "target_expiry": "2026-06-12",
    "min_otm_pct": 0.10,
    "max_otm_pct": 0.20,
    "max_strikes_per_ticker": 12,
    "market_data_type": 3,
    "quote_wait_seconds": 4
  }
}
```

Returns JSON grouped by ticker. Each contract includes strike, bid, ask, mid, delta, open interest, volume, implied volatility, capital required, premium at ask, and effective entry price when IBKR provides those fields.

### Option Chain Summary

Tool:

```json
{
  "name": "get_option_chain_summary",
  "arguments": {
    "symbol": "AAPL",
    "max_expirations": 12
  }
}
```

## Market Data Notes

IBKR may return `null` for bid, ask, delta, or implied volatility when:

- the market is closed,
- delayed option data is not available for that contract,
- the account lacks the required options market-data subscription,
- TWS/Gateway has not fully connected to the relevant market-data farm.

The scanner normalizes IBKR's unavailable quote values, such as `-1`, to `null`.

### Example AI Prompts

- `Show me the nearest AAPL option chain around spot with 10 strikes.`
- `Get delayed call and put quotes for MSFT expiry 20260619.`
- `List the available TSLA option expirations and strike range.`
- `For AAPL, MSFT, and TSLA, identify cash-secured put selling opportunities closest to 2026-06-12, approximately 10-20% OTM, and return bid, ask, mid, delta, open interest, volume, implied volatility, capital required, premium at ask, and effective entry price as JSON grouped by ticker.`
- `Return my current portfolio snapshot with all open positions and cash balances as structured JSON.`

### MCP Client Config

For MCP clients that support HTTP/streamable HTTP, point the client at:

```json
{
  "servers": {
    "ib-option-chain": {
      "type": "http",
      "url": "http://localhost:8010/mcp"
    }
  }
}
```

## Troubleshooting

- `Connection refused` on `7496` or `7497`: TWS/Gateway is not listening, API socket access is disabled, or the wrong port is configured.
- `connected: false` from `check_ibkr_connection`: TWS/Gateway is closed, logged out, session-expired, or blocking API clients.
- Empty option prices with valid contracts: usually market-data permissions, delayed data availability, or market-hours behavior.
- Docker cannot reach TWS: use `IB_HOST=host.docker.internal` when TWS runs on the Docker host.
- Running on paper TWS: set `IB_PORT=7497`.
- Running on live TWS: set `IB_PORT=7496`.
