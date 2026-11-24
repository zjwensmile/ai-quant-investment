from __future__ import annotations

import math
from datetime import date, datetime
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .market_store import CorporateAction, DailyBar, MarketStore
from .securities import Exchange, SecurityType, normalize_symbol


class HistoryProvider(Protocol):
    name: str
    sources: dict[str, str]

    def fetch_daily_history(
        self,
        symbol: str,
        exchange_prefix: str,
        security_type: str,
        start_date: date,
        end_date: date,
        adjustment: str,
    ) -> Sequence[dict[str, object]]: ...

    def fetch_dividends(self, symbol: str) -> Sequence[dict[str, object]]: ...


class HistorySyncService:
    def __init__(self, provider: HistoryProvider, store: MarketStore) -> None:
        self.provider = provider
        self.store = store

    def sync_symbol(
        self,
        symbol: str,
        exchange: Exchange,
        security_type: SecurityType,
        start_date: date,
        end_date: date,
        include_qfq: bool = True,
        include_dividends: bool = True,
    ) -> dict[str, object]:
        code = normalize_symbol(symbol)
        prefix = "sh" if exchange is Exchange.SSE else "sz"
        available_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        raw_records = self.provider.fetch_daily_history(
            code, prefix, security_type.value, start_date, end_date, "raw"
        )
        raw_bars = _bars(code, raw_records, start_date, end_date)
        if not raw_bars:
            if self.store.latest_date(code) is not None:
                return {
                    "symbol": code,
                    "raw_bars": 0,
                    "qfq_bars": 0,
                    "corporate_actions": 0,
                    "available_at": available_at,
                    "warnings": ["请求区间暂时没有新增日线，已保留现有历史"],
                }
            raise ValueError(
                f"{code} 在 {start_date.isoformat()} 至 {end_date.isoformat()} 没有可用原始日线"
            )
        raw_count = self.store.upsert_bars(
            raw_bars,
            "raw",
            self.provider.sources.get(f"history:{code}:raw", self.provider.name),
            available_at,
        )

        warnings: list[str] = []
        qfq_count = 0
        if include_qfq and security_type is SecurityType.STOCK:
            try:
                qfq_records = self.provider.fetch_daily_history(
                    code, prefix, security_type.value, start_date, end_date, "qfq"
                )
                qfq_count = self.store.upsert_bars(
                    _bars(code, qfq_records, start_date, end_date),
                    "qfq",
                    self.provider.sources.get(f"history:{code}:qfq", self.provider.name),
                    available_at,
                )
            except Exception as exc:
                warnings.append(f"前复权展示数据不可用: {exc}")

        action_count = 0
        if include_dividends and security_type is SecurityType.STOCK:
            try:
                records = self.provider.fetch_dividends(code)
                action_count = self.store.upsert_actions(
                    _actions(code, records),
                    self.provider.sources.get(f"dividends:{code}", self.provider.name),
                    available_at,
                )
            except Exception as exc:
                warnings.append(f"分红数据不可用: {exc}")
        return {
            "symbol": code,
            "raw_bars": raw_count,
            "qfq_bars": qfq_count,
            "corporate_actions": action_count,
            "available_at": available_at,
            "warnings": warnings,
        }


def _bars(
    symbol: str,
    rows: Sequence[dict[str, object]],
    start_date: date,
    end_date: date,
) -> list[DailyBar]:
    result: list[DailyBar] = []
    for row in rows:
        trade_date = _as_date(_first(row, "date", "日期"))
        if trade_date is None or trade_date < start_date or trade_date > end_date:
            continue
        values = [_number(_first(row, name, chinese)) for name, chinese in (
            ("open", "开盘"),
            ("high", "最高"),
            ("low", "最低"),
            ("close", "收盘"),
        )]
        if any(value is None for value in values):
            continue
        result.append(
            DailyBar(
                symbol=symbol,
                trade_date=trade_date,
                open=values[0],
                high=values[1],
                low=values[2],
                close=values[3],
                volume=_number(_first(row, "volume", "成交量")),
                amount=_number(_first(row, "amount", "成交额")),
                outstanding_share=_number(_first(row, "outstanding_share", "流通股本")),
                turnover=_number(_first(row, "turnover", "换手率")),
            )
        )
    return result


def _actions(symbol: str, rows: Sequence[dict[str, object]]) -> list[CorporateAction]:
    return [
        CorporateAction(
            symbol=symbol,
            announcement_date=_as_date(
                _first(row, "公告日期", "实施方案公告日期", "最新公告日期", "预案公告日")
            ),
            ex_date=_as_date(_first(row, "除权除息日", "除权日")),
            record_date=_as_date(_first(row, "股权登记日")),
            cash_dividend_per_10=_number(
                _first(row, "派息", "派息比例", "现金分红-现金分红比例")
            ),
            bonus_shares_per_10=_number(
                _first(row, "送股", "送股比例", "送转股份-送股比例")
            ),
            transfer_shares_per_10=_number(
                _first(row, "转增", "转增比例", "送转股份-转股比例")
            ),
            status=str(_first(row, "进度", "方案进度", "分红类型", default="")) or None,
            raw=dict(row),
        )
        for row in rows
    ]


def _first(row: dict[str, object], *names: str, default: object = None) -> object:
    for name in names:
        if name in row:
            return row[name]
    return default


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or str(value) in {"", "NaT", "None"}:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
