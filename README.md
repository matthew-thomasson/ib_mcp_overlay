# IB_MCP Overlay

This repository provides a customization layer for the [IB_MCP](https://github.com/rcontesti/IB_MCP) project without forking it. It uses Docker Compose's override functionality to extend the base IB_MCP setup with additional services and configurations.

## Architecture

This overlay adds:
- **Price Poller**: Real-time market data monitoring and trigger logic
- **LangGraph API**: AI-powered trading workflow orchestration
- **Custom Configurations**: Environment-specific overrides via `docker-compose.override.yml`

## Prerequisites

- Docker and Docker Compose installed on your system
- Git
- Access to Interactive Brokers (TWS or IB Gateway)
- (Optional) Claude AI API key for LangGraph functionality

## Setup Instructions

### 1. Clone the Upstream IB_MCP Repository

```bash
git clone https://github.com/rcontesti/IB_MCP.git
```

### 2. Clone This Overlay Repository

```bash
git clone https://github.com/matthew-thomasson/ib_mcp_overlay.git
```

Your directory structure should look like:
```
parent-folder/
├── IB_MCP/                    # Upstream repository
└── ib_mcp_overlay/            # This overlay repository
```

### 3. Start the Stack

Use Docker Compose to combine both configuration files:

```bash
docker compose -f IB_MCP/docker-compose.yml -f ib_mcp_overlay/docker-compose.override.yml up -d --build
```

This command:
- Uses the base configuration from `IB_MCP/docker-compose.yml`
- Applies customizations from `ib_mcp_overlay/docker-compose.override.yml`
- Builds images and starts containers in detached mode

### 4. View Logs

To monitor the running services:

```bash
# View logs from all services
docker compose -f IB_MCP/docker-compose.yml -f ib_mcp_overlay/docker-compose.override.yml logs -f

# View logs from a specific service (e.g., ib-mcp-server)
docker compose -f IB_MCP/docker-compose.yml -f ib_mcp_overlay/docker-compose.override.yml logs -f ib-mcp-server
```

### 5. Stop the Stack

To stop all running services:

```bash
docker compose -f IB_MCP/docker-compose.yml -f ib_mcp_overlay/docker-compose.override.yml down
```

To stop and remove volumes (⚠️ this will delete data):

```bash
docker compose -f IB_MCP/docker-compose.yml -f ib_mcp_overlay/docker-compose.override.yml down -v
```

## Customization

### Adding Services

Edit `docker-compose.override.yml` to add new services or override existing ones. For example:

```yaml
services:
  poller:
    build: ./ib_mcp_overlay/poller
    environment:
      - IB_MCP_URL=http://ib-mcp-server:3000
    depends_on:
      - ib-mcp-server
```

### Environment Variables

Create a `.env` file in the same directory where you run `docker compose` to set environment variables:

```env
IB_GATEWAY_PORT=4002
CLAUDE_API_KEY=your_api_key_here
```

## Raspberry Pi 5 Notes

This stack is designed to run on Raspberry Pi 5 (ARM64 architecture):
- Ensure you're using ARM-compatible base images in any custom Dockerfiles
- Monitor resource usage with `docker stats` as trading applications can be resource-intensive
- Consider using external storage for Docker volumes if using heavy logging

## Development

To modify the overlay:
1. Make changes to the files in `ib_mcp_overlay/`
2. Rebuild and restart: `docker compose -f IB_MCP/docker-compose.yml -f ib_mcp_overlay/docker-compose.override.yml up -d --build`

## Updating Upstream

To pull the latest changes from IB_MCP:

```bash
cd IB_MCP
git pull origin main
cd ..
docker compose -f IB_MCP/docker-compose.yml -f ib_mcp_overlay/docker-compose.override.yml up -d --build
```

## License

This overlay follows the same license as the upstream IB_MCP project. Please refer to the [IB_MCP repository](https://github.com/rcontesti/IB_MCP) for license details.
