from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import logging
import math
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time
from typing import Annotated, Any, Iterable, Literal

import ib_async as ib
from fastmcp import FastMCP
from pydantic import Field

logger = logging.getLogger(__name__)

Right = Literal["call", "put", "both"]
FlexTradeQueryPeriod = Literal["default", "last_business_week", "ytd", "mtd"]
FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"


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


def _parse_date_or_datetime(value: str, boundary: Literal["start", "end"]) -> datetime:
    raw_value = value.strip()
    if not raw_value:
        raise ValueError(f"{boundary}_date is required")

    normalized = raw_value.replace("T", " ").replace("/", "-")
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y%m%d %H:%M",
        "%Y-%m-%d",
        "%Y%m%d",
    ]

    parsed: datetime | None = None
    for date_format in formats:
        try:
            parsed = datetime.strptime(normalized, date_format)
            break
        except ValueError:
            continue

    if parsed is None:
        raise ValueError(
            f"{boundary}_date must be YYYY-MM-DD, YYYYMMDD, or include HH:MM[:SS]"
        )

    date_only = len(normalized.replace("-", "")) == 8
    if date_only and boundary == "end":
        parsed = datetime.combine(parsed.date(), time(23, 59, 59))
    elif date_only:
        parsed = datetime.combine(parsed.date(), time(0, 0, 0))

    return parsed.replace(tzinfo=UTC)


def _format_execution_filter_time(value: datetime) -> str:
    return value.strftime("%Y%m%d %H:%M:%S")


def _parse_execution_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("T", " ").replace("-", "")
    formats = [
        "%Y%m%d %H:%M:%S %Z",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d  %H:%M:%S",
        "%Y%m%d",
    ]
    for date_format in formats:
        try:
            parsed = datetime.strptime(normalized, date_format)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _contract_summary(contract: Any) -> dict[str, Any]:
    return {
        "symbol": getattr(contract, "symbol", None),
        "sec_type": getattr(contract, "secType", None),
        "con_id": getattr(contract, "conId", None),
        "exchange": getattr(contract, "exchange", None),
        "primary_exchange": getattr(contract, "primaryExchange", None),
        "currency": getattr(contract, "currency", None),
        "local_symbol": getattr(contract, "localSymbol", None),
        "trading_class": getattr(contract, "tradingClass", None),
        "expiry": getattr(contract, "lastTradeDateOrContractMonth", None),
        "strike": _clean_float(getattr(contract, "strike", None)),
        "right": getattr(contract, "right", None),
        "multiplier": getattr(contract, "multiplier", None),
    }


def _commission_report_summary(report: Any) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "exec_id": getattr(report, "execId", None),
        "commission": _clean_float(getattr(report, "commission", None)),
        "currency": getattr(report, "currency", None),
        "realized_pnl": _clean_float(getattr(report, "realizedPNL", None)),
        "yield": _clean_float(getattr(report, "yield_", None)),
        "yield_redemption_date": getattr(report, "yieldRedemptionDate", None),
    }


def _strip_xml_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _xml_child_text(element: ET.Element, child_name: str) -> str | None:
    for child in list(element):
        if _strip_xml_namespace(child.tag) == child_name:
            return (child.text or "").strip() or None
    return None


def _xml_to_data(element: ET.Element) -> dict[str, Any]:
    data: dict[str, Any] = dict(element.attrib)
    text = (element.text or "").strip()
    if text:
        data["text"] = text

    for child in list(element):
        key = _strip_xml_namespace(child.tag)
        value = _xml_to_data(child)
        if key in data:
            if not isinstance(data[key], list):
                data[key] = [data[key]]
            data[key].append(value)
        else:
            data[key] = value
    return data


def _flex_key_to_snake(value: str) -> str:
    output = []
    for index, char in enumerate(value.strip()):
        if char.isupper() and index > 0 and value[index - 1].islower():
            output.append("_")
        elif char in {" ", "-", "/", ";"}:
            output.append("_")
            continue
        output.append(char.lower())
    return "".join(output).strip("_")


def _parse_flex_trade_datetime(row: dict[str, Any]) -> datetime | None:
    raw_value = _first_present_string(
        row.get("dateTime"),
        row.get("DateTime"),
        row.get("date_time"),
        row.get("tradeDateTime"),
        row.get("TradeDateTime"),
        row.get("trade_date_time"),
        row.get("transactionDateTime"),
        row.get("TransactionDateTime"),
        row.get("transaction_date_time"),
        row.get("tradeDate"),
        row.get("TradeDate"),
        row.get("trade_date"),
        row.get("date"),
        row.get("Date"),
        row.get("reportDate"),
        row.get("ReportDate"),
        row.get("report_date"),
    )
    if raw_value is None:
        return None

    value = raw_value.strip()
    if ";" in value:
        left, right = value.split(";", 1)
        value = f"{left.strip()} {right.strip()}"
    value = value.replace("T", " ").replace("/", "-")
    compact = value.replace("-", "")

    formats = [
        "%Y%m%d %H:%M:%S",
        "%Y%m%d %H%M%S",
        "%Y%m%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y%m%d",
        "%Y-%m-%d",
    ]
    for date_format in formats:
        candidate = compact if date_format.startswith("%Y%m%d") else value
        try:
            return datetime.strptime(candidate, date_format).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _first_present_string(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _row_matches_text_filter(row: dict[str, Any], expected: str, keys: Iterable[str]) -> bool:
    normalized_expected = expected.upper().strip()
    if not normalized_expected:
        return True
    return any(
        str(row.get(key, row.get(_flex_key_to_snake(key), ""))).upper().strip() == normalized_expected
        for key in keys
    )


def _row_matches_symbol_filter(row: dict[str, Any], symbol: str) -> bool:
    normalized_symbol = symbol.upper().strip()
    if not normalized_symbol:
        return True
    for key in ("symbol", "underlyingSymbol", "issuer", "description"):
        value = str(row.get(key, row.get(_flex_key_to_snake(key), ""))).upper()
        if value == normalized_symbol or value.startswith(f"{normalized_symbol} "):
            return True
    return False


def _row_matches_side_filter(row: dict[str, Any], side: str) -> bool:
    normalized_side = side.upper().strip()
    if not normalized_side:
        return True

    flex_side = str(
        row.get("buySell", row.get("buy_sell", row.get("side", "")))
    ).upper().strip()
    aliases = {
        "BOT": {"BOT", "BUY", "BOUGHT"},
        "SLD": {"SLD", "SELL", "SOLD"},
    }
    return flex_side in aliases.get(normalized_side, {normalized_side})


def _http_get_text(url: str, params: dict[str, str], timeout: float) -> str:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}", headers={"User-Agent": "ib-mcp-overlay/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Flex Web Service request failed: {exc}") from exc


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
        self._wsh_metadata_fetched = False
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

    async def _ensure_wsh_metadata(self) -> None:
        """Fetch WSH metadata once per session to initialise the event filter."""
        if self._wsh_metadata_fetched:
            return
        try:
            if hasattr(self.ib, "reqWshMetaDataAsync"):
                await self.ib.reqWshMetaDataAsync()
            else:
                await _maybe_await(self.ib.reqWshMetaData())
            self._wsh_metadata_fetched = True
            logger.info("WSH metadata initialised")
        except Exception as exc:
            logger.warning("WSH metadata fetch failed (subscription may be inactive): %s", exc)

    async def _flex_send_request(
        self,
        token: str,
        query_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        xml_text = await asyncio.to_thread(
            _http_get_text,
            f"{FLEX_BASE_URL}/SendRequest",
            {"t": token, "q": query_id, "v": "3"},
            timeout_seconds,
        )
        root = ET.fromstring(xml_text)
        status = _xml_child_text(root, "Status")
        if status == "Success":
            return {
                "reference_code": _xml_child_text(root, "ReferenceCode"),
                "url": _xml_child_text(root, "Url"),
                "raw_xml": xml_text,
            }
        if status == "Fail":
            return {
                "error": _xml_child_text(root, "ErrorMessage") or _xml_child_text(root, "ErrorCode"),
                "error_code": _xml_child_text(root, "ErrorCode"),
                "raw_xml": xml_text,
            }
        raise ValueError("Unexpected response format from IBKR Flex SendRequest")

    async def _flex_get_statement(
        self,
        token: str,
        reference_code: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        xml_text = await asyncio.to_thread(
            _http_get_text,
            f"{FLEX_BASE_URL}/GetStatement",
            {"t": token, "q": reference_code, "v": "3"},
            timeout_seconds,
        )
        if not xml_text.lstrip().startswith("<"):
            return {"data": xml_text, "format": "csv"}

        root = ET.fromstring(xml_text)
        root_name = _strip_xml_namespace(root.tag)
        if root_name == "FlexQueryResponse":
            return {"data": xml_text, "format": "xml"}

        status = _xml_child_text(root, "Status")
        if status == "Success":
            return {"data": xml_text, "format": "xml"}
        if status == "Fail":
            return {
                "error": _xml_child_text(root, "ErrorMessage") or _xml_child_text(root, "ErrorCode"),
                "error_code": _xml_child_text(root, "ErrorCode"),
                "raw_xml": xml_text,
            }
        raise ValueError("Unexpected response format from IBKR Flex GetStatement")

    async def _execute_flex_query(
        self,
        token: str,
        query_id: str,
        max_retries: int,
        retry_delay_seconds: float,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        send_response = await self._flex_send_request(token, query_id, timeout_seconds)
        if send_response.get("error"):
            return send_response

        reference_code = send_response.get("reference_code")
        if not reference_code:
            return {"error": "No reference code received from IBKR Flex SendRequest"}

        for attempt in range(1, max_retries + 1):
            await asyncio.sleep(retry_delay_seconds)
            statement_response = await self._flex_get_statement(token, reference_code, timeout_seconds)
            if not statement_response.get("error"):
                statement_response["reference_code"] = reference_code
                statement_response["attempts"] = attempt
                return statement_response

            error = str(statement_response.get("error", ""))
            error_code = str(statement_response.get("error_code", ""))
            if error_code == "1019" or "in progress" in error.lower() or "not ready" in error.lower():
                continue
            statement_response["reference_code"] = reference_code
            statement_response["attempts"] = attempt
            return statement_response

        return {
            "error": f"Flex statement not ready after {max_retries} retries",
            "reference_code": reference_code,
            "error_code": "TIMEOUT",
        }

    def _parse_flex_trades(
        self,
        xml_text: str,
        start_at: datetime,
        end_at: datetime,
        account: str,
        symbol: str,
        sec_type: str,
        side: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        root = ET.fromstring(xml_text)
        parsed_statement = {
            "query_name": root.attrib.get("queryName"),
            "type": root.attrib.get("type"),
            "flex_statements": [],
        }
        trades: list[dict[str, Any]] = []

        for statement in root.iter():
            if _strip_xml_namespace(statement.tag) != "FlexStatement":
                continue
            parsed_statement["flex_statements"].append(dict(statement.attrib))

        for element in root.iter():
            if _strip_xml_namespace(element.tag) != "Trade":
                continue
            row = _xml_to_data(element)
            trade_time = _parse_flex_trade_datetime(row)
            if trade_time is not None and not (start_at <= trade_time <= end_at):
                continue
            if not _row_matches_text_filter(row, account, ("accountId", "acctId", "account")):
                continue
            if not _row_matches_symbol_filter(row, symbol):
                continue
            if not _row_matches_text_filter(row, sec_type, ("assetCategory", "secType")):
                continue
            if not _row_matches_side_filter(row, side):
                continue

            row["parsed_trade_time"] = trade_time.isoformat() if trade_time else None
            trades.append(row)

        trades.sort(key=lambda row: row.get("parsed_trade_time") or "")
        return parsed_statement, trades

    def _parse_flex_csv_trades(
        self,
        csv_text: str,
        start_at: datetime,
        end_at: datetime,
        account: str,
        symbol: str,
        sec_type: str,
        side: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        reader = csv.reader(csv_text.splitlines())
        statement: dict[str, Any] = {
            "format": "csv",
            "sections": [],
        }
        current_section: str | None = None
        headers_by_section: dict[str, list[str]] = {}
        trades: list[dict[str, Any]] = []

        for raw_row in reader:
            if not raw_row:
                continue
            code = raw_row[0]

            if code == "BOF":
                statement["account_id"] = raw_row[1] if len(raw_row) > 1 else None
                statement["query_name"] = raw_row[2] if len(raw_row) > 2 else None
                statement["from_date"] = raw_row[4] if len(raw_row) > 4 else None
                statement["to_date"] = raw_row[5] if len(raw_row) > 5 else None
                continue

            if code == "BOS":
                current_section = raw_row[1] if len(raw_row) > 1 else None
                statement["sections"].append({
                    "code": current_section,
                    "name": raw_row[2] if len(raw_row) > 2 else None,
                })
                continue

            if code == "EOS":
                current_section = None
                continue

            if current_section is None:
                continue

            if current_section not in headers_by_section:
                headers_by_section[current_section] = raw_row
                continue

            if current_section != "TRNT":
                continue

            headers = headers_by_section[current_section]
            row = {
                headers[index]: raw_row[index] if index < len(raw_row) else ""
                for index in range(len(headers))
            }
            for key, value in list(row.items()):
                row.setdefault(_flex_key_to_snake(key), value)

            trade_time = _parse_flex_trade_datetime(row)
            if trade_time is not None and not (start_at <= trade_time <= end_at):
                continue
            if not _row_matches_text_filter(row, account, ("accountId", "ClientAccountID", "account_id", "client_account_id")):
                continue
            if not _row_matches_symbol_filter(row, symbol):
                continue
            if not _row_matches_text_filter(row, sec_type, ("assetCategory", "AssetClass", "asset_category", "asset_class", "secType")):
                continue
            if not _row_matches_side_filter(row, side):
                continue

            row["parsed_trade_time"] = trade_time.isoformat() if trade_time else None
            trades.append(row)

        trades.sort(key=lambda row: row.get("parsed_trade_time") or "")
        return statement, trades

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

        @self.mcp.tool(
            description=(
                "Return read-only IBKR execution/trade history for a requested time window. "
                "Supports optional account, symbol, security type, exchange, and side filters."
            )
        )
        async def get_trade_history(
            start_date: Annotated[
                str,
                "Start date/time as YYYY-MM-DD, YYYYMMDD, or YYYY-MM-DD HH:MM[:SS].",
            ],
            end_date: Annotated[
                str,
                "End date/time as YYYY-MM-DD, YYYYMMDD, or YYYY-MM-DD HH:MM[:SS].",
            ],
            account: Annotated[
                str,
                "Optional IBKR account id. Empty returns all visible accounts.",
            ] = "",
            symbol: Annotated[
                str,
                "Optional ticker/local symbol filter.",
            ] = "",
            sec_type: Annotated[
                str,
                "Optional security type filter, e.g. STK, OPT, FUT, CASH.",
            ] = "",
            exchange: Annotated[
                str,
                "Optional execution exchange filter.",
            ] = "",
            side: Annotated[
                Literal["", "BOT", "SLD"],
                "Optional execution side filter: BOT or SLD.",
            ] = "",
            max_results: Annotated[
                int,
                Field(description="Maximum executions returned after client-side filtering", ge=1, le=500),
            ] = 200,
        ) -> dict[str, Any]:
            await self._ensure_connected()

            start_at = _parse_date_or_datetime(start_date, "start")
            end_at = _parse_date_or_datetime(end_date, "end")
            if start_at > end_at:
                raise ValueError("start_date must be earlier than or equal to end_date")

            execution_filter = ib.ExecutionFilter(
                acctCode=account,
                time=_format_execution_filter_time(start_at),
                symbol=symbol.upper().strip(),
                secType=sec_type.upper().strip(),
                exchange=exchange.upper().strip(),
                side=side,
            )

            if hasattr(self.ib, "reqExecutionsAsync"):
                fills_raw = await self.ib.reqExecutionsAsync(execution_filter)
            else:
                fills_raw = await _maybe_await(self.ib.reqExecutions(execution_filter))

            executions: list[dict[str, Any]] = []
            for fill in list(fills_raw or []):
                execution = getattr(fill, "execution", None)
                contract = getattr(fill, "contract", None)
                commission_report = getattr(fill, "commissionReport", None)

                execution_time = _parse_execution_time(
                    getattr(execution, "time", None) or getattr(fill, "time", None)
                )
                if execution_time is not None and not (start_at <= execution_time <= end_at):
                    continue

                executions.append(
                    {
                        "time": execution_time.isoformat() if execution_time else None,
                        "account": getattr(execution, "acctNumber", None),
                        "exec_id": getattr(execution, "execId", None),
                        "order_id": getattr(execution, "orderId", None),
                        "perm_id": getattr(execution, "permId", None),
                        "client_id": getattr(execution, "clientId", None),
                        "side": getattr(execution, "side", None),
                        "shares": _clean_float(getattr(execution, "shares", None)),
                        "price": _clean_float(getattr(execution, "price", None)),
                        "avg_price": _clean_float(getattr(execution, "avgPrice", None)),
                        "cum_qty": _clean_float(getattr(execution, "cumQty", None)),
                        "last_liquidity": getattr(execution, "lastLiquidity", None),
                        "exchange": getattr(execution, "exchange", None),
                        "order_ref": getattr(execution, "orderRef", None),
                        "contract": _contract_summary(contract),
                        "commission_report": _commission_report_summary(commission_report),
                    }
                )

            executions.sort(key=lambda row: row.get("time") or "")
            truncated = len(executions) > max_results

            return {
                "connected": self.ib.isConnected(),
                "readonly": True,
                "requested_window": {
                    "start": start_at.isoformat(),
                    "end": end_at.isoformat(),
                },
                "filters": {
                    "account": account or None,
                    "symbol": symbol.upper().strip() or None,
                    "sec_type": sec_type.upper().strip() or None,
                    "exchange": exchange.upper().strip() or None,
                    "side": side or None,
                },
                "ib_execution_filter_time": execution_filter.time,
                "returned_executions": min(len(executions), max_results),
                "available_executions_after_filter": len(executions),
                "truncated": truncated,
                "executions": executions[:max_results],
                "notes": [
                    "This tool is read-only and uses IBKR execution reports.",
                    "IBKR/TWS controls how much execution history is available from the API; older trades may require Flex Queries or statements.",
                    "The start date is passed to IBKR, while the end date is applied by this MCP after results are returned.",
                ],
            }

        @self.mcp.tool(
            description=(
                "Return historical trades from an IBKR Flex Query. Requires IB_FLEX_TOKEN "
                "and either a query_id argument or IB_FLEX_TRADE_QUERY_ID."
            )
        )
        async def get_flex_trade_history(
            start_date: Annotated[
                str,
                "Start date/time to filter returned Flex trades as YYYY-MM-DD, YYYYMMDD, or YYYY-MM-DD HH:MM[:SS].",
            ],
            end_date: Annotated[
                str,
                "End date/time to filter returned Flex trades as YYYY-MM-DD, YYYYMMDD, or YYYY-MM-DD HH:MM[:SS].",
            ],
            query_id: Annotated[
                str,
                "Optional IBKR Flex Query ID. Overrides query_period when provided.",
            ] = "",
            query_period: Annotated[
                FlexTradeQueryPeriod,
                "Named Flex query period: default, last_business_week, ytd, or mtd.",
            ] = "default",
            account: Annotated[
                str,
                "Optional account id filter applied after the Flex statement is returned.",
            ] = "",
            symbol: Annotated[
                str,
                "Optional symbol filter applied after the Flex statement is returned.",
            ] = "",
            sec_type: Annotated[
                str,
                "Optional Flex assetCategory/secType filter, e.g. STK, OPT, FUT.",
            ] = "",
            side: Annotated[
                Literal["", "BOT", "SLD"],
                "Optional side filter. BOT matches BUY/BOUGHT, SLD matches SELL/SOLD.",
            ] = "",
            max_results: Annotated[
                int,
                Field(description="Maximum parsed Trade rows returned after filtering", ge=1, le=1000),
            ] = 500,
            include_raw_xml: Annotated[
                bool,
                "Include the raw Flex XML statement in the response. Avoid unless debugging.",
            ] = False,
            max_retries: Annotated[
                int,
                Field(description="Maximum GetStatement polling attempts", ge=1, le=20),
            ] = 10,
            retry_delay_seconds: Annotated[
                float,
                Field(description="Delay between GetStatement polling attempts", ge=0.5, le=10),
            ] = 2,
            timeout_seconds: Annotated[
                float,
                Field(description="HTTP timeout for each Flex Web Service request", ge=5, le=120),
            ] = 60,
        ) -> dict[str, Any]:
            token = os.getenv("IB_FLEX_TOKEN", "").strip()
            if not token:
                raise ValueError(
                    "IB_FLEX_TOKEN is not configured. Enable Flex Web Service in IBKR "
                    "Account Management, set IB_FLEX_TOKEN, and restart the MCP container."
                )

            period_env_names = {
                "default": "IB_FLEX_TRADE_QUERY_ID",
                "last_business_week": "IB_FLEX_TRADE_QUERY_ID_LAST_BUSINESS_WEEK",
                "ytd": "IB_FLEX_TRADE_QUERY_ID_YTD",
                "mtd": "IB_FLEX_TRADE_QUERY_ID_MTD",
            }
            selected_query_id = query_id.strip()
            selected_period = query_period
            selected_env_name = None
            if not selected_query_id:
                selected_env_name = period_env_names[selected_period]
                selected_query_id = os.getenv(selected_env_name, "").strip()
                if not selected_query_id and selected_period != "default":
                    selected_env_name = period_env_names["default"]
                    selected_query_id = os.getenv(selected_env_name, "").strip()
            if not selected_query_id:
                raise ValueError(
                    "No Flex Query ID provided. Pass query_id or set IB_FLEX_TRADE_QUERY_ID "
                    "to a Trade Confirmation / Activity Flex Query ID. Optional named env vars: "
                    "IB_FLEX_TRADE_QUERY_ID_LAST_BUSINESS_WEEK, IB_FLEX_TRADE_QUERY_ID_YTD, "
                    "IB_FLEX_TRADE_QUERY_ID_MTD."
                )

            start_at = _parse_date_or_datetime(start_date, "start")
            end_at = _parse_date_or_datetime(end_date, "end")
            if start_at > end_at:
                raise ValueError("start_date must be earlier than or equal to end_date")

            flex_response = await self._execute_flex_query(
                token,
                selected_query_id,
                max_retries,
                retry_delay_seconds,
                timeout_seconds,
            )
            if flex_response.get("error"):
                return {
                    "success": False,
                    "source": "ibkr_flex",
                    "query_id": selected_query_id,
                    "error": flex_response.get("error"),
                    "error_code": flex_response.get("error_code"),
                    "reference_code": flex_response.get("reference_code"),
                }

            statement_text = str(flex_response.get("data") or "")
            statement_format = str(flex_response.get("format") or "").lower()
            if statement_format == "csv" or not statement_text.lstrip().startswith("<"):
                statement, trades = self._parse_flex_csv_trades(
                    statement_text,
                    start_at,
                    end_at,
                    account,
                    symbol,
                    sec_type,
                    side,
                )
                statement_format = "csv"
            else:
                statement, trades = self._parse_flex_trades(
                    statement_text,
                    start_at,
                    end_at,
                    account,
                    symbol,
                    sec_type,
                    side,
                )
                statement_format = "xml"
            truncated = len(trades) > max_results

            response: dict[str, Any] = {
                "success": True,
                "source": "ibkr_flex",
                "format": statement_format,
                "readonly": True,
                "query_id": selected_query_id,
                "query_period": selected_period,
                "query_env_name": selected_env_name,
                "reference_code": flex_response.get("reference_code"),
                "poll_attempts": flex_response.get("attempts"),
                "requested_window": {
                    "start": start_at.isoformat(),
                    "end": end_at.isoformat(),
                },
                "filters": {
                    "account": account or None,
                    "symbol": symbol.upper().strip() or None,
                    "sec_type": sec_type.upper().strip() or None,
                    "side": side or None,
                },
                "statement": statement,
                "returned_trades": min(len(trades), max_results),
                "available_trades_after_filter": len(trades),
                "truncated": truncated,
                "trades": trades[:max_results],
                "notes": [
                    "This tool uses IBKR Flex Web Service, not the TWS reqExecutions endpoint.",
                    "The Flex Query template in IBKR Account Management controls the actual report period and fields returned.",
                    "The requested start/end dates are applied by this MCP after the Flex statement is returned.",
                ],
            }
            if include_raw_xml:
                response["raw_statement"] = statement_text
            return response

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

        @self.mcp.tool(
            description=(
                "Check whether any earnings announcement falls within a given date window "
                "for a list of tickers. Uses the IBKR Wall Street Horizon calendar. "
                "Returns a PASS/WARN/UNAVAILABLE verdict per ticker."
            )
        )
        async def get_earnings_check(
            tickers: Annotated[list[str], "Stock tickers to check, e.g. ['AAPL', 'MSFT']"],
            window_start: Annotated[
                str,
                "Start of the window to check, as YYYY-MM-DD (usually today).",
            ],
            window_end: Annotated[
                str,
                "End of the window to check, as YYYY-MM-DD (usually the option expiry date).",
            ],
            exchange: Annotated[str, "Stock routing exchange"] = "SMART",
            currency: Annotated[str, "Contract currency"] = "USD",
            primary_exchange: Annotated[str, "Primary exchange hint, usually empty"] = "",
        ) -> dict[str, Any]:
            import json as _json

            await self._ensure_connected()
            await self._ensure_wsh_metadata()

            start_ib = window_start.replace("-", "")
            end_ib = window_end.replace("-", "")

            results: dict[str, Any] = {}

            for raw_symbol in tickers:
                symbol = raw_symbol.upper().strip()
                if not symbol:
                    continue
                try:
                    stock = await self._stock_contract(symbol, exchange, currency, primary_exchange)
                    con_id = stock.conId
                    if not con_id:
                        raise ValueError("Could not resolve conId for stock")

                    # WSH filter: earnings only (wshe_ed), for this specific conId
                    filter_json = _json.dumps({
                        "watchlist": [str(con_id)],
                        "wshe_ed": "true",
                    })
                    wsh_data = ib.WshEventData(
                        filter=filter_json,
                        startDate=start_ib,
                        endDate=end_ib,
                        totalLimit=5,
                    )

                    raw_result = None
                    if hasattr(self.ib, "reqWshEventDataAsync"):
                        raw_result = await self.ib.reqWshEventDataAsync(wsh_data)
                    else:
                        raw_result = await _maybe_await(self.ib.reqWshEventData(wsh_data))

                    # Parse the returned JSON string from WSH
                    earnings_events: list[dict[str, Any]] = []
                    if raw_result:
                        try:
                            parsed = _json.loads(raw_result) if isinstance(raw_result, str) else raw_result
                            if isinstance(parsed, dict):
                                earnings_events = parsed.get("data", [])
                            elif isinstance(parsed, list):
                                earnings_events = parsed
                        except Exception:
                            pass

                    if not earnings_events:
                        results[symbol] = {
                            "verdict": "PASS",
                            "message": f"No earnings found between {window_start} and {window_end}.",
                            "earnings_date": None,
                            "con_id": con_id,
                        }
                    else:
                        # Pick the earliest event date in the window
                        earliest = min(
                            earnings_events,
                            key=lambda e: str(e.get("date", e.get("startDate", ""))),
                        )
                        earnings_date = earliest.get("date") or earliest.get("startDate") or "unknown"
                        results[symbol] = {
                            "verdict": "WARN",
                            "message": (
                                f"⚠️ Earnings detected on {earnings_date} — inside your trade window "
                                f"({window_start} → {window_end}). Review before staging the order."
                            ),
                            "earnings_date": earnings_date,
                            "con_id": con_id,
                            "raw_events": earnings_events,
                        }

                except Exception as exc:
                    logger.warning("Earnings check failed for %s: %s", symbol, exc)
                    results[symbol] = {
                        "verdict": "UNAVAILABLE",
                        "message": (
                            f"Could not retrieve earnings data for {symbol}. "
                            "Verify your Wall Street Horizon subscription is active in IBKR Account Management. "
                            f"Error: {exc}"
                        ),
                        "earnings_date": None,
                    }

            return {
                "window_start": window_start,
                "window_end": window_end,
                "results": results,
            }

        @self.mcp.tool(
            description="Preview a cash-secured put limit order without placing it in TWS."
        )
        async def preview_cash_secured_put_order(
            ticker: Annotated[str, "Stock ticker, for example AAPL"],
            expiry: Annotated[str, "Target expiry as YYYY-MM-DD"],
            strike: Annotated[float, "Strike price"],
            quantity: Annotated[int, "Number of contracts"],
            limit_price: Annotated[float, "Limit price for the premium"],
            confirm_stage_only: Annotated[bool, "Must be true to confirm this is a stage-only order"],
            account: Annotated[str, "Optional IBKR account id"] = "",
        ) -> dict[str, Any]:
            if not confirm_stage_only:
                raise ValueError("confirm_stage_only must be true")
            if quantity <= 0:
                raise ValueError("Quantity must be greater than 0")
            if limit_price <= 0:
                raise ValueError("Limit price must be greater than 0")

            await self._ensure_connected()

            expiry_ib = expiry.replace("-", "")
            contract = ib.Option(
                symbol=ticker.upper(),
                lastTradeDateOrContractMonth=expiry_ib,
                strike=strike,
                right="P",
                exchange="SMART",
                currency="USD"
            )
            qualified_raw = await self.ib.qualifyContractsAsync(contract)
            qualified = [c for c in qualified_raw if c is not None]
            if not qualified:
                raise ValueError("IBKR did not qualify the option contract. Check the ticker, strike, and expiry.")

            capital_required = strike * 100 * quantity
            premium = limit_price * 100 * quantity
            effective_entry = strike - limit_price

            return {
                "status": "preview_successful",
                "ticker": ticker.upper(),
                "expiry": expiry,
                "strike": strike,
                "right": "PUT",
                "action": "SELL",
                "quantity": quantity,
                "limit_price": limit_price,
                "premium": premium,
                "capital_required": capital_required,
                "effective_entry": effective_entry,
                "transmit": False,
                "message": "Preview successful. Ready to stage."
            }

        @self.mcp.tool(
            description="Create a staged/draft SELL PUT limit order in TWS so I can review it manually and click Transmit myself."
        )
        async def create_draft_cash_secured_put_order(
            ticker: Annotated[str, "Stock ticker, for example AAPL"],
            expiry: Annotated[str, "Target expiry as YYYY-MM-DD"],
            strike: Annotated[float, "Strike price"],
            quantity: Annotated[int, "Number of contracts"],
            limit_price: Annotated[float, "Limit price for the premium"],
            confirm_stage_only: Annotated[bool, "Must be true to confirm this is a stage-only order"],
            account: Annotated[str, "Optional IBKR account id"] = "",
        ) -> dict[str, Any]:
            if not confirm_stage_only:
                raise ValueError("confirm_stage_only must be true")
            if quantity <= 0:
                raise ValueError("Quantity must be greater than 0")
            if limit_price <= 0:
                raise ValueError("Limit price must be greater than 0")

            await self._ensure_connected()

            expiry_ib = expiry.replace("-", "")
            contract = ib.Option(
                symbol=ticker.upper(),
                lastTradeDateOrContractMonth=expiry_ib,
                strike=strike,
                right="P",
                exchange="SMART",
                currency="USD"
            )
            qualified_raw = await self.ib.qualifyContractsAsync(contract)
            qualified = [c for c in qualified_raw if c is not None]
            if not qualified:
                raise ValueError("IBKR did not qualify the option contract. Check the ticker, strike, and expiry.")
            
            qualified_contract = qualified[0]

            capital_required = strike * 100 * quantity
            premium = limit_price * 100 * quantity
            effective_entry = strike - limit_price

            order = ib.LimitOrder(
                action="SELL",
                totalQuantity=quantity,
                lmtPrice=limit_price,
            )
            order.transmit = False
            if account:
                order.account = account

            trade = self.ib.placeOrder(qualified_contract, order)

            return {
                "status": "draft_created",
                "ticker": ticker.upper(),
                "expiry": expiry,
                "strike": strike,
                "right": "PUT",
                "action": "SELL",
                "quantity": quantity,
                "limit_price": limit_price,
                "premium": premium,
                "capital_required": capital_required,
                "effective_entry": effective_entry,
                "transmit": False,
                "ib_order_id": str(trade.order.orderId) if trade.order else None,
                "message": "Draft order created in TWS. Review manually and click Transmit if happy."
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
