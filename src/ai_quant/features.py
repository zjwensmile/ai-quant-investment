from __future__ import annotations

import copy
import math
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from .chanlun import (
    POLICY_VERSION,
    ChanlunCandidate,
    MarketBar,
    RustChanlunBridge,
    confirm_recent_scans,
    select_candidates,
)
from .market_store import MarketStore
from .securities import normalize_symbol


class FeatureEngine:
    """Deterministic feature and candidate generation; no investment approval."""

    def __init__(
        self,
        store: MarketStore,
        top_n: int = 20,
        *,
        chanlun_bridge: RustChanlunBridge | None = None,
        chanlun_mode: str = "shadow",
        chanlun_reference_passed: bool = False,
        chanlun_min_bars: int = 250,
        chanlun_batch_size: int = 100,
    ) -> None:
        if chanlun_mode not in {"disabled", "shadow", "active"}:
            raise ValueError("chanlun_mode must be disabled, shadow, or active")
        self.store = store
        self.top_n = top_n
        self.chanlun_bridge = chanlun_bridge
        self.chanlun_requested_mode = chanlun_mode
        self.chanlun_reference_passed = chanlun_reference_passed
        self.chanlun_mode = chanlun_mode
        self.chanlun_min_bars = chanlun_min_bars
        self.chanlun_batch_size = chanlun_batch_size

    def enrich(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(snapshot)
        securities = {item["symbol"]: item for item in result.get("securities", [])}
        positions = {
            normalize_symbol(item["symbol"]): item
            for item in result.get("account", {}).get("positions", [])
            if item.get("symbol")
        }
        available_symbols = set(self.store.symbols())
        daily_cutoff = _daily_bar_cutoff(result)
        industry_map = self.store.primary_industry_map(
            daily_cutoff, result["as_of_time"]
        )
        fundamental_map = self.store.fundamental_map_as_known(
            daily_cutoff, result["as_of_time"]
        )
        rows: list[dict[str, Any]] = []
        post_market_quote_overlays = 0
        chanlun_batch: dict[str, list[MarketBar]] = {}
        chanlun_found: list[ChanlunCandidate] = []
        chanlun_scanned = 0
        chanlun_history_ready = 0
        chanlun_error: str | None = None

        def flush_chanlun_batch() -> None:
            nonlocal chanlun_scanned, chanlun_error
            if not chanlun_batch or self.chanlun_bridge is None or chanlun_error:
                chanlun_batch.clear()
                return
            scan_as_of = max(bar.trade_date for bars in chanlun_batch.values() for bar in bars)
            try:
                scans = self.chanlun_bridge.scan_recent_prefixes(
                    chanlun_batch, scan_as_of, sessions=20
                )
                signals = confirm_recent_scans(scans, scan_as_of, sessions=20)
                chanlun_found.extend(
                    select_candidates(
                        signals,
                        chanlun_batch,
                        scan_as_of,
                        max_candidates=len(chanlun_batch),
                        valid_sessions=20,
                    )
                )
                chanlun_scanned += len(chanlun_batch)
            except Exception as exc:
                # New opportunities fail closed. Holdings and ordinary market features remain.
                chanlun_error = str(exc)[:500]
            finally:
                chanlun_batch.clear()

        for symbol in securities.keys() & available_symbols:
            item = securities[symbol]
            if item.get("risk_warning") is True:
                continue
            held = symbol in positions
            if item.get("trading_status") != "tradable" and not held:
                continue
            bars = self.store.bars_as_known(
                symbol,
                2500 if self.chanlun_bridge is not None and self.chanlun_mode != "disabled" else 130,
                daily_cutoff,
                result["as_of_time"],
            )
            bars, overlaid = _overlay_post_market_quote(
                bars, result.get("quotes", {}).get(symbol), result, daily_cutoff
            )
            post_market_quote_overlays += int(overlaid)
            actions = self.store.corporate_actions(
                symbol, daily_cutoff, result["as_of_time"]
            )
            features = _features(
                bars,
                actions,
            )
            if features is None:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "name": item["name"],
                    "security_type": item["security_type"],
                    "held": held,
                    "position": positions.get(symbol),
                    "security": item,
                    "industry": _compact_industry(industry_map.get(symbol)),
                    "fundamentals": _compact_fundamentals(fundamental_map.get(symbol)),
                    "quote": _quote_with_derived_valuation(
                        result.get("quotes", {}).get(symbol, {}),
                        fundamental_map.get(symbol),
                    ),
                    "features": features,
                }
            )
            if (
                self.chanlun_bridge is not None
                and self.chanlun_mode != "disabled"
                and item.get("trading_status") == "tradable"
                and len(bars) >= self.chanlun_min_bars
            ):
                chanlun_history_ready += 1
                chanlun_batch[symbol] = _chanlun_bars(symbol, bars, actions)
                if len(chanlun_batch) >= self.chanlun_batch_size:
                    flush_chanlun_batch()

        flush_chanlun_batch()

        by_symbol = {item["symbol"]: item for item in rows}
        holdings = [
            _source_candidate(by_symbol[symbol], "HOLDINGS", 1.0, True)
            for symbol in sorted(positions)
            if symbol in by_symbol
        ]
        if chanlun_error:
            chanlun_found = []
        newest: dict[str, ChanlunCandidate] = {}
        for candidate in chanlun_found:
            current = newest.get(candidate.symbol)
            if current is None or candidate.confirmed_date > current.confirmed_date:
                newest[candidate.symbol] = candidate
        selected_chanlun = sorted(
            newest.values(),
            key=lambda value: (
                -value.confirmed_date.toordinal(), -value.liquidity, value.symbol
            ),
        )
        routed_chanlun = {
            "CHANLUN_SECOND_BUY": [],
            "CHANLUN_THIRD_BUY": [],
        }
        for candidate in selected_chanlun:
            row = by_symbol.get(candidate.symbol)
            if row is None:
                continue
            routed_chanlun[candidate.candidate_source].append(
                _chanlun_source_candidate(row, candidate)
            )
        result["candidates"] = {"HOLDINGS": holdings}
        result["candidates"].update({key: [] for key in routed_chanlun})
        result["chanlun_daily_candidates"] = (
            routed_chanlun if self.chanlun_mode != "disabled" and not chanlun_error
            else {key: [] for key in routed_chanlun}
        )
        result["chanlun_shadow_candidates"] = (
            routed_chanlun if self.chanlun_mode == "shadow" and not chanlun_error else {}
        )
        result["research_universe"] = rows
        result["candidate_policy"] = {
            "version": POLICY_VERSION,
            "concept_refresh": "monthly",
            "concept_role": "market_background_only",
            "candidate_selection": "rust_chanlun_full_pool_then_agent_selects_up_to_20",
            "chanlun_mode": self.chanlun_mode,
            "chanlun_requested_mode": self.chanlun_requested_mode,
            "chanlun_reference_passed": self.chanlun_reference_passed,
            "signal_valid_sessions": 20,
            "signal_anchor_valid_sessions": 20,
            "maximum_new_candidates": None,
            "agent_selection_maximum": self.top_n,
            "program_ranked_new_buys": False,
            "acceptance_role": "reference_only",
            "research_universe_requires_history_features": True,
        }
        capital_budget = result.get("account", {}).get("capital_budget")
        position_market_value = sum(
            _position_market_value(item, result.get("quotes", {}))
            for item in positions.values()
        )
        result["portfolio_context"] = {
            "positions": list(positions.values()),
            "held_symbols": sorted(positions),
            "cash": result.get("account", {}).get("cash"),
            "capital_budget": capital_budget,
            "position_market_value": position_market_value,
            "planning_unallocated_budget": (
                max(float(capital_budget) - position_market_value, 0.0)
                if capital_budget is not None
                else None
            ),
        }
        coverage_ratio = len(rows) / len(securities) if securities else 0.0
        breadth = _breadth(rows, len(securities))
        result["market"].update(breadth)
        result["market"]["sector_diffusion"] = _sector_diffusion(rows)
        result["market"]["regime"] = _market_regime(breadth, coverage_ratio)
        result["feature_coverage"] = {
            "eligible_securities": len(securities),
            "symbols_with_history": len(rows),
            "history_as_of_distribution": dict(sorted(Counter(
                item["features"]["history_as_of_date"] for item in rows
            ).items())),
            "coverage_ratio": coverage_ratio,
            "minimum_history_days": 61,
            "defensive_history_days": 121,
            "daily_bar_cutoff": daily_cutoff.isoformat(),
            "price_basis_for_signals": "point_in_time_action_adjusted_raw",
            "display_price_basis": "qfq",
            "candidate_rule_version": POLICY_VERSION,
            "market_regime_rule_version": "market-regime-provisional-v1",
            "industry_symbols": sum(item.get("industry") is not None for item in rows),
            "fundamental_symbols": sum(
                item.get("fundamentals") is not None for item in rows
            ),
            "post_market_quote_overlays": post_market_quote_overlays,
            "chanlun": {
                "status": (
                    "disabled" if self.chanlun_mode == "disabled"
                    else "unavailable" if self.chanlun_bridge is None
                    else "failed" if chanlun_error
                    else "complete"
                ),
                "mode": self.chanlun_mode,
                "requested_mode": self.chanlun_requested_mode,
                "reference_validation_passed": self.chanlun_reference_passed,
                "minimum_history_bars": self.chanlun_min_bars,
                "history_ready_symbols": chanlun_history_ready,
                "scanned_symbols": chanlun_scanned,
                "candidate_count": sum(len(values) for values in routed_chanlun.values()),
                "agent_selection_maximum": self.top_n,
                "error": chanlun_error,
                "schema_version": "chanlun_bridge/v1",
            },
        }
        return result


def _overlay_post_market_quote(
    bars: list[dict[str, object]],
    quote: dict[str, object] | None,
    snapshot: dict[str, Any],
    daily_cutoff,
) -> tuple[list[dict[str, object]], bool]:
    if (
        not bars
        or not quote
        or snapshot.get("market", {}).get("session") != "post_market"
        or snapshot.get("market", {}).get("is_trading_day") is not True
    ):
        return bars, False
    available_at = quote.get("available_at")
    if not available_at or datetime.fromisoformat(str(available_at)) > datetime.fromisoformat(
        snapshot["as_of_time"]
    ):
        return bars, False
    values = {
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": quote.get("last"),
        "volume": quote.get("volume"),
        "amount": quote.get("turnover"),
    }
    try:
        if any(values[key] is None or float(values[key]) <= 0 for key in ("open", "high", "low", "close")):
            return bars, False
        if any(values[key] is None or float(values[key]) < 0 for key in ("volume", "amount")):
            return bars, False
    except (TypeError, ValueError):
        return bars, False
    current = {"trade_date": daily_cutoff.isoformat(), **values}
    result = list(bars)
    latest = str(result[-1]["trade_date"])
    if latest == daily_cutoff.isoformat():
        result[-1] = current
    elif latest < daily_cutoff.isoformat():
        result.append(current)
    else:
        return bars, False
    return result, True


def _chanlun_bars(
    symbol: str,
    bars: list[dict[str, object]],
    actions: list[dict[str, object]],
) -> list[MarketBar]:
    """Build point-in-time action-adjusted OHLC without changing execution prices."""
    adjusted_closes, _ = _signal_closes(bars, actions)
    result: list[MarketBar] = []
    for item, adjusted_close in zip(bars, adjusted_closes, strict=True):
        raw_close = float(item["close"])
        if raw_close <= 0:
            continue
        factor = adjusted_close / raw_close
        result.append(
            MarketBar(
                symbol=symbol,
                trade_date=(
                    item["trade_date"]
                    if isinstance(item["trade_date"], date)
                    else datetime.fromisoformat(str(item["trade_date"])).date()
                ),
                open=float(item["open"]) * factor,
                high=float(item["high"]) * factor,
                low=float(item["low"]) * factor,
                close=adjusted_close,
                amount=(
                    float(item["amount"])
                    if item.get("amount") is not None
                    else None
                ),
            )
        )
    return result


def _chanlun_source_candidate(
    item: dict[str, Any], candidate: ChanlunCandidate
) -> dict[str, Any]:
    signal_name = "二买" if candidate.candidate_source == "CHANLUN_SECOND_BUY" else "三买"
    chanlun = {
        **candidate.chanlun,
        "timeframe": "1d",
        "signal": candidate.signal.value,
        "anchor_date": candidate.anchor_date.isoformat(),
        "confirmed_date": candidate.confirmed_date.isoformat(),
        "first_visible_date": candidate.first_visible_date.isoformat(),
        "status": candidate.status,
        "price_basis": "point_in_time_action_adjusted_raw_ohlc",
    }
    return {
        **item,
        **candidate.as_dict(),
        "chanlun": chanlun,
        "selection_score": None,
        "entry_eligible": True,
        "program_reason": (
            f"Rust日线缠论{signal_name}；结构标记于{candidate.anchor_date.isoformat()}，"
            f"首次确认为{candidate.confirmed_date.isoformat()}；仅进入多Agent研究，非买入指令"
        ),
        "not_an_approved_trade": True,
    }


def _features(
    bars: list[dict[str, object]], actions: list[dict[str, object]] | None = None
) -> dict[str, Any] | None:
    if len(bars) < 61:
        return None
    raw_closes = [float(item["close"]) for item in bars]
    closes, applied_actions = _signal_closes(bars, actions or [])
    amounts = [float(item["amount"] or 0) for item in bars]
    daily_returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]

    def period_return(days: int) -> float | None:
        if len(closes) <= days:
            return None
        return closes[-1] / closes[-days - 1] - 1

    recent_returns = daily_returns[-20:]
    returns_60 = daily_returns[-60:]
    volatility = statistics.pstdev(recent_returns) * math.sqrt(252) if recent_returns else 0.0
    volatility_60 = (
        statistics.pstdev(returns_60) * math.sqrt(252) if len(returns_60) >= 40 else None
    )
    average_amount = statistics.fmean(amounts[-20:])
    window_120 = closes[-121:] if len(closes) >= 121 else []
    maximum_drawdown_120 = _maximum_drawdown(window_120) if window_120 else None
    monthly_positive_ratio = (
        statistics.fmean(
            closes[index] > closes[index - 20]
            for index in range(len(closes) - 100, len(closes), 20)
        )
        if len(closes) >= 121 else None
    )
    return {
        "history_as_of_date": str(bars[-1]["trade_date"]),
        "return_1d": daily_returns[-1],
        "return_5d": period_return(5) or 0.0,
        "return_20d": period_return(20) or 0.0,
        "return_60d": period_return(60) or 0.0,
        "return_120d": period_return(120),
        "volatility_20d_annualized": volatility,
        "volatility_60d_annualized": volatility_60,
        "maximum_drawdown_120d": maximum_drawdown_120,
        "positive_20d_windows_ratio": monthly_positive_ratio,
        "close_to_ma20": closes[-1] / statistics.fmean(closes[-20:]) - 1,
        "amount_20d_average": average_amount,
        "amount_ratio_20d": amounts[-1] / average_amount if average_amount > 0 else 0.0,
        "last_amount": amounts[-1],
        "last_raw_close": raw_closes[-1],
        "previous_raw_close": raw_closes[-2],
        "corporate_actions_applied": float(applied_actions),
    }


def _maximum_drawdown(closes: list[float]) -> float:
    peak = closes[0]
    worst = 0.0
    for value in closes:
        peak = max(peak, value)
        worst = min(worst, value / peak - 1.0)
    return worst


def _signal_closes(
    bars: list[dict[str, object]], actions: list[dict[str, object]]
) -> tuple[list[float], int]:
    by_ex_date: dict[str, dict[str, object]] = {}
    for action in actions:
        ex_date = str(action.get("ex_date") or "")
        announcement_date = str(action.get("announcement_date") or "")
        if not ex_date or not announcement_date or announcement_date > ex_date:
            continue
        current = by_ex_date.get(ex_date)
        if current is None or _action_completeness(action) > _action_completeness(current):
            by_ex_date[ex_date] = action

    raw_closes = [float(item["close"]) for item in bars]
    adjusted = [raw_closes[0]]
    applied = 0
    for index in range(1, len(bars)):
        previous = raw_closes[index - 1]
        denominator = previous
        action = by_ex_date.get(str(bars[index]["trade_date"]))
        if action is not None:
            cash = float(action.get("cash_dividend_per_10") or 0.0) / 10.0
            shares = (
                float(action.get("bonus_shares_per_10") or 0.0)
                + float(action.get("transfer_shares_per_10") or 0.0)
            ) / 10.0
            theoretical = (previous - cash) / (1.0 + shares)
            if theoretical > 0:
                denominator = theoretical
                applied += 1
        day_return = raw_closes[index] / denominator - 1.0
        adjusted.append(adjusted[-1] * (1.0 + day_return))
    return adjusted, applied


def _action_completeness(action: dict[str, object]) -> int:
    return sum(
        action.get(key) is not None
        for key in (
            "cash_dividend_per_10",
            "bonus_shares_per_10",
            "transfer_shares_per_10",
        )
    )


def _quality_value_candidates(
    rows: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    eligible = []
    for item in rows:
        fundamental = item.get("fundamentals")
        quote = item.get("quote", {})
        if item["security_type"] != "stock" or not fundamental:
            continue
        values = (
            fundamental.get("return_on_equity"),
            fundamental.get("gross_margin"),
            fundamental.get("operating_cash_flow_per_share"),
            fundamental.get("net_profit"),
            fundamental.get("revenue_growth"),
            fundamental.get("net_profit_growth"),
            quote.get("pe_ratio"),
            quote.get("pb_ratio"),
        )
        if any(value is None for value in values):
            continue
        roe, gross_margin, cashflow, profit, revenue_growth, profit_growth, pe, pb = (
            float(value) for value in values
        )
        if not (
            roe >= 8 and gross_margin > 0 and cashflow > 0 and profit > 0
            and revenue_growth > -20 and profit_growth > -30
            and 0 < pe <= 35 and 0 < pb <= 5
        ):
            continue
        eligible.append(item)
    if not eligible:
        return []
    metrics = {
        "roe": [float(item["fundamentals"]["return_on_equity"]) for item in eligible],
        "gross": [float(item["fundamentals"]["gross_margin"]) for item in eligible],
        "cash": [
            float(item["fundamentals"]["operating_cash_flow_per_share"])
            for item in eligible
        ],
        "growth": [float(item["fundamentals"]["net_profit_growth"]) for item in eligible],
        "pe": [float(item["quote"]["pe_ratio"]) for item in eligible],
        "pb": [float(item["quote"]["pb_ratio"]) for item in eligible],
        "vol": [float(item["features"]["volatility_20d_annualized"]) for item in eligible],
    }
    ranked = []
    for item in eligible:
        fundamental = item["fundamentals"]
        quote = item["quote"]
        score = (
            0.16 * _percentile(metrics["roe"], float(fundamental["return_on_equity"]))
            + 0.10 * _percentile(metrics["gross"], float(fundamental["gross_margin"]))
            + 0.10 * _percentile(metrics["cash"], float(fundamental["operating_cash_flow_per_share"]))
            + 0.09 * _percentile(metrics["growth"], float(fundamental["net_profit_growth"]))
            + 0.20 * (1 - _percentile(metrics["pe"], float(quote["pe_ratio"])))
            + 0.15 * (1 - _percentile(metrics["pb"], float(quote["pb_ratio"])))
            + 0.20 * (1 - _percentile(metrics["vol"], float(item["features"]["volatility_20d_annualized"])))
        )
        if score >= 0.50:
            ranked.append((score, item))
    selected = _industry_neutral_select(ranked, limit, 2)
    return [
        _source_candidate(item, "QUALITY_VALUE", score, _anti_chase_ok(item))
        for score, item in selected
    ]


def _defensive_candidates(
    rows: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    eligible = []
    for item in rows:
        features = item["features"]
        vol = features.get("volatility_60d_annualized")
        drawdown = features.get("maximum_drawdown_120d")
        positive = features.get("positive_20d_windows_ratio")
        minimum_amount = 50_000_000 if item["security_type"] == "stock" else 20_000_000
        if None in (vol, drawdown, positive):
            continue
        if not (
            float(vol) <= 0.35
            and float(drawdown) >= -0.25
            and float(features["amount_20d_average"]) >= minimum_amount
            and abs(float(features["close_to_ma20"])) <= 0.10
            and float(features["return_60d"]) > -0.10
        ):
            continue
        eligible.append(item)
    if not eligible:
        return []
    vols = [float(item["features"]["volatility_60d_annualized"]) for item in eligible]
    drawdowns = [abs(float(item["features"]["maximum_drawdown_120d"])) for item in eligible]
    liquidity = [math.log1p(float(item["features"]["amount_20d_average"])) for item in eligible]
    ranked = []
    for item in eligible:
        values = item["features"]
        score = (
            0.40 * (1 - _percentile(vols, float(values["volatility_60d_annualized"])))
            + 0.30 * (1 - _percentile(drawdowns, abs(float(values["maximum_drawdown_120d"]))))
            + 0.15 * float(values["positive_20d_windows_ratio"])
            + 0.15 * _percentile(liquidity, math.log1p(float(values["amount_20d_average"])))
        )
        if score >= 0.50:
            ranked.append((score, item))
    selected = _industry_neutral_select(ranked, limit, 2)
    return [
        _source_candidate(item, "DEFENSIVE", score, _anti_chase_ok(item))
        for score, item in selected
    ]


def _tactical_candidates(
    rows: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    ranked = []
    for item in rows:
        values = item["features"]
        one_day = abs(float(values["return_1d"]))
        amount_ratio = float(values["amount_ratio_20d"])
        if one_day < 0.02 or amount_ratio < 1.5:
            continue
        score = min(one_day / 0.10, 1.0) * 0.45 + min(amount_ratio / 5, 1.0) * 0.55
        if score >= 0.35:
            ranked.append((score, item))
    selected = _industry_neutral_select(ranked, limit, 2)
    return [
        _source_candidate(item, "TACTICAL", score, _anti_chase_ok(item))
        for score, item in selected
    ]


def _source_candidate(
    item: dict[str, Any], source: str, score: float, entry_eligible: bool
) -> dict[str, Any]:
    reasons = {
        "HOLDINGS": _holding_reason,
        "QUALITY_VALUE": _quality_value_reason,
        "DEFENSIVE": _defensive_reason,
        "TACTICAL": _tactical_reason,
    }
    result = {
        **item,
        "candidate_source": source,
        "selection_score": score,
        "entry_eligible": entry_eligible,
        "program_reason": reasons[source](item),
        "not_an_approved_trade": True,
    }
    if not entry_eligible and source != "HOLDINGS":
        result["entry_blocker"] = "防追涨门槛未通过，仅供研究，不得进入新买入方案"
    return result


def _holding_reason(item: dict[str, Any]) -> str:
    values = item["features"]
    position = item.get("position") or {}
    return (
        f"现有持仓，成本{float(position.get('cost_price') or 0):.3f}；"
        f"20日{values['return_20d'] * 100:+.2f}%，"
        f"最大回撤120日{_fmt_percent(values.get('maximum_drawdown_120d'))}；"
        "必须复核原持有逻辑、公告和退出条件"
    )


def _quality_value_reason(item: dict[str, Any]) -> str:
    fundamental = item["fundamentals"]
    quote = item["quote"]
    valuation_note = (
        "（EPS/BVPS推算）"
        if quote.get("pe_ratio_method") or quote.get("pb_ratio_method") else ""
    )
    return (
        f"ROE {float(fundamental['return_on_equity']):.2f}%，"
        f"毛利率{float(fundamental['gross_margin']):.2f}%，"
        f"经营现金流/股{float(fundamental['operating_cash_flow_per_share']):.3f}，"
        f"净利润同比{float(fundamental['net_profit_growth']):+.2f}%，"
        f"PE {float(quote['pe_ratio']):.2f}、PB {float(quote['pb_ratio']):.2f}{valuation_note}；"
        f"财报公告日{fundamental['announcement_date']}"
    )


def _defensive_reason(item: dict[str, Any]) -> str:
    values = item["features"]
    return (
        f"60日年化波动{float(values['volatility_60d_annualized']) * 100:.2f}%，"
        f"120日最大回撤{float(values['maximum_drawdown_120d']) * 100:.2f}%，"
        f"60日收益{float(values['return_60d']) * 100:+.2f}%，"
        f"20日均成交额{float(values['amount_20d_average']) / 100_000_000:.2f}亿元"
    )


def _tactical_reason(item: dict[str, Any]) -> str:
    values = item["features"]
    eligibility = "可进入后续核验" if _anti_chase_ok(item) else "触发防追涨，仅观察"
    return (
        f"单日{float(values['return_1d']) * 100:+.2f}%，"
        f"成交额为20日均值{float(values['amount_ratio_20d']):.2f}倍，"
        f"5日{float(values['return_5d']) * 100:+.2f}%，"
        f"相对MA20 {float(values['close_to_ma20']) * 100:+.2f}%；{eligibility}"
    )


def _anti_chase_ok(item: dict[str, Any]) -> bool:
    values = item["features"]
    return (
        float(values["return_1d"]) < 0.07
        and float(values["return_5d"]) < 0.15
        and float(values["close_to_ma20"]) < 0.12
        and not _at_estimated_limit_up(item)
    )


def _at_estimated_limit_up(item: dict[str, Any]) -> bool:
    if item["security_type"] != "stock":
        return False
    ratio = item.get("security", {}).get("price_limit_ratio")
    threshold = float(ratio) if ratio is not None else 0.10
    return float(item["features"]["return_1d"]) >= threshold - 0.001


def _industry_neutral_select(
    ranked: list[tuple[float, dict[str, Any]]], limit: int, per_industry: int
) -> list[tuple[float, dict[str, Any]]]:
    counts: dict[str, int] = {}
    selected = []
    for score, item in sorted(ranked, key=lambda value: (-value[0], value[1]["symbol"])):
        group = _industry_name(item) or (
            f"ETF:{item.get('security', {}).get('fund_category') or '未分类'}"
            if item["security_type"] == "etf" else "未分类股票"
        )
        if counts.get(group, 0) >= per_industry:
            continue
        selected.append((score, item))
        counts[group] = counts.get(group, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def _percentile(values: list[float], value: float) -> float:
    if len(values) <= 1:
        return 0.5
    ordered = sorted(values)
    below = sum(item < value for item in ordered)
    equal = sum(item == value for item in ordered)
    return (below + 0.5 * equal) / len(ordered)


def _fmt_percent(value: object) -> str:
    return "数据不足" if value is None else f"{float(value) * 100:.2f}%"


def _industry_name(item: dict[str, Any]) -> str | None:
    industry = item.get("industry") or {}
    return industry.get("minor") or industry.get("middle") or industry.get("major")


def _position_market_value(
    position: dict[str, Any], quotes: dict[str, dict[str, Any]]
) -> float:
    quote = quotes.get(normalize_symbol(position["symbol"]), {})
    last = quote.get("last")
    quantity = position.get("quantity")
    if last is not None and quantity is not None:
        return float(last) * float(quantity)
    return float(position.get("market_value") or 0)


def _compact_industry(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        key: value.get(key)
        for key in (
            "standard",
            "standard_code",
            "industry_code",
            "section",
            "major",
            "middle",
            "minor",
            "effective_from",
            "source",
        )
    }


def _compact_fundamentals(
    value: dict[str, object] | None
) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        key: value.get(key)
        for key in (
            "report_period", "announcement_date", "available_at", "revenue",
            "revenue_growth", "net_profit", "net_profit_growth",
            "earnings_per_share", "book_value_per_share",
            "operating_cash_flow_per_share", "return_on_equity", "gross_margin",
            "industry_name", "source", "source_url",
        )
    }


def _quote_with_derived_valuation(
    quote: dict[str, object], fundamental: dict[str, object] | None
) -> dict[str, object]:
    result = dict(quote)
    if not fundamental or result.get("last") is None:
        return result
    last = float(result["last"])
    if result.get("pb_ratio") is None:
        bvps = fundamental.get("book_value_per_share")
        if bvps is not None and float(bvps) > 0:
            result["pb_ratio"] = last / float(bvps)
            result["pb_ratio_method"] = "price_divided_by_latest_announced_bvps"
    if result.get("pe_ratio") is None:
        eps = fundamental.get("earnings_per_share")
        report_period = fundamental.get("report_period")
        if eps is not None and report_period:
            month = datetime.fromisoformat(str(report_period)).month
            annualized_eps = float(eps) * 12 / month if month else 0.0
            if annualized_eps > 0:
                result["pe_ratio"] = last / annualized_eps
                result["pe_ratio_method"] = "price_divided_by_annualized_ytd_eps"
    return result


def _breadth(rows: list[dict[str, Any]], eligible_count: int) -> dict[str, object]:
    changes = [item["features"]["return_1d"] for item in rows]
    if not changes:
        return {"breadth": None, "turnover": None, "limit_activity": None}
    limit_up = 0
    limit_down = 0
    for item in rows:
        if item["security_type"] != "stock":
            continue
        if item["security"].get("price_limit_exempt"):
            continue
        ratio = item["security"].get("price_limit_ratio")
        limit_ratio = float(ratio) if ratio is not None else 0.10
        change = item["features"]["return_1d"]
        limit_up += change >= limit_ratio - 0.001
        limit_down += change <= -limit_ratio + 0.001
    return {
        "breadth": {
            "advancers": sum(value > 0 for value in changes),
            "decliners": sum(value < 0 for value in changes),
            "unchanged": sum(value == 0 for value in changes),
            "above_ma20": sum(item["features"]["close_to_ma20"] > 0 for item in rows),
            "coverage": len(rows),
            "eligible": eligible_count,
            "coverage_ratio": len(rows) / eligible_count if eligible_count else 0.0,
            "median_return_1d": statistics.median(changes),
        },
        "limit_activity": {
            "estimated_limit_up": limit_up,
            "estimated_limit_down": limit_down,
            "method": "action_adjusted_return_vs_versioned_limit_estimate",
        },
        "turnover": sum(item["features"]["last_amount"] for item in rows),
        "turnover_20d_average": sum(
            item["features"]["amount_20d_average"] for item in rows
        ),
    }


def _daily_bar_cutoff(snapshot: dict[str, Any]):
    completed = snapshot.get("market", {}).get("latest_completed_trade_date")
    if completed:
        return date.fromisoformat(str(completed))
    as_of = datetime.fromisoformat(snapshot["as_of_time"])
    session = snapshot.get("market", {}).get("session")
    return as_of.date() if session == "post_market" else as_of.date() - timedelta(days=1)


def _market_regime(breadth: dict[str, object], coverage_ratio: float) -> dict[str, object]:
    values = breadth.get("breadth")
    sample_size = int(values["coverage"]) if values else 0
    if coverage_ratio < 0.8 or sample_size < 100:
        return {
            "state": "insufficient_data",
            "reason": "历史特征覆盖率不足 80% 或有效样本少于 100，不对全市场状态作强判断",
            "coverage_ratio": coverage_ratio,
            "sample_size": sample_size,
        }
    coverage = int(values["coverage"])
    advancer_ratio = int(values["advancers"]) / coverage
    above_ma20_ratio = int(values["above_ma20"]) / coverage
    if advancer_ratio >= 0.6 and above_ma20_ratio >= 0.55:
        state = "offensive"
    elif advancer_ratio <= 0.35 and above_ma20_ratio <= 0.4:
        state = "defensive"
    else:
        state = "neutral"
    return {
        "state": state,
        "advancer_ratio": advancer_ratio,
        "above_ma20_ratio": above_ma20_ratio,
        "coverage_ratio": coverage_ratio,
    }


def _sector_diffusion(rows: list[dict[str, Any]]) -> dict[str, object]:
    stock_rows = [item for item in rows if item["security_type"] == "stock"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in stock_rows:
        industry = item.get("industry")
        if not industry:
            continue
        name = industry.get("major") or industry.get("middle") or industry.get("section")
        if name:
            groups.setdefault(str(name), []).append(item)
    sectors = []
    for name, members in groups.items():
        if len(members) < 3:
            continue
        changes = [item["features"]["return_1d"] for item in members]
        above_ma20 = sum(item["features"]["close_to_ma20"] > 0 for item in members)
        advancers = sum(value > 0 for value in changes)
        sectors.append(
            {
                "industry": name,
                "members_covered": len(members),
                "advancers": advancers,
                "advancer_ratio": advancers / len(members),
                "above_ma20_ratio": above_ma20 / len(members),
                "median_return_1d": statistics.median(changes),
                "median_return_20d": statistics.median(
                    item["features"]["return_20d"] for item in members
                ),
            }
        )
    sectors.sort(
        key=lambda item: (
            item["advancer_ratio"] + item["above_ma20_ratio"],
            item["median_return_20d"],
        ),
        reverse=True,
    )
    classified = sum(item.get("industry") is not None for item in stock_rows)
    return {
        "status": "available" if sectors else "insufficient_data",
        "classified_stocks": classified,
        "stock_feature_rows": len(stock_rows),
        "classification_coverage_ratio": classified / len(stock_rows) if stock_rows else 0.0,
        "leading": sectors[:10],
        "lagging": list(reversed(sectors[-10:])),
    }
