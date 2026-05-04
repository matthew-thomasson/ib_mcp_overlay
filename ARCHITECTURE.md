# IB MCP Overlay Architecture

## Purpose

This repository provides a read-only MCP layer for Interactive Brokers so an MCP-aware LLM can query:

- option chain metadata
- option prices
- cash-secured put candidates
- portfolio positions and cash balances
- execution/trade history for requested date/time windows through IBKR Flex Web Service
- IBKR connection status

## High-Level Flow

```text
LLM / MCP client
  -> HTTP MCP endpoint on localhost:8010/mcp
  -> Docker container: option_chain_mcp
  -> ib_async client for TWS API tools
  -> TWS API on localhost:7496
  -> Interactive Brokers

Historical trade flow:

```text
LLM / MCP client
  -> HTTP MCP endpoint on localhost:8010/mcp
  -> Docker container: option_chain_mcp
  -> IBKR Flex Web Service over HTTPS
  -> Interactive Brokers Account Management reports
```

## Key Components

- `option_chain_mcp/`: the MCP server implementation.
- `docker-compose.override.yml`: starts the MCP container.
- `scripts/start_mcp_stack.sh`: starts Colima and then the Docker stack at login.
- `launchd/com.matthewthomasson.ib_mcp_overlay.plist`: macOS LaunchAgent that runs the startup script.
- `README.md`: setup, tool examples, and troubleshooting.
- `GEMINI.md` and `.gemini/settings.json`: Gemini-facing instructions and MCP endpoint config.
- `system.md`: operating instructions for the options analyst workflow.

## Runtime Dependencies

### Required

- macOS user session
- Docker CLI
- Colima
- Interactive Brokers TWS or IB Gateway
- A logged-in TWS/Gateway session
- IBKR API socket access enabled

### Expected Local Ports

- MCP HTTP endpoint: `8010`
- TWS live API: `7496`
- TWS paper API: `7497`
- IB Gateway live API: `4001`
- IB Gateway paper API: `4002`

## Startup Order

1. User logs in to macOS.
2. LaunchAgent starts `scripts/start_mcp_stack.sh`.
3. Script starts Colima if needed.
4. Script waits for the Docker daemon.
5. Script runs `docker compose -f docker-compose.override.yml up -d --build`.
6. Docker starts `option_chain_mcp`.
7. MCP connects to TWS at `host.docker.internal:7496`.

## MCP Tools

- `check_ibkr_connection`
- `get_portfolio_snapshot`
- `get_flex_trade_history`
- `get_trade_history`
- `get_option_chain_summary`
- `get_option_chain_prices`
- `find_cash_secured_put_opportunities`

## Data Notes

- The MCP is read-only.
- It does not place orders.
- Durable historical trade history is sourced from IBKR Flex Web Service. The Flex Query template controls the report period and fields returned.
- TWS execution reports are still available through `get_trade_history`, but TWS/Gateway controls whether any execution history is exposed for the API session.
- Option quote fields such as bid, ask, delta, and implied volatility may be `null` if IBKR has no live or delayed market data for the contract.
- The scanner normalizes IBKR placeholder values such as `-1` to `null`.

## Rebuild Path

If the repo changes:

```bash
IB_PORT=7496 /opt/homebrew/bin/docker compose -f docker-compose.override.yml up -d --build
```

## Handoff Summary

This is the layer to point an MCP-aware LLM at when you want it to query IBKR without writing bespoke client code.
