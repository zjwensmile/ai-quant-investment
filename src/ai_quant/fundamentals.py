from __future__ import annotations

import math
from datetime import date, datetime
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .market_store import FundamentalSnapshot, MarketStore
from .securities import classify_research_stock, normalize_symbol


SOURCE_URL = "https://data.eastmoney.com/bbsj/"


class FinancialReportProvider(Protocol):
    def fetch_financial_reports(self, report_period: date) -> Sequence[dict[str, object]]: ...


class AkshareFinancialReportProvider:
    def __init__(self) -> None:
        import akshare as ak

        self._akshare = ak

    def fetch_financial_reports(self, report_period: date) -> list[dict[str, object]]:
        frame = self._akshare.stock_yjbb_em(date=report_period.strftime("%Y%m%d"))
        return frame.to_dict(orient="records")


class FundamentalSyncService:
    """Stores bulk financial reports with announcement and observation timestamps."""

    def __init__(self, provider: FinancialReportProvider, store: MarketStore) -> None:
        self.provider = provider
        self.store = store

    def sync(self, report_periods: Sequence[date]) -> dict[str, object]:
        available_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        )
        total = 0
        by_period: dict[str, int] = {}
        for period in report_periods:
            normalized = []
            for row in self.provider.fetch_financial_reports(period):
                item = _normalize_report(row, period)
                if item is not None:
                    normalized.append(item)
            count = self.store.upsert_fundamental_snapshots(
                normalized, "eastmoney_financial_report", available_at
            )
            by_period[period.isoformat()] = count
            total += count
        return {
            "available_at": available_at,
            "source_url": SOURCE_URL,
            "rows": total,
            "by_period": by_period,
            "point_in_time_policy": (
                "Only records whose report period, announcement date, and local "
                "observation timestamp are no later than the decision timestamp are used."
            ),
        }


def recent_completed_periods(as_of: date, count: int = 4) -> list[date]:
    quarter_ends = ((3, 31), (6, 30), (9, 30), (12, 31))
    values = [
        date(year, month, day)
        for year in range(as_of.year - 2, as_of.year + 1)
        for month, day in quarter_ends
        if date(year, month, day) <= as_of
    ]
    return list(reversed(values[-count:]))


def _normalize_report(
    row: dict[str, object], report_period: date
) -> FundamentalSnapshot | None:
    try:
        symbol = normalize_symbol(row.get("股票代码"))
    except ValueError:
        return None
    if classify_research_stock(symbol) is None:
        return None
    announcement = _date_value(row.get("最新公告日期"))
    if announcement is None or announcement < report_period:
        return None
    return FundamentalSnapshot(
        symbol=symbol,
        report_period=report_period,
        announcement_date=announcement,
        revenue=_number(row.get("营业总收入-营业总收入")),
        revenue_growth=_number(row.get("营业总收入-同比增长")),
        net_profit=_number(row.get("净利润-净利润")),
        net_profit_growth=_number(row.get("净利润-同比增长")),
        earnings_per_share=_number(row.get("每股收益")),
        book_value_per_share=_number(row.get("每股净资产")),
        operating_cash_flow_per_share=_number(row.get("每股经营现金流量")),
        return_on_equity=_number(row.get("净资产收益率")),
        gross_margin=_number(row.get("销售毛利率")),
        industry_name=str(row.get("所处行业") or "").strip() or None,
        source_url=SOURCE_URL + report_period.strftime("%Y%m") + "/yjbb.html",
        raw=row,
    )


def _date_value(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, ZoneInfo("Asia/Shanghai")).date()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if value is None or value in ("", "-"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
