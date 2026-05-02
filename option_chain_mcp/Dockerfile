FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml ./
COPY option_chain_mcp ./option_chain_mcp

RUN uv sync --no-dev

ENV IB_HOST=host.docker.internal
ENV IB_PORT=7497
ENV IB_CLIENT_ID=21
ENV IB_MCP_TRANSPORT=http
ENV IB_MCP_HTTP_HOST=0.0.0.0
ENV IB_MCP_HTTP_PORT=8010

EXPOSE 8010

CMD ["uv", "run", "option-chain-mcp"]
