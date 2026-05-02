from __future__ import annotations

import argparse
import inspect
import logging
import math
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Iterable, Literal

import ib_async as ib
from fastmcp import FastMCP
from pydantic import Field

logger = logging.getLogger(__name__)

Right = Literal["call", "put", "both"]


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiry: str
    strike: float
    right: str
    con_id: int
    local_symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    close: float | None
    mark: float | None
    midpoint: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None


def _clean_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _compact_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _today_yyyymmdd() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class OptionChainMCP:
    def __init__(self, host: str, port: int, client_id: int) -> None:
        self.mcp = FastMCP(
            name="IBKR Option Chain MCP",
            instructions=(
                "Read-only Interactive Brokers tools for option chain discovery "
                "and option quote snapshots. Do not place orders."
            ),
        )
        self.ib = ib.IB()
        self.host = host
        self.port = port
        self.client_id = client_id
        self.connected = False
        self._register_tools()

    async def _ensure_connected(self) -> None:
        if self.connected and self.ib.isConnected():
            return
        await self.ib.connectAsync(self.host, self.port, self.client_id, readonly=True)
        self.connected = True
        logger.info("Connected read-only to IBKR at %s:%s", self.host, self.port)

    async def _set_market_data_type(self, market_data_type: int) -> None:
        await _maybe_await(self.ib.reqMarketDataType(market_data_type))

    async def _stock_contract(
        self,
        symbol: str,
        exchange: str,
        currency: str,
        primary_exchange: str,
    ) -> ib.Contract:
        contract = ib.Stock(symbol.upper(), exchange, currency, primaryExchange=primary_exchange)
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"No stock contract found for {symbol}")
        return qualified[0]

    async def _option_params(
        self,
        stock: ib.Contract,
    ) -> list[Any]:
        if hasattr(self.ib, "reqSecDefOptParamsAsync"):
            params = await self.ib.reqSecDefOptParamsAsync(
                stock.symbol,
                "",
                stock.secType,
                stock.conId,
            )
        else:
            params = await _maybe_await(
                self.ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
            )
        return list(params or [])

    def _choose_chain(self, params: list[Any], exchange: str, trading_class: str) -> Any:
        if not params:
            raise ValueError("No option chain metadata returned by IBKR")

        exchange = exchange.upper()
        trading_class = trading_class.upper()

        if trading_class:
            for chain in params:
                if str(getattr(chain, "tradingClass", "")).upper() == trading_class:
                    return chain

        for chain in params:
            if str(getattr(chain, "exchange", "")).upper() == exchange:
                return chain

        for chain in params:
            if str(getattr(chain, "exchange", "")).upper() == "SMART":
                return chain

        return params[0]

    async def _underlying_price(
        self,
        stock: ib.Contract,
        market_data_type: int,
    ) -> float | None:
        await self._set_market_data_type(market_data_type)
        tickers = await self.ib.reqTickersAsync(stock)
        if not tickers:
            return None
        ticker = tickers[0]
        price = _clean_float(ticker.marketPrice())
        if price is not None:
            return price
        for attr in ("last", "close", "bid", "ask"):
            price = _clean_float(getattr(ticker, attr, None))
            if price is not None and price > 0:
                return price
        return None

    def _select_expiry(self, expirations: Iterable[str], expiry: str) -> str:
        valid = sorted(exp for exp in expirations if exp >= _today_yyyymmdd())
        if not valid:
            valid = sorted(expirations)
        if not valid:
            raise ValueError("No option expirations returned by IBKR")
        if expiry:
            if expiry not in valid:
                raise ValueError(f"Expiry {expiry} is not available; nearest available is {valid[0]}")
            return expiry
        return valid[0]

    def _select_strikes(
        self,
        strikes: Iterable[float],
        around_price: float | None,
        strike_count: int,
    ) -> list[float]:
        available = sorted(float(strike) for strike in strikes if float(strike) > 0)
        if not available:
            raise ValueError("No option strikes returned by IBKR")
        if around_price is None:
            midpoint = len(available) // 2
            half = strike_count // 2
            return available[max(0, midpoint - half) : midpoint + half + 1]
        ranked = sorted(available, key=lambda strike: (abs(strike - around_price), strike))
        return sorted(ranked[:strike_count])

    def _option_contracts(
        self,
        symbol: str,
        expiry: str,
        strikes: Iterable[float],
        right: Right,
        exchange: str,
        currency: str,
        trading_class: str,
    ) -> list[ib.Option]:
        rights = ["C", "P"] if right == "both" else ["C" if right == "call" else "P"]
        contracts: list[ib.Option] = []
        for strike in strikes:
            for opt_right in rights:
                contract = ib.Option(
                    symbol=symbol.upper(),
                    lastTradeDateOrContractMonth=expiry,
                    strike=float(strike),
                    right=opt_right,
                    exchange=exchange,
                    currency=currency,
                    tradingClass=trading_class or "",
                )
                contracts.append(contract)
        return contracts

    def _quote_from_ticker(self, ticker: Any) -> OptionQuote:
        contract = ticker.contract
        bid = _clean_float(getattr(ticker, "bid", None))
        ask = _clean_float(getattr(ticker, "ask", None))
        midpoint = None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            midpoint = (bid + ask) / 2
        mark = _clean_float(ticker.marketPrice())

        greeks = getattr(ticker, "modelGreeks", None) or getattr(ticker, "bidGreeks", None)
        return OptionQuote(
            symbol=str(getattr(contract, "symbol", "")),
            expiry=str(getattr(contract, "lastTradeDateOrContractMonth", "")),
            strike=float(getattr(contract, "strike", 0.0)),
            right="call" if getattr(contract, "right", "") == "C" else "put",
            con_id=int(getattr(contract, "conId", 0) or 0),
            local_symbol=str(getattr(contract, "localSymbol", "")),
            bid=bid,
            ask=ask,
            last=_clean_float(getattr(ticker, "last", None)),
            close=_clean_float(getattr(ticker, "close", None)),
            mark=mark,
            midpoint=midpoint,
            implied_volatility=_clean_float(getattr(greeks, "impliedVol", None)),
            delta=_clean_float(getattr(greeks, "delta", None)),
            gamma=_clean_float(getattr(greeks, "gamma", None)),
            theta=_clean_float(getattr(greeks, "theta", None)),
            vega=_clean_float(getattr(greeks, "vega", None)),
        )

    def _register_tools(self) -> None:
        @self.mcp.tool(description="Check whether the MCP can connect read-only to IB Gateway or TWS.")
        async def check_ibkr_connection() -> dict[str, Any]:
            try:
                await self._ensure_connected()
                accounts = await _maybe_await(self.ib.managedAccounts())
                server_version = getattr(self.ib.client, "serverVersion", lambda: None)()
                return {
                    "connected": self.ib.isConnected(),
                    "host": self.host,
                    "port": self.port,
                    "client_id": self.client_id,
                    "readonly": True,
                    "server_version": server_version,
                    "managed_accounts": list(accounts or []),
                }
            except Exception as exc:
                return {
                    "connected": False,
                    "host": self.host,
                    "port": self.port,
                    "client_id": self.client_id,
                    "readonly": True,
                    "error": str(exc),
                }

        @self.mcp.tool(description="List available option expirations and strike ranges for a stock ticker.")
        async def get_option_chain_summary(
            symbol: Annotated[str, "Stock ticker, for example AAPL or MSFT"],
            exchange: Annotated[str, "Stock routing exchange"] = "SMART",
            currency: Annotated[str, "Contract currency"] = "USD",
            primary_exchange: Annotated[str, "Primary exchange hint, usually empty"] = "",
            option_exchange: Annotated[str, "Preferred option exchange, usually SMART"] = "SMART",
            trading_class: Annotated[str, "Optional IBKR trading class filter"] = "",
            max_expirations: Annotated[int, Field(ge=1, le=24)] = 12,
        ) -> dict[str, Any]:
            await self._ensure_connected()
            stock = await self._stock_contract(symbol, exchange, currency, primary_exchange)
            params = await self._option_params(stock)
            chain = self._choose_chain(params, option_exchange, trading_class)

            expirations = sorted(getattr(chain, "expirations", []) or [])
            strikes = sorted(float(strike) for strike in (getattr(chain, "strikes", []) or []))
            future_expirations = [exp for exp in expirations if exp >= _today_yyyymmdd()]

            return {
                "symbol": stock.symbol,
                "underlying_con_id": stock.conId,
                "chain_exchange": getattr(chain, "exchange", ""),
                "trading_class": getattr(chain, "tradingClass", ""),
                "multiplier": getattr(chain, "multiplier", ""),
                "expirations": future_expirations[:max_expirations],
                "expiration_count": len(future_expirations),
                "strike_count": len(strikes),
                "min_strike": _compact_number(strikes[0]) if strikes else None,
                "max_strike": _compact_number(strikes[-1]) if strikes else None,
            }

        @self.mcp.tool(description="Get read-only option chain price snapshots for a stock ticker.")
        async def get_option_chain_prices(
            symbol: Annotated[str, "Stock ticker, for example AAPL or MSFT"],
            expiry: Annotated[str, "Option expiry in YYYYMMDD. Empty means nearest future expiry."] = "",
            right: Annotated[Right, "call, put, or both"] = "both",
            strike_count: Annotated[
                int,
                Field(description="Number of strikes around the underlying price", ge=1, le=30),
            ] = 10,
            around_price: Annotated[
                float | None,
                "Optional center price. Empty uses current underlying snapshot.",
            ] = None,
            exchange: Annotated[str, "Stock routing exchange"] = "SMART",
            currency: Annotated[str, "Contract currency"] = "USD",
            primary_exchange: Annotated[str, "Primary exchange hint, usually empty"] = "",
            option_exchange: Annotated[str, "Preferred option exchange, usually SMART"] = "SMART",
            trading_class: Annotated[str, "Optional IBKR trading class filter"] = "",
            market_data_type: Annotated[
                int,
                Field(
                    description="IBKR market data type: 1 live, 2 frozen, 3 delayed, 4 delayed frozen",
                    ge=1,
                    le=4,
                ),
            ] = 1,
            max_contracts: Annotated[
                int,
                Field(description="Hard cap on option contracts requested from IBKR", ge=1, le=60),
            ] = 40,
        ) -> dict[str, Any]:
            await self._ensure_connected()
            stock = await self._stock_contract(symbol, exchange, currency, primary_exchange)
            params = await self._option_params(stock)
            chain = self._choose_chain(params, option_exchange, trading_class)

            selected_expiry = self._select_expiry(getattr(chain, "expirations", []) or [], expiry)
            underlying_price = around_price
            if underlying_price is None:
                underlying_price = await self._underlying_price(stock, market_data_type)

            selected_strikes = self._select_strikes(
                getattr(chain, "strikes", []) or [],
                underlying_price,
                strike_count,
            )
            contracts = self._option_contracts(
                stock.symbol,
                selected_expiry,
                selected_strikes,
                right,
                option_exchange,
                currency,
                trading_class or str(getattr(chain, "tradingClass", "")),
            )
            contracts = contracts[:max_contracts]
            qualified = await self.ib.qualifyContractsAsync(*contracts)
            if not qualified:
                raise ValueError("IBKR did not qualify any option contracts for the request")

            await self._set_market_data_type(market_data_type)
            tickers = await self.ib.reqTickersAsync(*qualified)
            quotes = [asdict(self._quote_from_ticker(ticker)) for ticker in tickers]
            quotes.sort(key=lambda row: (row["expiry"], row["strike"], row["right"]))

            return {
                "symbol": stock.symbol,
                "underlying_con_id": stock.conId,
                "underlying_price": underlying_price,
                "expiry": selected_expiry,
                "right": right,
                "market_data_type": market_data_type,
                "chain_exchange": getattr(chain, "exchange", ""),
                "requested_contracts": len(contracts),
                "returned_quotes": len(quotes),
                "quotes": quotes,
            }

    def run(self, transport: str, http_host: str, http_port: int) -> None:
        logging.basicConfig(level=logging.INFO)
        try:
            if transport == "http":
                self.mcp.run(transport="streamable-http", host=http_host, port=http_port)
            else:
                self.mcp.run()
        finally:
            if self.connected:
                self.ib.disconnect()
                self.connected = False


def main() -> None:
    parser = argparse.ArgumentParser(description="IBKR read-only option-chain MCP server")
    parser.add_argument("--host", default=os.getenv("IB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("IB_PORT", "7497")))
    parser.add_argument("--client-id", type=int, default=int(os.getenv("IB_CLIENT_ID", "21")))
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.getenv("IB_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--http-host", default=os.getenv("IB_MCP_HTTP_HOST", "127.0.0.1"))
    parser.add_argument("--http-port", type=int, default=int(os.getenv("IB_MCP_HTTP_PORT", "8010")))
    args = parser.parse_args()

    server = OptionChainMCP(args.host, args.port, args.client_id)
    server.run(args.transport, args.http_host, args.http_port)


if __name__ == "__main__":
    main()
