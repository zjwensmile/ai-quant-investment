from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .execution import transaction_fees
from .features import (
    FeatureEngine,
    _defensive_candidates,
    _quality_value_candidates,
    _tactical_candidates,
)
from .market_store import MarketStore
from .rules import Side
from .securities import SecurityType


SOURCES = ("QUALITY_VALUE", "DEFENSIVE", "TACTICAL")
VERSION = "diversified-source-pit-backtest-v1"


class SourceBacktester:
    """Point-in-time source comparison with next-open fills and trading friction."""

    def __init__(
        self,
        store: MarketStore,
        *,
        initial_cash: float = 50_000,
        rebalance_sessions: int = 20,
        maximum_positions: int = 5,
        slippage_bps: float = 5,
        commission_fixed_per_order: Decimal = Decimal("1"),
        stock_sell_stamp_duty_rate: Decimal = Decimal("0.0005"),
    ) -> None:
        self.store = store
        self.initial_cash = initial_cash
        self.rebalance_sessions = rebalance_sessions
        self.maximum_positions = maximum_positions
        self.slippage_bps = slippage_bps
        self.commission = commission_fixed_per_order
        self.stamp_duty = stock_sell_stamp_duty_rate

    def run(self, start: date, end: date) -> dict[str, Any]:
        dates = self.store.trading_dates(start, end)
        securities = self._securities()
        trades = {source: [] for source in SOURCES}
        observations = {source: 0 for source in SOURCES}
        pit_history_sufficient = self._pit_history_sufficient(dates, securities)
        if len(dates) > self.rebalance_sessions + 1 and securities and pit_history_sufficient:
            for index in range(120, len(dates) - self.rebalance_sessions - 1,
                               self.rebalance_sessions):
                signal_date = dates[index]
                candidates = self._candidates(signal_date, securities)
                for source in SOURCES:
                    observations[source] += 1
                    for item in candidates[source][: self.maximum_positions]:
                        trade = self._trade(
                            item, source, dates, index + 1,
                            min(index + 1 + self.rebalance_sessions, len(dates) - 1),
                        )
                        trades[source].append(trade)
        summaries = {
            source: _summary(
                trades[source], observations[source], self.initial_cash,
                self.maximum_positions,
            )
            for source in SOURCES
        }
        usable = any(value["filled_trades"] > 0 for value in summaries.values())
        return {
            "version": VERSION,
            "status": "completed" if usable else "insufficient_point_in_time_history",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "initial_cash": self.initial_cash,
            "sources": summaries,
            "method": {
                "selection_data": "only revisions available by each signal timestamp",
                "financial_data": "latest report announced and locally observed by signal timestamp",
                "execution": "next-session open; locked limit-up buys and limit-down sells unfilled",
                "slippage_bps_each_side": self.slippage_bps,
                "commission_fixed_per_order": float(self.commission),
                "stock_sell_stamp_duty_rate": float(self.stamp_duty),
                "rebalance_sessions": self.rebalance_sessions,
                "maximum_positions": self.maximum_positions,
                "survivorship_note": (
                    "Security master is included only when its record was locally available by "
                    "the signal timestamp; delisted history absent from local master remains a limitation."
                ),
                "point_in_time_history_preflight": pit_history_sufficient,
            },
        }

    def _pit_history_sufficient(
        self, dates: list[date], securities: list[dict[str, Any]]
    ) -> bool:
        if len(dates) <= self.rebalance_sessions + 1 or not securities:
            return False
        latest_viable = dates[-self.rebalance_sessions - 1]
        known_at = datetime.combine(
            latest_viable, time(23, 59, 59), ZoneInfo("Asia/Shanghai")
        ).isoformat()
        minimum_symbols = max(min(len(securities), 100), 1)
        return self.store.point_in_time_bar_revision_count(
            latest_viable, known_at
        ) >= minimum_symbols * 121

    def _securities(self) -> list[dict[str, Any]]:
        result = []
        for row in self.store.security_master_rows(active_only=False):
            raw = dict(row.get("raw") or {})
            raw.update({
                "symbol": str(row["symbol"]), "name": str(row["name"]),
                "security_type": str(row["security_type"]),
                "trading_status": str(row["trading_status"]),
                "risk_warning": None if row["risk_warning"] is None else bool(row["risk_warning"]),
                "buy_lot_size": int(row["buy_lot_size"]),
                "price_limit_ratio": row["price_limit_ratio"],
                "available_at": row["available_at"],
            })
            result.append(raw)
        return result

    def _candidates(
        self, signal_date: date, securities: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        known_at = datetime.combine(
            signal_date, time(23, 59, 59), ZoneInfo("Asia/Shanghai")
        ).isoformat()
        fundamentals = self.store.fundamental_map_as_known(signal_date, known_at)
        available = [
            item for item in securities
            if not item.get("available_at") or str(item["available_at"]) <= known_at
        ]
        quotes: dict[str, dict[str, Any]] = {}
        for item in available:
            bars = self.store.bars_as_known(item["symbol"], 1, signal_date, known_at)
            if not bars:
                continue
            last = float(bars[-1]["close"])
            quote: dict[str, Any] = {"last": last, "available_at": known_at}
            fundamental = fundamentals.get(item["symbol"])
            if fundamental:
                eps = fundamental.get("earnings_per_share")
                bvps = fundamental.get("book_value_per_share")
                month = date.fromisoformat(str(fundamental["report_period"])).month
                annual_eps = float(eps) * 12 / month if eps and month else 0
                quote["pe_ratio"] = last / annual_eps if annual_eps > 0 else None
                quote["pb_ratio"] = last / float(bvps) if bvps and float(bvps) > 0 else None
            quotes[item["symbol"]] = quote
        snapshot = {
            "snapshot_id": f"backtest-{signal_date.isoformat()}",
            "as_of_time": known_at,
            "account": {"capital_budget": self.initial_cash, "cash": self.initial_cash,
                        "positions": []},
            "market": {"session": "post_market", "is_trading_day": False},
            "events": [], "securities": available, "quotes": quotes,
            "data_quality": {"status": "backtest"},
        }
        enriched = FeatureEngine(self.store).enrich(snapshot)
        # This report intentionally preserves the three legacy deterministic
        # sources so historical comparisons remain reproducible.  They are no
        # longer used to form the live Agent research pool.
        universe = enriched.get("research_universe", [])
        limit = FeatureEngine(self.store).top_n
        return {
            "QUALITY_VALUE": _quality_value_candidates(universe, limit),
            "DEFENSIVE": _defensive_candidates(universe, limit),
            "TACTICAL": _tactical_candidates(universe, limit),
        }

    def _trade(
        self, candidate: dict[str, Any], source: str, dates: list[date],
        entry_index: int, exit_index: int,
    ) -> dict[str, Any]:
        symbol = candidate["symbol"]
        bars = self.store.bars_between(symbol, dates[entry_index - 1], dates[exit_index])
        by_date = {date.fromisoformat(str(item["trade_date"])): item for item in bars}
        previous = by_date.get(dates[entry_index - 1])
        entry = by_date.get(dates[entry_index])
        exit_bar = by_date.get(dates[exit_index])
        base = {"source": source, "symbol": symbol,
                "signal_date": dates[entry_index - 1].isoformat(),
                "entry_date": dates[entry_index].isoformat(),
                "exit_date": dates[exit_index].isoformat(), "status": "unfilled"}
        if not previous or not entry or not exit_bar:
            return {**base, "reason": "missing_bar"}
        limit = float(candidate.get("security", {}).get("price_limit_ratio") or 0.10)
        if _locked(entry, float(previous["close"]) * (1 + limit), upward=True):
            return {**base, "reason": "locked_limit_up"}
        security_type = SecurityType(candidate["security_type"])
        lot = int(candidate.get("security", {}).get("buy_lot_size") or 100)
        allocation = self.initial_cash / self.maximum_positions
        entry_price = float(entry["open"]) * (1 + self.slippage_bps / 10_000)
        quantity = math.floor(max(allocation - float(self.commission), 0) / (entry_price * lot)) * lot
        if quantity <= 0:
            return {**base, "reason": "lot_too_large"}
        previous_exit = by_date.get(dates[exit_index - 1])
        if previous_exit and _locked(
            exit_bar, float(previous_exit["close"]) * (1 - limit), upward=False
        ):
            return {**base, "reason": "locked_limit_down_at_exit"}
        exit_price = float(exit_bar["open"]) * (1 - self.slippage_bps / 10_000)
        buy_notional = Decimal(str(entry_price * quantity))
        sell_notional = Decimal(str(exit_price * quantity))
        buy_fee = transaction_fees(
            security_type, Side.BUY, buy_notional,
            commission_fixed_per_order=self.commission,
            stock_sell_stamp_duty_rate=self.stamp_duty,
        )["total"]
        sell_fee = transaction_fees(
            security_type, Side.SELL, sell_notional,
            commission_fixed_per_order=self.commission,
            stock_sell_stamp_duty_rate=self.stamp_duty,
        )["total"]
        cost = float(buy_notional + buy_fee)
        proceeds = float(sell_notional - sell_fee)
        return {**base, "status": "filled", "quantity": quantity,
                "entry_price": entry_price, "exit_price": exit_price,
                "gross_return": exit_price / entry_price - 1,
                "net_return": proceeds / cost - 1,
                "fees": float(buy_fee + sell_fee),
                "turnover_notional": float(buy_notional + sell_notional)}


def _locked(bar: dict[str, object], limit_price: float, *, upward: bool) -> bool:
    values = [float(bar[key]) for key in ("open", "high", "low", "close")]
    tolerance = max(limit_price * 0.001, 0.001)
    return all(value >= limit_price - tolerance for value in values) if upward else all(
        value <= limit_price + tolerance for value in values
    )


def _summary(
    trades: list[dict[str, Any]], observations: int, initial_cash: float,
    maximum_positions: int,
) -> dict[str, Any]:
    filled = [item for item in trades if item["status"] == "filled"]
    returns = [float(item["net_return"]) / maximum_positions for item in filled]
    equity = peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    return {
        "observations": observations,
        "signals": len(trades),
        "filled_trades": len(filled),
        "unfilled_trades": len(trades) - len(filled),
        "hit_rate": (
            sum(float(item["net_return"]) > 0 for item in filled) / len(filled)
            if filled else None
        ),
        "mean_net_return_per_trade": (
            sum(float(item["net_return"]) for item in filled) / len(filled)
            if filled else None
        ),
        "portfolio_compounded_return": equity - 1 if filled else None,
        "maximum_drawdown": drawdown if filled else None,
        "turnover_ratio": (
            sum(float(item["turnover_notional"]) for item in filled) / initial_cash
            if filled else 0.0
        ),
        "fees": sum(float(item["fees"]) for item in filled),
        "unfilled_reasons": _counts(
            str(item.get("reason") or "unknown") for item in trades
            if item["status"] != "filled"
        ),
        "trades": trades,
    }


def _counts(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result
