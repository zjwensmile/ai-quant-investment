from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Exchange(StrEnum):
    SSE = "SSE"
    SZSE = "SZSE"


class SecurityType(StrEnum):
    STOCK = "stock"
    ETF = "etf"


class Board(StrEnum):
    MAIN = "main"
    STAR = "star"
    CHINEXT = "chinext"
    NOT_APPLICABLE = "not_applicable"


class TradingStatus(StrEnum):
    TRADABLE = "tradable"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    exchange: Exchange
    security_type: SecurityType
    board: Board
    risk_warning: bool | None
    trading_status: TradingStatus
    price_tick: Decimal
    buy_lot_size: int = 100
    same_day_turnaround: bool | None = None
    listing_date: date | None = None
    price_limit_ratio: Decimal | None = None
    price_limit_exempt: bool = False
    source: str = "unknown"
    available_at: str | None = None
    minimum_buy_quantity: int = 100

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["exchange"] = self.exchange.value
        value["security_type"] = self.security_type.value
        value["board"] = self.board.value
        value["trading_status"] = self.trading_status.value
        value["price_tick"] = str(self.price_tick)
        value["price_limit_ratio"] = (
            str(self.price_limit_ratio) if self.price_limit_ratio is not None else None
        )
        value["listing_date"] = self.listing_date.isoformat() if self.listing_date else None
        return value


_SSE_MAIN_PREFIXES = ("600", "601", "603", "605")
_SZSE_MAIN_PREFIXES = ("000", "001", "002", "003")


def classify_main_board_stock(symbol: str) -> tuple[Exchange, Board] | None:
    code = normalize_symbol(symbol)
    if code.startswith(_SSE_MAIN_PREFIXES):
        return Exchange.SSE, Board.MAIN
    if code.startswith(_SZSE_MAIN_PREFIXES):
        return Exchange.SZSE, Board.MAIN
    return None


def classify_research_stock(symbol: str) -> tuple[Exchange, Board] | None:
    """Identify research coverage; this does not grant trading permission."""
    code = normalize_symbol(symbol)
    main = classify_main_board_stock(code)
    if main is not None:
        return main
    if code.startswith("688"):
        return Exchange.SSE, Board.STAR
    if code.startswith(("300", "301")):
        return Exchange.SZSE, Board.CHINEXT
    return None


def classify_etf_exchange(symbol: str) -> Exchange:
    code = normalize_symbol(symbol)
    if code.startswith("5"):
        return Exchange.SSE
    if code.startswith("1"):
        return Exchange.SZSE
    raise ValueError(f"无法根据 ETF 代码识别交易所: {symbol}")


def normalize_symbol(symbol: object) -> str:
    text = str(symbol).strip()
    lowered = text.lower()
    if lowered[:2] in {"sh", "sz", "bj"} and lowered[2:].isdigit():
        text = text[2:]
    elif "." in text:
        code, suffix = text.split(".", maxsplit=1)
        if code.isdigit() and suffix.lower() in {"sh", "sz", "sse", "szse"}:
            text = code
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit():
        raise ValueError(f"证券代码必须是数字: {symbol}")
    return text.zfill(6)
