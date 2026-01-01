# Price Polling and Trigger Service

This directory will contain the price polling service that monitors market data and triggers trading actions.

## Planned Features

- Real-time price polling for configured symbols
- Trigger logic for automated trading decisions
- Integration with IB_MCP server for trade execution
- Configurable polling intervals and alert thresholds

## Structure

```
poller/
├── Dockerfile          # Container definition for the poller service
├── requirements.txt    # Python dependencies
├── poller.py          # Main polling logic
└── config.yaml        # Configuration for symbols, intervals, triggers
```

## Usage

The poller service will be integrated into the Docker Compose stack via the override file and will communicate with the IB_MCP server to execute trades based on market conditions.
