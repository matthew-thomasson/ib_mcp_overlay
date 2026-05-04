import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import ib_async as ib
import pytest

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
    elif hasattr(response, "text"):
        return json.loads(response.text)
    elif hasattr(response, "content") and isinstance(response.content, list):
        return json.loads(response.content[0].text)
    elif isinstance(response, str):
        return json.loads(response)
    return response


def _fill(exec_id: str, when: str, symbol: str = "AAPL"):
    contract = SimpleNamespace(
        symbol=symbol,
        secType="STK",
        conId=123,
        exchange="SMART",
        primaryExchange="NASDAQ",
        currency="USD",
        localSymbol=symbol,
        tradingClass=symbol,
        lastTradeDateOrContractMonth="",
        strike=0.0,
        right="",
        multiplier="",
    )
    execution = SimpleNamespace(
        time=when,
        acctNumber="U123",
        execId=exec_id,
        orderId=456,
        permId=789,
        clientId=123,
        side="BOT",
        shares=10,
        price=150.25,
        avgPrice=150.25,
        cumQty=10,
        lastLiquidity=1,
        exchange="NASDAQ",
        orderRef="",
    )
    commission_report = SimpleNamespace(
        execId=exec_id,
        commission=1.0,
        currency="USD",
        realizedPNL=0.0,
        yield_=0.0,
        yieldRedemptionDate=0,
    )
    return SimpleNamespace(
        contract=contract,
        execution=execution,
        commissionReport=commission_report,
    )


@pytest.mark.asyncio
async def test_trade_history_returns_executions_in_window(mcp_server):
    mcp_server.ib.reqExecutionsAsync = AsyncMock(
        return_value=[
            _fill("outside", "20260501 09:30:00"),
            _fill("inside", "20260502 10:15:00"),
        ]
    )

    response = await mcp_server.mcp.call_tool("get_trade_history", {
        "start_date": "2026-05-02",
        "end_date": "2026-05-02",
        "account": "U123",
        "symbol": "aapl",
    })
    result = extract_json(response)

    assert result["readonly"] is True
    assert result["returned_executions"] == 1
    assert result["executions"][0]["exec_id"] == "inside"
    assert result["executions"][0]["contract"]["symbol"] == "AAPL"
    assert result["executions"][0]["commission_report"]["commission"] == 1.0

    execution_filter = mcp_server.ib.reqExecutionsAsync.call_args.args[0]
    assert execution_filter.acctCode == "U123"
    assert execution_filter.symbol == "AAPL"
    assert execution_filter.time == "20260502 00:00:00"


@pytest.mark.asyncio
async def test_trade_history_rejects_inverted_window(mcp_server):
    with pytest.raises(Exception, match="start_date must be earlier"):
        await mcp_server.mcp.call_tool("get_trade_history", {
            "start_date": "2026-05-03",
            "end_date": "2026-05-02",
        })


@pytest.mark.asyncio
async def test_trade_history_truncates_results(mcp_server):
    mcp_server.ib.reqExecutionsAsync = AsyncMock(
        return_value=[
            _fill("one", "20260502 10:15:00"),
            _fill("two", "20260502 11:15:00"),
        ]
    )

    response = await mcp_server.mcp.call_tool("get_trade_history", {
        "start_date": "2026-05-02 00:00",
        "end_date": "2026-05-02 23:59",
        "max_results": 1,
    })
    result = extract_json(response)

    assert result["available_executions_after_filter"] == 2
    assert result["returned_executions"] == 1
    assert result["truncated"] is True
    assert len(result["executions"]) == 1


FLEX_TRADES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Trade History" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U123" fromDate="2026-04-01" toDate="2026-05-04">
      <Trades>
        <Trade accountId="U123" symbol="AAPL" assetCategory="STK" buySell="BUY"
               dateTime="20260402;101500" quantity="10" tradePrice="150.25"
               ibExecID="exec-1" />
        <Trade accountId="U123" symbol="MSFT" assetCategory="STK" buySell="SELL"
               dateTime="20260502;111500" quantity="5" tradePrice="410.50"
               ibExecID="exec-2" />
        <Trade accountId="U123" symbol="AAPL" assetCategory="STK" buySell="SELL"
               dateTime="20260315;111500" quantity="1" tradePrice="140.00"
               ibExecID="outside" />
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

FLEX_TRADES_CSV = """"BOF","U12024249","trades","1","20260501","20260501","20260504;041910","100","100"
"BOA","U12024249"
"BOS","TRNT","Trades; trade date basis"
"ClientAccountID","AccountAlias","Model","CurrencyPrimary","FXRateToBase","AssetClass","SubCategory","Symbol","Description","Conid","Buy/Sell","DateTime","Quantity","TradePrice","IBExecID"
"U12024249","","","USD","1","STK","COMMON","AAPL","APPLE INC","265598","BUY","20260501;101500","10","150.25","exec-1"
"U12024249","","","USD","1","STK","COMMON","MSFT","MICROSOFT CORP","272093","SELL","20260502;111500","5","410.50","exec-2"
"EOS","TRNT"
"EOF"
"""


@pytest.mark.asyncio
async def test_flex_trade_history_requires_token(mcp_server, monkeypatch):
    monkeypatch.delenv("IB_FLEX_TOKEN", raising=False)

    with pytest.raises(Exception, match="IB_FLEX_TOKEN is not configured"):
        await mcp_server.mcp.call_tool("get_flex_trade_history", {
            "start_date": "2026-04-01",
            "end_date": "2026-05-04",
            "query_id": "123456",
        })


@pytest.mark.asyncio
async def test_flex_trade_history_parses_and_filters_trades(mcp_server, monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "token")
    mcp_server._execute_flex_query = AsyncMock(
        return_value={
            "data": FLEX_TRADES_XML,
            "reference_code": "ref-123",
            "attempts": 1,
        }
    )

    response = await mcp_server.mcp.call_tool("get_flex_trade_history", {
        "start_date": "2026-04-01",
        "end_date": "2026-05-04",
        "query_id": "123456",
        "symbol": "AAPL",
    })
    result = extract_json(response)

    assert result["success"] is True
    assert result["source"] == "ibkr_flex"
    assert result["query_id"] == "123456"
    assert result["statement"]["query_name"] == "Trade History"
    assert result["returned_trades"] == 1
    assert result["trades"][0]["ibExecID"] == "exec-1"
    assert result["trades"][0]["parsed_trade_time"] == "2026-04-02T10:15:00+00:00"


@pytest.mark.asyncio
async def test_flex_trade_history_uses_env_query_id_and_truncates(mcp_server, monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "token")
    monkeypatch.setenv("IB_FLEX_TRADE_QUERY_ID", "env-query")
    mcp_server._execute_flex_query = AsyncMock(
        return_value={
            "data": FLEX_TRADES_XML,
            "reference_code": "ref-123",
            "attempts": 1,
        }
    )

    response = await mcp_server.mcp.call_tool("get_flex_trade_history", {
        "start_date": "2026-04-01",
        "end_date": "2026-05-04",
        "max_results": 1,
    })
    result = extract_json(response)

    assert result["query_id"] == "env-query"
    assert result["available_trades_after_filter"] == 2
    assert result["returned_trades"] == 1
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_flex_trade_history_uses_named_period_query_id(mcp_server, monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "token")
    monkeypatch.setenv("IB_FLEX_TRADE_QUERY_ID_YTD", "ytd-query")
    mcp_server._execute_flex_query = AsyncMock(
        return_value={
            "data": FLEX_TRADES_XML,
            "reference_code": "ref-123",
            "attempts": 1,
        }
    )

    response = await mcp_server.mcp.call_tool("get_flex_trade_history", {
        "start_date": "2026-04-01",
        "end_date": "2026-05-04",
        "query_period": "ytd",
    })
    result = extract_json(response)

    assert result["query_id"] == "ytd-query"
    assert result["query_period"] == "ytd"
    assert result["query_env_name"] == "IB_FLEX_TRADE_QUERY_ID_YTD"


@pytest.mark.asyncio
async def test_flex_trade_history_parses_csv_statement(mcp_server, monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "token")
    monkeypatch.setenv("IB_FLEX_TRADE_QUERY_ID", "env-query")
    mcp_server._execute_flex_query = AsyncMock(
        return_value={
            "data": FLEX_TRADES_CSV,
            "format": "csv",
            "reference_code": "ref-123",
            "attempts": 1,
        }
    )

    response = await mcp_server.mcp.call_tool("get_flex_trade_history", {
        "start_date": "2026-05-01",
        "end_date": "2026-05-04",
        "symbol": "MSFT",
    })
    result = extract_json(response)

    assert result["success"] is True
    assert result["format"] == "csv"
    assert result["statement"]["query_name"] == "trades"
    assert result["returned_trades"] == 1
    assert result["trades"][0]["Symbol"] == "MSFT"
    assert result["trades"][0]["symbol"] == "MSFT"
    assert result["trades"][0]["parsed_trade_time"] == "2026-05-02T11:15:00+00:00"
