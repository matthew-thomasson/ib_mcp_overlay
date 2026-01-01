# LangGraph API Server

This directory will contain the LangGraph server implementation that provides AI-powered trading workflows and decision-making capabilities.

## Planned Features

- LangGraph-based workflow orchestration
- Integration with Claude AI for market analysis
- Multi-step trading decision pipelines
- Context-aware trade execution strategies

## Structure

```
langgraph_api/
├── Dockerfile          # Container definition for LangGraph server
├── requirements.txt    # Python dependencies including langgraph
├── server.py          # LangGraph server implementation
├── workflows/         # Trading workflow definitions
└── config.yaml        # Server configuration
```

## Usage

The LangGraph API will be exposed as a service in the Docker Compose stack and will coordinate with both the IB_MCP server and the price poller to enable intelligent, automated trading workflows.
