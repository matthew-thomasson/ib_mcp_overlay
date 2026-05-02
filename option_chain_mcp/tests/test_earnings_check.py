import json
import pytest
from unittest.mock import AsyncMock, Mock, patch
import ib_async as ib
from option_chain_mcp.server import OptionChainMCP


@pytest.fixture
def mcp_server():
    server = OptionChainMCP("127.0.0.1", 7496, 123)
    server.connected = True
    server._wsh_metadata_fetched = True  # skip metadata fetch in unit tests
    server.ib = Mock(spec=ib.IB)
    server.ib.isConnected.return_value = True
    return server


def extract_json(response):
    if isinstance(response, list):
        return json.loads(response[0].text)
    elif hasattr(response, "text"):
        return json.loads(response.text)
    elif hasattr(response, "content") and isinstance(response.content, list):
        return json.loads(response.content[0].text)
    elif isinstance(response, str):
        return json.loads(response)
    return response


def _make_stock(con_id: int) -> ib.Stock:
    stock = ib.Stock("AAPL", "SMART", "USD")
    stock.conId = con_id
    return stock


@pytest.mark.asyncio
async def test_earnings_pass_no_events(mcp_server):
    """PASS: WSH returns empty data — no earnings in window."""
    mock_stock = _make_stock(8314)
    mcp_server.ib.qualifyContractsAsync = AsyncMock(return_value=[mock_stock])
    mcp_server.ib.reqWshEventData = Mock(return_value=json.dumps({"data": []}))

    response = await mcp_server.mcp.call_tool("get_earnings_check", {
        "tickers": ["AAPL"],
        "window_start": "2026-05-02",
        "window_end": "2026-06-18",
    })
    result = extract_json(response)

    assert result["results"]["AAPL"]["verdict"] == "PASS"
    assert result["results"]["AAPL"]["earnings_date"] is None


@pytest.mark.asyncio
async def test_earnings_warn_event_in_window(mcp_server):
    """WARN: WSH returns an earnings event inside the window."""
    mock_stock = _make_stock(8314)
    mcp_server.ib.qualifyContractsAsync = AsyncMock(return_value=[mock_stock])
    wsh_payload = json.dumps({"data": [{"date": "2026-06-05", "event_type": "Earnings"}]})
    mcp_server.ib.reqWshEventData = Mock(return_value=wsh_payload)

    response = await mcp_server.mcp.call_tool("get_earnings_check", {
        "tickers": ["AAPL"],
        "window_start": "2026-05-02",
        "window_end": "2026-06-18",
    })
    result = extract_json(response)

    assert result["results"]["AAPL"]["verdict"] == "WARN"
    assert result["results"]["AAPL"]["earnings_date"] == "2026-06-05"
    assert "⚠️" in result["results"]["AAPL"]["message"]


@pytest.mark.asyncio
async def test_earnings_unavailable_on_error(mcp_server):
    """UNAVAILABLE: qualify fails — WSH subscription or contract error."""
    mcp_server.ib.qualifyContractsAsync = AsyncMock(
        side_effect=Exception("WSH subscription not active")
    )

    response = await mcp_server.mcp.call_tool("get_earnings_check", {
        "tickers": ["AAPL"],
        "window_start": "2026-05-02",
        "window_end": "2026-06-18",
    })
    result = extract_json(response)

    assert result["results"]["AAPL"]["verdict"] == "UNAVAILABLE"
    assert "WSH subscription" in result["results"]["AAPL"]["message"]


@pytest.mark.asyncio
async def test_earnings_multi_ticker_mixed(mcp_server):
    """Multi-ticker: one PASS, one WARN."""
    def qualify_side_effect(*contracts):
        symbol = contracts[0].symbol if contracts else ""
        stock = _make_stock(8314 if symbol == "AAPL" else 272093)
        return [stock]

    mcp_server.ib.qualifyContractsAsync = AsyncMock(side_effect=qualify_side_effect)

    def wsh_side_effect(wsh_data):
        # MSFT has earnings; AAPL does not
        if "272093" in wsh_data.filter:
            return json.dumps({"data": [{"date": "2026-05-28", "event_type": "Earnings"}]})
        return json.dumps({"data": []})

    mcp_server.ib.reqWshEventData = Mock(side_effect=wsh_side_effect)

    response = await mcp_server.mcp.call_tool("get_earnings_check", {
        "tickers": ["AAPL", "MSFT"],
        "window_start": "2026-05-02",
        "window_end": "2026-06-18",
    })
    result = extract_json(response)

    assert result["results"]["AAPL"]["verdict"] == "PASS"
    assert result["results"]["MSFT"]["verdict"] == "WARN"
    assert result["results"]["MSFT"]["earnings_date"] == "2026-05-28"


@pytest.mark.asyncio
async def test_earnings_window_dates_in_output(mcp_server):
    """Returned payload always echoes back the requested window."""
    mock_stock = _make_stock(8314)
    mcp_server.ib.qualifyContractsAsync = AsyncMock(return_value=[mock_stock])
    mcp_server.ib.reqWshEventData = Mock(return_value=json.dumps({"data": []}))

    response = await mcp_server.mcp.call_tool("get_earnings_check", {
        "tickers": ["AAPL"],
        "window_start": "2026-05-02",
        "window_end": "2026-06-18",
    })
    result = extract_json(response)

    assert result["window_start"] == "2026-05-02"
    assert result["window_end"] == "2026-06-18"
