from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
import time
from typing import Callable


class ProviderError(RuntimeError):
    pass


class AkshareProvider:
    name = "akshare-multi-source"

    def __init__(self, akshare_module: object | None = None) -> None:
        if akshare_module is None:
            try:
                import akshare
            except ImportError as exc:
                raise ProviderError("未安装 AKShare；请安装项目的 data 可选依赖") from exc
            akshare_module = akshare
        self._akshare = akshare_module
        self.warnings: list[str] = []
        self.sources: dict[str, str] = {}
        self.risk_warning_complete = True
        self.suspension_complete = True
        self.trading_calendar_complete = True

    def fetch_stock_spot(self) -> list[dict[str, object]]:
        return self._first_available(
            "stock_spot",
            (
                ("sina", self._akshare.stock_zh_a_spot),
                ("eastmoney", self._akshare.stock_zh_a_spot_em),
            ),
        )

    def fetch_etf_spot(self) -> list[dict[str, object]]:
        return self._first_available(
            "etf_spot",
            (
                ("sina", lambda: self._akshare.fund_etf_category_sina(symbol="ETF基金")),
                ("eastmoney", self._akshare.fund_etf_spot_em),
            ),
        )

    def fetch_index_spot(self) -> list[dict[str, object]]:
        return self._first_available(
            "index_spot",
            (
                ("sina", self._akshare.stock_zh_index_spot_sina),
                (
                    "eastmoney",
                    lambda: self._akshare.stock_zh_index_spot_em(
                        symbol="沪深重要指数"
                    ),
                ),
            ),
            require_symbol=False,
        )

    def fetch_risk_warning(self) -> list[dict[str, object]]:
        try:
            records = self._records("stock_zh_a_st_em", self._akshare.stock_zh_a_st_em)
            self.sources["risk_warning"] = "eastmoney"
            return records
        except ProviderError as exc:
            self.risk_warning_complete = False
            self.sources["risk_warning"] = "unavailable"
            self.warnings.append(f"风险警示名单不可用: {exc}")
            return []

    def fetch_suspensions(self, trade_date: date) -> list[dict[str, object]]:
        try:
            records = self._records(
                "stock_tfp_em",
                lambda: self._akshare.stock_tfp_em(date=trade_date.strftime("%Y%m%d")),
            )
            self.sources["suspensions"] = "eastmoney"
            return records
        except ProviderError as exc:
            self.suspension_complete = False
            self.sources["suspensions"] = "unavailable"
            self.warnings.append(f"停复牌名单不可用: {exc}")
            return []

    def fetch_trading_calendar(self) -> list[dict[str, object]]:
        from .exchange_calendar import verified_exchange_calendar
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        records, evidence = verified_exchange_calendar(now)
        if records:
            self.trading_calendar_complete = True
            self.trading_calendar_evidence = evidence
            self.sources["trading_calendar"] = "sse_verified_annual_calendar"
            return records
        try:
            records = self._records(
                "tool_trade_date_hist_sina",
                self._akshare.tool_trade_date_hist_sina,
                require_symbol=False,
            )
            self.sources["trading_calendar"] = "sina"
            self.trading_calendar_complete = bool(records)
            return records
        except ProviderError as exc:
            self.trading_calendar_complete = False
            self.sources["trading_calendar"] = "unavailable"
            self.warnings.append(f"交易日历不可用: {exc}")
            return []

    def fetch_daily_history(
        self,
        symbol: str,
        exchange_prefix: str,
        security_type: str,
        start_date: date,
        end_date: date,
        adjustment: str,
    ) -> list[dict[str, object]]:
        provider_adjustment = "" if adjustment == "raw" else adjustment
        purpose = f"history:{symbol}:{adjustment}"
        if security_type == "etf":
            candidates: tuple[tuple[str, Callable[[], object]], ...] = (
                (
                    "sina",
                    lambda: self._akshare.fund_etf_hist_sina(
                        symbol=f"{exchange_prefix}{symbol}"
                    ),
                ),
                (
                    "eastmoney",
                    lambda: self._akshare.fund_etf_hist_em(
                        symbol=symbol,
                        period="daily",
                        start_date=start_date.strftime("%Y%m%d"),
                        end_date=end_date.strftime("%Y%m%d"),
                        adjust=provider_adjustment,
                    ),
                ),
            )
            if adjustment != "raw":
                candidates = candidates[1:]
            try:
                return self._first_available(purpose, candidates, require_symbol=False, attempts_per_source=2)
            except ProviderError:
                if adjustment != "qfq":
                    raise
                from .tencent_history import fetch_etf_qfq
                rows = fetch_etf_qfq(symbol, exchange_prefix, start_date, end_date)
                if not rows:
                    raise ProviderError(f"{purpose}: Tencent前复权请求区间为空")
                self.sources[purpose] = "tencent_explicit_qfqday"
                self.warnings.append(f"{purpose}: Eastmoney不可用，已使用Tencent明确qfqday整段序列")
                return rows
        return self._first_available(
            purpose,
            (
                (
                    "sina",
                    lambda: self._akshare.stock_zh_a_daily(
                        symbol=f"{exchange_prefix}{symbol}",
                        start_date=start_date.strftime("%Y%m%d"),
                        end_date=end_date.strftime("%Y%m%d"),
                        adjust=provider_adjustment,
                    ),
                ),
                (
                    "eastmoney",
                    lambda: self._akshare.stock_zh_a_hist(
                        symbol=symbol,
                        period="daily",
                        start_date=start_date.strftime("%Y%m%d"),
                        end_date=end_date.strftime("%Y%m%d"),
                        adjust=provider_adjustment,
                    ),
                ),
            ),
            require_symbol=False,
            attempts_per_source=2,
        )

    def fetch_dividends(self, symbol: str) -> list[dict[str, object]]:
        purpose = f"dividends:{symbol}"
        return self._first_available(
            purpose,
            (
                (
                    "cninfo",
                    lambda: self._akshare.stock_dividend_cninfo(symbol=symbol),
                ),
                (
                    "eastmoney",
                    lambda: self._akshare.stock_fhps_detail_em(symbol=symbol),
                ),
                (
                    "sina",
                    lambda: self._akshare.stock_history_dividend_detail(
                        symbol=symbol, indicator="分红"
                    ),
                ),
            ),
            require_symbol=False,
            allow_empty=True,
        )

    def fetch_industry_changes(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, object]]:
        purpose = f"industry:{symbol}"
        records = self._records(
            purpose,
            lambda: self._akshare.stock_industry_change_cninfo(
                symbol=symbol,
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            ),
            require_symbol=False,
        )
        self.sources[purpose] = "cninfo"
        return records

    def _first_available(
        self,
        purpose: str,
        candidates: tuple[tuple[str, Callable[[], object]], ...],
        require_symbol: bool = True,
        allow_empty: bool = False,
        attempts_per_source: int = 1,
    ) -> list[dict[str, object]]:
        failures: list[str] = []
        for source, loader in candidates:
            for attempt in range(attempts_per_source):
                try:
                    records = self._records(
                        f"{purpose}:{source}", loader, require_symbol=require_symbol
                    )
                    if not records and not allow_empty:
                        raise ProviderError(f"{purpose}:{source} 返回空数据")
                    self.sources[purpose] = source
                    if failures:
                        self.warnings.append(
                            f"{purpose} 已降级或重试成功到 {source}; "
                            f"失败尝试: {'; '.join(failures)}"
                        )
                    return records
                except ProviderError as exc:
                    failures.append(str(exc))
                    if attempt + 1 < attempts_per_source:
                        time.sleep(0.35)
        raise ProviderError(f"{purpose} 所有来源均失败: {'; '.join(failures)}")

    @staticmethod
    def _records(
        name: str,
        loader: Callable[[], object],
        require_symbol: bool = True,
    ) -> list[dict[str, object]]:
        try:
            frame = loader()
            columns = [str(item) for item in frame.columns]
            records = frame.to_dict(orient="records")
        except Exception as exc:
            raise ProviderError(f"{name} 调用失败: {exc}") from exc
        if not isinstance(records, list):
            raise ProviderError(f"{name} 返回类型异常")
        if (
            require_symbol
            and records
            and not ({"代码", "股票代码", "symbol", "code"} & set(columns))
        ):
            raise ProviderError(f"{name} 缺少证券代码列，实际列: {columns}")
        return records
