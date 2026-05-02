import pytest
import asyncio
import json
from unittest.mock import AsyncMock, Mock
import ib_async as ib
from option_chain_mcp.server import OptionChainMCP

@pytest.fixture
def mcp_server():
    server = OptionChainMCP("127.0.0.1", 7496, 123)
    server.connected = True
    server.ib = Mock(spec=ib.IB)
    server.ib.isConnected.return_value = True
    return server

def extract_json(response):
    if isinstance(response, list):
        return json.loads(response[0].text)
    elif hasattr(response, 'text'):
        return json.loads(response.text)
    elif hasattr(response, 'content') and isinstance(response.content, list):
        return json.loads(response.content[0].text)
    elif isinstance(response, str):
        return json.loads(response)
    # fastmcp might just return the dict natively in some call_tool variants?
    if isinstance(response, list) and hasattr(response[0], 'text'):
        return json.loads(response[0].text)
    return response

@pytest.mark.asyncio
async def test_preview_cash_secured_put_order_valid(mcp_server):
    mock_contract = ib.Option(symbol="AAPL", strike=150, right="P")
    mcp_server.ib.qualifyContractsAsync = AsyncMock(return_value=[mock_contract])
    
    response = await mcp_server.mcp.call_tool("preview_cash_secured_put_order", {
        "ticker": "AAPL",
        "expiry": "2026-06-12",
        "strike": 150.0,
        "quantity": 2,
        "limit_price": 5.0,
        "confirm_stage_only": True
    })
    
    result = extract_json(response)
    
    assert result["status"] == "preview_successful"
    assert result["capital_required"] == 30000.0
    assert result["premium"] == 1000.0
    assert result["effective_entry"] == 145.0
    assert result["transmit"] is False

@pytest.mark.asyncio
async def test_create_draft_order_valid(mcp_server):
    mock_contract = ib.Option(symbol="AAPL", strike=150, right="P")
    mcp_server.ib.qualifyContractsAsync = AsyncMock(return_value=[mock_contract])
    
    mock_trade = Mock()
    mock_trade.order.orderId = 12345
    mcp_server.ib.placeOrder = Mock(return_value=mock_trade)
    
    response = await mcp_server.mcp.call_tool("create_draft_cash_secured_put_order", {
        "ticker": "AAPL",
        "expiry": "2026-06-12",
        "strike": 150.0,
        "quantity": 2,
        "limit_price": 5.0,
        "confirm_stage_only": True
    })
    
    result = extract_json(response)
    assert result["status"] == "draft_created"
    assert result["ib_order_id"] == "12345"
    assert result["transmit"] is False
    
    mcp_server.ib.placeOrder.assert_called_once()
    args, kwargs = mcp_server.ib.placeOrder.call_args
    contract, order = args
    assert contract == mock_contract
    assert order.action == "SELL"
    assert order.totalQuantity == 2
    assert order.lmtPrice == 5.0
    assert order.transmit is False

@pytest.mark.asyncio
async def test_reject_missing_confirm(mcp_server):
    with pytest.raises(Exception, match="confirm_stage_only must be true"):
        await mcp_server.mcp.call_tool("create_draft_cash_secured_put_order", {
            "ticker": "AAPL",
            "expiry": "2026-06-12",
            "strike": 150.0,
            "quantity": 2,
            "limit_price": 5.0,
            "confirm_stage_only": False
        })

@pytest.mark.asyncio
async def test_reject_invalid_quantity(mcp_server):
    with pytest.raises(Exception, match="Quantity must be greater than 0"):
        await mcp_server.mcp.call_tool("create_draft_cash_secured_put_order", {
            "ticker": "AAPL",
            "expiry": "2026-06-12",
            "strike": 150.0,
            "quantity": 0,
            "limit_price": 5.0,
            "confirm_stage_only": True
        })

@pytest.mark.asyncio
async def test_reject_invalid_limit_price(mcp_server):
    with pytest.raises(Exception, match="Limit price must be greater than 0"):
        await mcp_server.mcp.call_tool("create_draft_cash_secured_put_order", {
            "ticker": "AAPL",
            "expiry": "2026-06-12",
            "strike": 150.0,
            "quantity": 2,
            "limit_price": -1.0,
            "confirm_stage_only": True
        })
