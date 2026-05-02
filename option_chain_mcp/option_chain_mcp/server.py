from __future__ import annotations

import argparse
import asyncio
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
    open_interest: float | None
    volume: float | None


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


def _clean_nonnegative_float(value: Any) -> float | None:
    number = _clean_float(value)
    if number is None or number < 0:
        return None
    return number


def _compact_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _first_clean_float(*values: Any) -> float | None:
    for value in values:
        clean = _clean_float(value)
        if clean is not None:
            return clean
    return None


def _first_nonnegative_float(*values: Any) -> float | None:
    for value in values:
        clean = _clean_nonnegative_float(value)
        if clean is not None:
            return clean
    return None


def _today_yyyymmdd() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def _parse_yyyymmdd(value: str) -> datetime:
    normalized = value.replace("-", "")
    return datetime.strptime(normalized, "%Y%m%d").replace(tzinfo=UTC)


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
        ticker = self.ib.reqMktData(stock, "", False, False)
        await asyncio.sleep(2)
        try:
            price = _clean_float(ticker.marketPrice())
            if price is not None and price > 0:
                return price
            for attr in ("last", "close", "bid", "ask"):
                price = _clean_float(getattr(ticker, attr, None))
                if price is not None and price > 0:
                    return price
            return None
        finally:
            self.ib.cancelMktData(stock)

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

    def _select_closest_expiry(self, expirations: Iterable[str], target_expiry: str) -> str:
        valid = sorted(exp for exp in expirations if exp >= _today_yyyymmdd())
        if not valid:
            valid = sorted(expirations)
        if not valid:
            raise ValueError("No option expirations returned by IBKR")

        target = _parse_yyyymmdd(target_expiry)
        return min(valid, key=lambda exp: abs((_parse_yyyymmdd(exp) - target).days))

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

    def _select_otm_put_strikes(
        self,
        strikes: Iterable[float],
        spot_price: float,
        min_otm_pct: float,
        max_otm_pct: float,
        max_strikes: int,
    ) -> list[float]:
        high = spot_price * (1 - min_otm_pct)
        low = spot_price * (1 - max_otm_pct)
        selected = sorted(float(strike) for strike in strikes if low <= float(strike) <= high)
        if len(selected) <= max_strikes:
            return selected

        # Keep an even spread across the OTM range rather than only nearest strikes.
        step = (len(selected) - 1) / max(max_strikes - 1, 1)
        indexes = sorted({round(i * step) for i in range(max_strikes)})
        return [selected[index] for index in indexes]

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
        bid = _clean_nonnegative_float(getattr(ticker, "bid", None))
        ask = _clean_nonnegative_float(getattr(ticker, "ask", None))
        midpoint = None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            midpoint = (bid + ask) / 2
        mark = _clean_nonnegative_float(ticker.marketPrice())

        greeks = getattr(ticker, "modelGreeks", None) or getattr(ticker, "bidGreeks", None)
        right = getattr(contract, "right", "")
        return OptionQuote(
            symbol=str(getattr(contract, "symbol", "")),
            expiry=str(getattr(contract, "lastTradeDateOrContractMonth", "")),
            strike=float(getattr(contract, "strike", 0.0)),
            right="call" if right == "C" else "put",
            con_id=int(getattr(contract, "conId", 0) or 0),
            local_symbol=str(getattr(contract, "localSymbol", "")),
            bid=bid,
            ask=ask,
            last=_clean_nonnegative_float(getattr(ticker, "last", None)),
            close=_clean_nonnegative_float(getattr(ticker, "close", None)),
            mark=mark,
            midpoint=midpoint,
            implied_volatility=_clean_float(getattr(greeks, "impliedVol", None)),
            delta=_clean_float(getattr(greeks, "delta", None)),
            gamma=_clean_float(getattr(greeks, "gamma", None)),
            theta=_clean_float(getattr(greeks, "theta", None)),
            vega=_clean_float(getattr(greeks, "vega", None)),
            open_interest=_first_nonnegative_float(
                getattr(ticker, "putOpenInterest", None) if right == "P" else None,
                getattr(ticker, "callOpenInterest", None) if right == "C" else None,
                getattr(ticker, "openInterest", None),
            ),
            volume=_first_nonnegative_float(
                getattr(ticker, "putVolume", None) if right == "P" else None,
                getattr(ticker, "callVolume", None) if right == "C" else None,
                getattr(ticker, "volume", None),
            ),
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

        @self.mcp.tool(
            description=(
                "Return a read-only portfolio snapshot: open positions, cash balances, "
                "and key account values grouped by account."
            )
        )
        async def get_portfolio_snapshot(
            account: Annotated[
                str,
                "Optional account id. Empty returns all accounts visible to this TWS session.",
            ] = "",
            include_zero_positions: Annotated[
                bool,
                "Include zero-quantity positions returned by TWS.",
            ] = False,
        ) -> dict[str, Any]:
            await self._ensure_connected()

            managed_accounts = list(await _maybe_await(self.ib.managedAccounts()) or [])
            requested_accounts = [account] if account else managed_accounts

            raw_positions = list(self.ib.positions(account) or [])
            positions_by_account: dict[str, list[dict[str, Any]]] = {
                account_id: [] for account_id in requested_accounts
            }

            for position in raw_positions:
                quantity = _clean_float(getattr(position, "position", None))
                if quantity == 0 and not include_zero_positions:
                    continue

                contract = getattr(position, "contract", None)
                account_id = str(getattr(position, "account", ""))
                if account and account_id != account:
                    continue

                positions_by_account.setdefault(account_id, []).append(
                    {
                        "account": account_id,
                        "symbol": getattr(contract, "symbol", None),
                        "sec_type": getattr(contract, "secType", None),
                        "con_id": getattr(contract, "conId", None),
                        "exchange": getattr(contract, "exchange", None),
                        "primary_exchange": getattr(contract, "primaryExchange", None),
                        "currency": getattr(contract, "currency", None),
                        "local_symbol": getattr(contract, "localSymbol", None),
                        "trading_class": getattr(contract, "tradingClass", None),
                        "position": quantity,
                        "avg_cost": _clean_float(getattr(position, "avgCost", None)),
                    }
                )

            summary_items = list(await self.ib.accountSummaryAsync(account))
            values_by_account: dict[str, dict[str, Any]] = {
                account_id: {} for account_id in requested_accounts
            }
            cash_balances: dict[str, dict[str, float]] = {
                account_id: {} for account_id in requested_accounts
            }

            cash_tags = {
                "CashBalance",
                "TotalCashBalance",
                "SettledCash",
                "AccruedCash",
            }

            for item in summary_items:
                account_id = str(getattr(item, "account", ""))
                tag = str(getattr(item, "tag", ""))
                currency = str(getattr(item, "currency", ""))
                value_raw = getattr(item, "value", None)
                value = _clean_float(value_raw)

                values_by_account.setdefault(account_id, {})
                cash_balances.setdefault(account_id, {})

                if currency:
                    values_by_account[account_id].setdefault(tag, {})[currency] = (
                        value if value is not None else value_raw
                    )
                else:
                    values_by_account[account_id][tag] = value if value is not None else value_raw

                if tag in cash_tags and currency and value is not None:
                    cash_balances[account_id][f"{tag}:{currency}"] = value

            accounts: dict[str, Any] = {}
            for account_id in sorted(set(requested_accounts) | set(positions_by_account)):
                account_values = values_by_account.get(account_id, {})
                accounts[account_id] = {
                    "cash_balances": cash_balances.get(account_id, {}),
                    "net_liquidation": account_values.get("NetLiquidation"),
                    "buying_power": account_values.get("BuyingPower"),
                    "available_funds": account_values.get("AvailableFunds"),
                    "excess_liquidity": account_values.get("ExcessLiquidity"),
                    "gross_position_value": account_values.get("GrossPositionValue"),
                    "positions": sorted(
                        positions_by_account.get(account_id, []),
                        key=lambda row: (str(row.get("sec_type")), str(row.get("symbol"))),
                    ),
                    "account_values": account_values,
                }

            return {
                "connected": self.ib.isConnected(),
                "readonly": True,
                "managed_accounts": managed_accounts,
                "requested_account": account or None,
                "accounts": accounts,
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
            quote_wait_seconds: Annotated[
                float,
                Field(
                    description="Seconds to wait for option ticks after subscribing",
                    ge=1,
                    le=10,
                ),
            ] = 3,
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
            qualified_raw = await self.ib.qualifyContractsAsync(*contracts)
            qualified = [contract for contract in qualified_raw if contract is not None]
            if not qualified:
                raise ValueError("IBKR did not qualify any option contracts for the request")

            await self._set_market_data_type(market_data_type)
            tickers = [
                self.ib.reqMktData(contract, "", False, False)
                for contract in qualified
            ]
            await asyncio.sleep(quote_wait_seconds)
            quotes = [asdict(self._quote_from_ticker(ticker)) for ticker in tickers]
            for contract in qualified:
                self.ib.cancelMktData(contract)
            quotes.sort(key=lambda row: (row["expiry"], row["strike"], row["right"]))

            return {
                "symbol": stock.symbol,
                "underlying_con_id": stock.conId,
                "underlying_price": underlying_price,
                "expiry": selected_expiry,
                "right": right,
                "market_data_type": market_data_type,
                "quote_wait_seconds": quote_wait_seconds,
                "chain_exchange": getattr(chain, "exchange", ""),
                "requested_contracts": len(contracts),
                "qualified_contracts": len(qualified),
                "skipped_unqualified_contracts": len(contracts) - len(qualified),
                "returned_quotes": len(quotes),
                "quotes": quotes,
            }

        @self.mcp.tool(
            description=(
                "Find read-only cash-secured put candidates near a target expiry, "
                "filtered to an OTM percentage range below spot."
            )
        )
        async def find_cash_secured_put_opportunities(
            tickers: Annotated[list[str], "Stock tickers, for example ['AAPL', 'MSFT']"],
            target_expiry: Annotated[
                str,
                "Target expiry as YYYY-MM-DD or YYYYMMDD. Nearest available expiry is used.",
            ] = "2026-06-12",
            min_otm_pct: Annotated[
                float,
                Field(description="Lower OTM bound, e.g. 0.10 means 10% below spot", ge=0.01, le=0.5),
            ] = 0.10,
            max_otm_pct: Annotated[
                float,
                Field(description="Upper OTM bound, e.g. 0.20 means 20% below spot", ge=0.01, le=0.8),
            ] = 0.20,
            max_strikes_per_ticker: Annotated[
                int,
                Field(description="Maximum put strikes to return per ticker", ge=1, le=30),
            ] = 12,
            exchange: Annotated[str, "Stock routing exchange"] = "SMART",
            currency: Annotated[str, "Contract currency"] = "USD",
            primary_exchange: Annotated[str, "Primary exchange hint, usually empty"] = "",
            option_exchange: Annotated[str, "Preferred option exchange, usually SMART"] = "SMART",
            market_data_type: Annotated[
                int,
                Field(
                    description="IBKR market data type: 1 live, 2 frozen, 3 delayed, 4 delayed frozen",
                    ge=1,
                    le=4,
                ),
            ] = 3,
            quote_wait_seconds: Annotated[
                float,
                Field(description="Seconds to wait for option ticks after subscribing", ge=1, le=10),
            ] = 4,
        ) -> dict[str, Any]:
            if min_otm_pct >= max_otm_pct:
                raise ValueError("min_otm_pct must be lower than max_otm_pct")

            await self._ensure_connected()
            await self._set_market_data_type(market_data_type)

            grouped: dict[str, Any] = {}
            for raw_symbol in tickers:
                symbol = raw_symbol.upper().strip()
                if not symbol:
                    continue

                try:
                    stock = await self._stock_contract(symbol, exchange, currency, primary_exchange)
                    params = await self._option_params(stock)
                    chain = self._choose_chain(params, option_exchange, "")
                    spot_price = await self._underlying_price(stock, market_data_type)
                    if spot_price is None or spot_price <= 0:
                        raise ValueError("Could not determine current underlying price")
                    expiry = self._select_closest_expiry(
                        getattr(chain, "expirations", []) or [],
                        target_expiry,
                    )
                    strikes = self._select_otm_put_strikes(
                        getattr(chain, "strikes", []) or [],
                        spot_price,
                        min_otm_pct,
                        max_otm_pct,
                        max_strikes_per_ticker,
                    )
                    contracts = self._option_contracts(
                        symbol,
                        expiry,
                        strikes,
                        "put",
                        option_exchange,
                        currency,
                        str(getattr(chain, "tradingClass", "")),
                    )
                    qualified_raw = await self.ib.qualifyContractsAsync(*contracts)
                    qualified = [contract for contract in qualified_raw if contract is not None]
                    if not qualified:
                        raise ValueError("IBKR did not qualify any OTM put contracts for the request")
                    tickers_live = [
                        self.ib.reqMktData(contract, "100,101,106", False, False)
                        for contract in qualified
                    ]
                    await asyncio.sleep(quote_wait_seconds)

                    contracts_out = []
                    for ticker in tickers_live:
                        quote = asdict(self._quote_from_ticker(ticker))
                        ask = quote["ask"]
                        bid = quote["bid"]
                        mid = quote["midpoint"]
                        if mid is None and bid is not None and ask is not None:
                            mid = (bid + ask) / 2
                        strike = float(quote["strike"])
                        contracts_out.append(
                            {
                                "symbol": quote["symbol"],
                                "expiry": quote["expiry"],
                                "strike": strike,
                                "right": "put",
                                "con_id": quote["con_id"],
                                "local_symbol": quote["local_symbol"],
                                "otm_pct": (spot_price - strike) / spot_price,
                                "bid": bid,
                                "ask": ask,
                                "mid": mid,
                                "delta": quote["delta"],
                                "open_interest": quote["open_interest"],
                                "volume": quote["volume"],
                                "implied_volatility": quote["implied_volatility"],
                                "capital_required": strike * 100,
                                "premium_at_ask": ask * 100 if ask is not None else None,
                                "effective_entry_price": strike - ask if ask is not None else None,
                            }
                        )

                    for contract in qualified:
                        self.ib.cancelMktData(contract)

                    contracts_out.sort(key=lambda row: row["strike"], reverse=True)
                    grouped[symbol] = {
                        "underlying_price": spot_price,
                        "target_expiry": target_expiry,
                        "selected_expiry": expiry,
                        "option_type": "put",
                        "otm_range": {
                            "min": min_otm_pct,
                            "max": max_otm_pct,
                            "strike_low": spot_price * (1 - max_otm_pct),
                            "strike_high": spot_price * (1 - min_otm_pct),
                        },
                        "market_data_type": market_data_type,
                        "quote_wait_seconds": quote_wait_seconds,
                        "contracts": contracts_out,
                    }
                except Exception as exc:
                    logger.exception("Cash-secured put scan failed for %s", symbol)
                    grouped[symbol] = {
                        "error": str(exc),
                        "target_expiry": target_expiry,
                        "option_type": "put",
                    }

            return grouped

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
