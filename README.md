# IB MCP Overlay

This overlay contains local additions for running Interactive Brokers MCP services without forking upstream MCP projects.

## Option Chain MCP

`option_chain_mcp` is a read-only MCP server that connects to IB Gateway or TWS through the TWS API using `ib_async`. It exposes tools for AI clients to query option-chain metadata and option price snapshots for stock tickers.

### Tools

- `get_option_chain_summary`: returns available expirations, strike count, strike range, chain exchange, trading class, and multiplier.
- `get_option_chain_prices`: returns capped option quote snapshots with bid, ask, last, close, mark, midpoint, implied volatility, and model greeks when IBKR provides them.

The server does not expose order-placement tools.

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

The HTTP MCP endpoint is:

```text
http://localhost:8010/mcp/
```

If TWS or IB Gateway is running on the same macOS/Windows host as Docker, keep:

```env
IB_HOST=host.docker.internal
```

If running the MCP server directly on the same machine without Docker, use:

```env
IB_HOST=127.0.0.1
```

### Example AI Prompts

- `Show me the nearest AAPL option chain around spot with 10 strikes.`
- `Get delayed call and put quotes for MSFT expiry 20260619.`
- `List the available TSLA option expirations and strike range.`

### MCP Client Config

For MCP clients that support HTTP/streamable HTTP, point the client at:

```json
{
  "servers": {
    "ib-option-chain": {
      "type": "http",
      "url": "http://localhost:8010/mcp/"
    }
  }
}
```
