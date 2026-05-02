import asyncio
import json
from option_chain_mcp.server import OptionChainMCP

async def main():
    # Connect directly to the underlying MCP logic
    server = OptionChainMCP('127.0.0.1', 7496, 999) # Using 999 as clientId to avoid conflict
    
    try:
        response = await server.mcp.call_tool("create_draft_cash_secured_put_order", {
            "ticker": "LRCX",
            "expiry": "2026-06-12",
            "strike": 230.0,
            "quantity": 2,
            "limit_price": 9.05,
            "confirm_stage_only": True
        })
        
        # In FastMCP 3.x, response is usually a list of content blocks
        if isinstance(response, list) and len(response) > 0:
            if hasattr(response[0], 'text'):
                print(response[0].text)
            else:
                print(response)
        else:
            print(response)
    except Exception as e:
        print(f"Error calling tool: {e}")
    finally:
        if server.connected:
            server.ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
