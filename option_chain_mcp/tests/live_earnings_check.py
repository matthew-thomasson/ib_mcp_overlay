"""
Live integration test for get_earnings_check.
Connects directly to IBKR TWS/Gateway and calls the real WSH API.
Run with: python tests/live_earnings_check.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from option_chain_mcp.server import OptionChainMCP

TICKERS = ["TER", "GLW", "ATI", "LRCX", "QS"]
WINDOW_START = "2026-05-02"
WINDOW_END = "2026-06-12"

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "22"))  # use a different ID to avoid conflicts


async def main() -> None:
    server = OptionChainMCP(IB_HOST, IB_PORT, IB_CLIENT_ID)
    print(f"Connecting to IBKR at {IB_HOST}:{IB_PORT} (client_id={IB_CLIENT_ID})...")

    try:
        await server._ensure_connected()
        print("✅ Connected.\n")

        print(f"Running earnings check for {TICKERS}")
        print(f"Window: {WINDOW_START} → {WINDOW_END}\n")
        print("-" * 60)

        result = await server.mcp.call_tool("get_earnings_check", {
            "tickers": TICKERS,
            "window_start": WINDOW_START,
            "window_end": WINDOW_END,
        })

        # Parse result
        if isinstance(result, list) and hasattr(result[0], "text"):
            data = json.loads(result[0].text)
        elif isinstance(result, str):
            data = json.loads(result)
        else:
            data = result

        results = data.get("results", {})
        for ticker, info in results.items():
            verdict = info.get("verdict", "?")
            message = info.get("message", "")
            icon = {"PASS": "[PASS]", "WARN": "[WARN]", "UNAVAILABLE": "[N/A] "}.get(verdict, "[?]")
            print(f"{icon}  {ticker:6s}  {message}")

        print("-" * 60)
        warns = [t for t, i in results.items() if i.get("verdict") == "WARN"]
        passes = [t for t, i in results.items() if i.get("verdict") == "PASS"]
        unavail = [t for t, i in results.items() if i.get("verdict") == "UNAVAILABLE"]

        print(f"\nSummary: {len(passes)} PASS | {len(warns)} WARN | {len(unavail)} UNAVAILABLE")
        if warns:
            print(f"[WARN] Do NOT stage orders for: {', '.join(warns)} without user confirmation.")
        if unavail:
            print(f"[N/A]  WSH data unavailable for: {', '.join(unavail)} -- check subscription.")

    except Exception as e:
        print(f"[ERROR] {e}")
    finally:
        if server.connected:
            server.ib.disconnect()
            print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(main())
