"""Retrospective minute-bar diagnostics; never a PIT selection or fill claim."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import statistics
import uuid
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "https://akshare.akfamily.xyz/data/stock/stock.html#id24"
SINA_SOURCE = "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData"
DEFAULT_THRESHOLDS = {"return_min": .01, "return_max": .05, "vwap_max": .025,
                      "high_distance_max": .01, "amount_min": 100_000_000}


def fetch_sina_five_minutes(symbol):
    """AKShare's raw Sina endpoint with an explicit 15-second request bound."""
    import requests
    prefix = "sh" if symbol.startswith("6") else "sz"
    response = requests.get(SINA_SOURCE, params={"symbol": prefix + symbol, "scale": "5", "ma": "no", "datalen": "1970"}, timeout=15)
    response.raise_for_status()
    raw = json.loads(response.text.split("=(", 1)[1].split(");", 1)[0])
    checked = matching = 0
    for row in raw:
        volume, amount = _number(row.get("volume")), _number(row.get("amount"))
        low, high = _number(row.get("low")), _number(row.get("high"))
        if volume and volume > 0 and amount and amount > 0 and low and high:
            checked += 1
            matching += low * Decimal(".99") <= amount / volume <= high * Decimal("1.01")
    if checked < 30 or matching / checked < .95:
        raise ValueError("Sina成交额或股单位无法通过amount/volume与OHLC交叉核验；不伪造VWAP")
    return [{"time": row["day"], **{key: row.get(key) for key in ("open", "high", "low", "close", "amount")},
             "volume": str(_number(row["volume"]) / 100), "provider_volume": row["volume"],
             "provider_volume_unit": "shares", "amount_basis": "source_reported"} for row in raw]


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except Exception:
        return None


def _stamp(value):
    value = datetime.fromisoformat(str(value))
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def minute_dataset_end_time(minute_data):
    """Latest observed raw bar, including rows unused by the diagnostic."""
    stamps = []
    for rows in minute_data.values():
        for row in rows:
            try:
                stamps.append(_stamp(row.get("time", row.get("时间"))))
            except (ValueError, TypeError):
                continue
    return max(stamps).isoformat() if stamps else None


def normalize_minutes(rows):
    normalized = {}
    keys = {"open": "开盘", "high": "最高", "low": "最低", "close": "收盘", "volume": "成交量", "amount": "成交额"}
    for raw in rows:
        try:
            stamp = _stamp(raw.get("time", raw.get("时间")))
        except (ValueError, TypeError):
            continue
        values = {key: _number(raw.get(key, raw.get(chinese))) for key, chinese in keys.items()}
        if any(value is None for value in values.values()):
            continue
        if min(values["open"], values["low"], values["close"]) <= 0 or values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"]) or min(values["volume"], values["amount"]) < 0:
            continue
        normalized[stamp] = {"time": stamp.isoformat(), **{key: str(value) for key, value in values.items()}}
    return [normalized[key] for key in sorted(normalized)]


def _signal(rows, previous_close, day, period, thresholds):
    cutoff = datetime.combine(day, time(14, 50), TZ)
    before = [row for row in rows if _stamp(row["time"]).date() == day and _stamp(row["time"]) <= cutoff]
    expected = [datetime.combine(day, time(9, 30), TZ) + timedelta(minutes=n)
                for n in range(period, 121, period)]
    expected += [datetime.combine(day, time(13), TZ) + timedelta(minutes=n)
                 for n in range(period, 111, period)]
    present = {_stamp(row["time"]) for row in before}
    if not set(expected) <= present:
        return None, "incomplete_pre_cutoff_minutes"
    last = next((row for row in before if _stamp(row["time"]) == cutoff), None)
    if last is None or not previous_close:
        return None, "missing_cutoff_or_previous_session_close"
    volume = sum((_number(row["volume"]) for row in before), Decimal(0))
    amount = sum((_number(row["amount"]) for row in before), Decimal(0))
    price = _number(last["close"])
    high = max(_number(row["high"]) for row in before)
    vwap = amount / (volume * 100) if volume else Decimal(0)
    ret = price / previous_close - 1
    metrics = {"return_at_cutoff": float(ret), "amount_at_cutoff": float(amount),
               "vwap": float(vwap), "price": float(price), "distance_from_high": float(price / high - 1),
               "observations": len(before), "latest_signal_time": last["time"]}
    accepted = (Decimal(str(thresholds["return_min"])) <= ret <= Decimal(str(thresholds["return_max"]))
                and vwap > 0 and vwap <= price <= vwap * (1 + Decimal(str(thresholds["vwap_max"])))
                and price / high >= 1 - Decimal(str(thresholds["high_distance_max"]))
                and amount >= Decimal(str(thresholds["amount_min"])))
    return metrics, None if accepted else "signal_threshold_not_met"


def _can_fill(row, side, reference, ratio, quantity):
    if row is None or _number(row["volume"]) * 100 < quantity:
        return False
    limit = (reference * (1 + ratio if side == "buy" else 1 - ratio)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    # Never assume queue priority at the limit, including a bar opening at the limit.
    price = _number(row["open"]) * (Decimal("1.001") if side == "buy" else Decimal(".999"))
    return price < limit if side == "buy" else price > limit


def diagnose_closing_history(snapshot, scan, minute_data, *, period=1, min_samples=30,
                             capital_per_trade="5000", thresholds=None, lookback_days=5):
    if period not in (1, 5):
        raise ValueError("仅支持 1 或 5 分钟精度")
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    calendar = snapshot.get("market", {}).get("recent_completed_trade_dates", [])
    securities = {row["symbol"]: row for row in snapshot.get("securities", [])}
    details = []
    for candidate in scan.get("candidates", []):
        symbol = candidate["symbol"]
        rows = normalize_minutes(minute_data.get(symbol, []))
        days = calendar[-lookback_days:] or sorted({_stamp(row["time"]).date().isoformat() for row in rows})
        by_stamp = {_stamp(row["time"]): row for row in rows}
        for day_text in days:
            record = {"symbol": symbol, "trade_date": day_text, "status": "unavailable", "net_return": None}
            details.append(record)
            if day_text not in calendar or calendar.index(day_text) == 0 or calendar.index(day_text) + 1 >= len(calendar):
                record["reason"] = "previous_or_next_completed_trading_day_unavailable"
                continue
            index = calendar.index(day_text)
            day = datetime.fromisoformat(day_text).date()
            next_day = datetime.fromisoformat(calendar[index + 1]).date()
            previous = by_stamp.get(datetime.fromisoformat(calendar[index - 1] + "T15:00:00+08:00"))
            prev_close = _number(previous["close"]) if previous else None
            metrics, reason = _signal(rows, prev_close, day, period, thresholds)
            record["signal"] = metrics
            if reason:
                record.update(status="no_signal" if metrics else "unavailable", reason=reason)
                continue
            security = securities.get(symbol, candidate.get("security", {}))
            if security.get("security_type") != "stock" or not symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
                record["reason"] = "unsupported_security_rules"
                continue
            rules = snapshot.get("historical_trade_rules", {}).get(symbol, {})
            today_rule, tomorrow_rule = rules.get(day_text, {}), rules.get(next_day.isoformat(), {})
            record["rule_basis"] = "historical_records" if today_rule and tomorrow_rule else "main_board_10pct_scenario_historical_status_unverified"
            if today_rule.get("trading_status") == "suspended" or today_rule.get("risk_warning") is True:
                record["reason"] = "historical_suspension_or_risk_warning"
                continue
            entry_end = datetime.combine(day, time(14, 50), TZ) + timedelta(minutes=period)
            entry = by_stamp.get(entry_end)
            if entry is None:
                record["reason"] = "missing_post_signal_entry_bar"
                continue
            entry_price = _number(entry["open"]) * Decimal("1.001")
            quantity = int((Decimal(str(capital_per_trade)) - 1) // (entry_price * 100)) * 100
            entry_ratio = _number(today_rule.get("price_limit_ratio", ".10"))
            if quantity <= 0 or not _can_fill(entry, "buy", prev_close, entry_ratio, quantity):
                record["reason"] = "entry_lot_volume_or_limit_blocked"
                continue
            record.update(quantity=quantity, entry_bar_end=entry["time"], entry_price=str(entry_price), next_trade_date=next_day.isoformat())
            record.update(entry_cost_cny=float(entry_price * quantity + 1), unused_trade_budget_cny=float(Decimal(str(capital_per_trade)) - entry_price * quantity - 1))
            if tomorrow_rule.get("trading_status") == "suspended" or tomorrow_rule.get("risk_warning") is True:
                record.update(status="unresolved_position", reason="next_day_suspended_or_rules_changed")
                continue
            closing = by_stamp.get(datetime.combine(day, time(15), TZ))
            if closing is None:
                record.update(status="unresolved_position", reason="missing_entry_day_close_for_next_day_reference")
                continue
            reference = _number(closing["close"])
            morning = [row for row in rows if _stamp(row["time"]).date() == next_day
                       and time(9, 30) < _stamp(row["time"]).time() <= time(10)]
            first_time = datetime.combine(next_day, time(9, 30), TZ) + timedelta(minutes=period)
            if not morning or _stamp(morning[0]["time"]) != first_time:
                record.update(status="unresolved_position", reason="next_open_missing_or_suspended")
                continue
            gap = _number(morning[0]["open"]) / reference - 1
            branch = "high" if gap > Decimal(".003") else "low" if gap < Decimal("-.003") else "flat"
            record.update(opening_branch=branch, opening_gap=float(gap), quantity=quantity,
                          entry_bar_end=entry["time"], entry_price=str(entry_price), next_trade_date=next_day.isoformat())
            exit_row = None
            ratio = _number(tomorrow_rule.get("price_limit_ratio", ".10"))
            trigger = branch == "low"
            for i, row in enumerate(morning):
                stamp = _stamp(row["time"])
                if i and stamp - _stamp(morning[i - 1]["time"]) != timedelta(minutes=period):
                    record["reason"] = "morning_minute_gap"
                    break
                # A close-based trigger can only fill on a subsequent bar.
                start = stamp - timedelta(minutes=period)
                due = trigger or start.time() >= time(9, 45) or stamp.time() == time(10)
                if due and _can_fill(row, "sell", reference, ratio, quantity):
                    exit_row = row
                    break
                if branch == "high" and _number(row["close"]) <= reference:
                    trigger = True
            if exit_row is None:
                record.update(status="unresolved_position", reason=record.get("reason", "no_sellable_bar_before_10am"))
                continue
            exit_price = _number(exit_row["open"]) * Decimal(".999")
            cost = entry_price * quantity + 1
            proceeds = exit_price * quantity * Decimal(".9995") - 1
            fees = Decimal(2) + exit_price * quantity * Decimal(".0005")
            slippage_cost = (_number(entry["open"]) + _number(exit_row["open"])) * Decimal(".001") * quantity
            record.update(status="simulated_bar_proxy", exit_bar_end=exit_row["time"], exit_price=str(exit_price),
                          net_pnl_cny=float(proceeds - cost), net_return=float((proceeds - cost) / cost),
                          fees_and_tax_cny=float(fees), slippage_cost_cny=float(slippage_cost), total_cost_cny=float(fees + slippage_cost))
    returns = [row["net_return"] for row in details if row["net_return"] is not None]
    by_day = []
    for day in sorted({row["trade_date"] for row in details}):
        daily = [row for row in details if row["trade_date"] == day]
        values = [row["net_return"] for row in daily if row["net_return"] is not None]
        by_day.append({"trade_date": day, "candidate_samples": len(daily), "available_signals": sum(row.get("signal") is not None for row in daily),
                       "simulated_samples": len(values), "coverage": len(values) / len(daily),
                       "signal_data_coverage": sum(row.get("signal") is not None for row in daily) / len(daily),
                       "net_return_mean": statistics.mean(values) if values else None,
                       "positive_samples": sum(value > 0 for value in values), "negative_samples": sum(value < 0 for value in values),
                       "net_return_stddev": statistics.stdev(values) if len(values) > 1 else None,
                       "net_return_min": min(values) if values else None})
    branches = {key: sum(row.get("opening_branch") == key for row in details) for key in ("high", "flat", "low")}
    branch_count = sum(branches.values())
    split_day = by_day[len(by_day) // 2]["trade_date"] if by_day else None
    stability = []
    for label in ("early_half", "late_half"):
        values = [row["net_return"] for row in details if row["net_return"] is not None
                  and ((row["trade_date"] < split_day) if label == "early_half" else (row["trade_date"] >= split_day))]
        stability.append({"period": label, "split_trade_date": split_day, "sample_count": len(values),
                          "mean_net_return": statistics.mean(values) if values else None,
                          "positive_samples": sum(value > 0 for value in values),
                          "worst_net_return": min(values) if values else None})
    return {"module": "closing", "experiment_type": "selected_today_retrospective", "pit_selection_backtest": False,
            "status": "insufficient_history" if len(returns) < min_samples else "retrospective_diagnostic_only",
            "stable_profitability_validated": False, "period_minutes": period, "minimum_samples": min_samples,
            "requested_lookback_trade_days": lookback_days,
            "thresholds": thresholds, "sample_count": len(returns), "candidate_count": len(scan.get("candidates", [])),
            "days": by_day, "details": details,
            "temporal_stability_observation": stability,
            "summary": {"net_return_distribution": sorted(returns), "net_return_mean": statistics.mean(returns) if returns else None,
                        "positive_samples": sum(value > 0 for value in returns), "negative_samples": sum(value < 0 for value in returns),
                        "trade_days_with_completed_samples": sum(day["simulated_samples"] > 0 for day in by_day),
                        "net_return_median": statistics.median(returns) if returns else None,
                        "worst_net_return": min(returns) if returns else None,
                        "worst_net_pnl_cny": min((row["net_pnl_cny"] for row in details if "net_pnl_cny" in row), default=None),
                        "worst_loss_scope": "completed_simulated_samples_only_unresolved_losses_unknown",
                        "opening_counts": branches, "opening_ratios": {key: value / branch_count if branch_count else None for key, value in branches.items()},
                        "unresolved_positions": sum(row["status"] == "unresolved_position" for row in details)},
            "assumptions": {"commission_cny_per_order": 1, "stock_sell_stamp_duty_rate": .0005, "slippage_bps_per_leg": 10,
                            "capital_per_trade_cny": str(capital_per_trade), "lot_size": 100,
                            "independent_trades_not_portfolio_equity_curve": True,
                            "same_day_total_capital_not_constrained": True,
                            "simultaneous_5000_cny_trades_must_not_be_reported_as_module_returns": True,
                            "price_precision": "next_bar_open_proxy_after_cutoff", "volume_unit": "lots_100_shares"},
            "limitations": ["当前候选回看存在选择及幸存者偏差，不是当时全市场选股回测", "源数据在实验时抓取，不能证明历史时点已可用且未修订",
                            "无逐日历史规则记录的样本仅按主板10%涨跌幅情景模拟，不代表可成交历史实绩",
                            "分钟OHLC不能证明盘口深度、排队优先级或实际成交；未平仓样本不计已实现收益",
                            "每笔5000元独立机会诊断，未限制同日总投入5000元；不是本模块组合收益或资金曲线",
                            "同日股票受共同市场冲击，样本相关；前后时段及三组阈值只是固定敏感性观察，不事后挑选最优参数",
                            "原始价格收益未计分红权益、配股等公司行动，不是完整总回报",
                            "短期样本与阈值敏感性不能证明稳定性，需跨日保存前瞻候选并继续验证"]}


def diagnose_daily_gap_proxy(snapshot, scan, daily_data, capital_per_trade="5000"):
    """A separately named daily-close/open proxy, with no tail-session signal test."""
    calendar = snapshot.get("market", {}).get("recent_completed_trade_dates", [])
    samples = []
    for candidate in scan.get("candidates", []):
        symbol = candidate["symbol"]
        bars = {str(row["trade_date"]): row for row in daily_data.get(symbol, [])}
        for day in calendar[-61:]:
            if day not in bars or calendar.index(day) + 1 >= len(calendar):
                continue
            next_day = calendar[calendar.index(day) + 1]
            following = bars.get(next_day)
            if following is None:
                continue
            close, opening = _number(bars[day].get("close")), _number(following.get("open"))
            if close is None or opening is None or min(close, opening) <= 0:
                continue
            buy, sell = close * Decimal("1.001"), opening * Decimal(".999")
            quantity = int((Decimal(str(capital_per_trade)) - 1) // (buy * 100)) * 100
            if quantity <= 0:
                continue
            cost, proceeds = buy * quantity + 1, sell * quantity * Decimal(".9995") - 1
            samples.append({"symbol": symbol, "trade_date": day, "next_trade_date": next_day,
                            "close": str(close), "next_open": str(opening), "raw_gap": float(opening / close - 1),
                            "cost_adjusted_proxy_return": float((proceeds - cost) / cost),
                            "source": bars[day].get("source"), "available_at": bars[day].get("available_at"),
                            "next_source": following.get("source"), "next_available_at": following.get("available_at")})
    returns = [row["cost_adjusted_proxy_return"] for row in samples]
    days = []
    for day in sorted({row["trade_date"] for row in samples}):
        values = [row["cost_adjusted_proxy_return"] for row in samples if row["trade_date"] == day]
        days.append({"trade_date": day, "sample_count": len(values), "net_proxy_mean": statistics.mean(values), "net_proxy_min": min(values)})
    branches = {name: sum((row["raw_gap"] > .003 if name == "high" else row["raw_gap"] < -.003 if name == "low" else -.003 <= row["raw_gap"] <= .003) for row in samples) for name in ("high", "flat", "low")}
    possible = max(0, min(60, len(calendar) - 1)) * len(scan.get("candidates", []))
    return {"experiment_type": "daily_close_next_open_proxy", "selection_basis": "selected_today_retrospective",
            "validates_tail_session_strategy": False, "execution_model_applied": False,
            "samples": samples, "sample_count": len(samples), "days": days,
            "possible_symbol_day_pairs": possible, "coverage": len(samples) / possible if possible else 0,
            "summary": {"net_proxy_distribution": sorted(returns), "net_proxy_mean": statistics.mean(returns) if returns else None,
                        "net_proxy_median": statistics.median(returns) if returns else None,
                        "worst_net_proxy_return": min(returns) if returns else None,
                        "opening_counts": branches, "opening_ratios": {key: value / len(samples) if samples else None for key, value in branches.items()}},
            "limitations": ["日线收盘到次日开盘价差，未核验14:50分钟信号或次日分支退出", "未应用盘口、停牌或涨跌停可成交模型，收益仅为价格算术代理，不能并入分钟实验统计"]}


def run_closing_experiments(snapshot, scan, output_dir, *, period=1, max_symbols=10, min_samples=30,
                            capital_per_trade="5000", minute_loader=None, daily_data=None, market_store=None,
                            minute_source="auto", lookback_days=40):
    if period not in (1, 5) or not 1 <= max_symbols <= 10:
        raise ValueError("实验支持1/5分钟，最多10只证券")
    if minute_source not in {"auto", "eastmoney", "sina"} or not 1 <= lookback_days <= 60 or min_samples < 1:
        raise ValueError("数据源须为auto/eastmoney/sina，回看1–60交易日且最小样本数为正")
    auto_fallback = minute_loader is None and minute_source == "auto"
    source_name = "custom_loader" if minute_loader else "sina" if minute_source == "sina" else "eastmoney"
    if minute_loader is None and source_name == "sina":
        minute_loader, period = fetch_sina_five_minutes, 5
    elif minute_loader is None:
        import akshare as ak
        minute_loader = lambda symbol: ak.stock_zh_a_hist_min_em(symbol=symbol, period=str(period), adjust="").to_dict("records")
    observed_at = datetime.now(TZ).isoformat(timespec="seconds")
    subset = {**scan, "candidates": scan.get("candidates", [])[:max_symbols]}
    if daily_data is None and market_store is not None:
        cutoff = snapshot.get("market", {}).get("latest_completed_trade_date")
        if cutoff:
            daily_data = {row["symbol"]: market_store.bars_as_known(row["symbol"], 61,
                           datetime.fromisoformat(cutoff).date(), snapshot["as_of_time"])
                          for row in subset["candidates"]}
    minute_data, errors = {}, {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = {pool.submit(minute_loader, row["symbol"]): row["symbol"] for row in subset["candidates"]}
        for future in as_completed(pending):
            symbol = pending[future]
            try:
                minute_data[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {exc}"
    if auto_fallback and not any(minute_data.values()):
        errors = {"eastmoney:" + symbol: error for symbol, error in errors.items()}
        source_name, period = "sina", 5
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {pool.submit(fetch_sina_five_minutes, row["symbol"]): row["symbol"] for row in subset["candidates"]}
            for future in as_completed(pending):
                symbol = pending[future]
                try:
                    minute_data[symbol] = future.result()
                except Exception as exc:
                    errors["sina:" + symbol] = f"{type(exc).__name__}: {exc}"
    directory = Path(output_dir) / f"experiment-{datetime.now(TZ):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    input_path = directory / "experiment-input.json"
    chosen = {row["symbol"] for row in subset["candidates"]}
    captured_snapshot = {key: snapshot.get(key) for key in ("snapshot_id", "as_of_time", "market", "historical_trade_rules") if key in snapshot}
    captured_snapshot["securities"] = [row for row in snapshot.get("securities", []) if row["symbol"] in chosen]
    input_path.write_text(json.dumps({"snapshot": captured_snapshot, "scan": subset}, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    raw = {"source": SINA_SOURCE if source_name == "sina" else SOURCE, "provider": source_name, "adjustment": "raw",
           "period_minutes": period, "requested_at": observed_at, "retrieved_at": datetime.now(TZ).isoformat(timespec="seconds"),
           "scan_id": scan.get("scan_id"), "selection_observed_at": scan.get("as_of_time"), "minutes": minute_data, "errors": errors}
    raw_path = directory / "minutes.json"
    encoded = json.dumps(raw, ensure_ascii=False, default=str, indent=2)
    raw_path.write_text(encoded, encoding="utf-8")
    report = diagnose_closing_history(snapshot, subset, minute_data, period=period, min_samples=min_samples, capital_per_trade=capital_per_trade, lookback_days=lookback_days)
    report["threshold_sensitivity"] = []
    for label, changes in (("looser", {"return_min": .005, "amount_min": 80_000_000}),
                           ("stricter", {"return_min": .02, "amount_min": 150_000_000})):
        trial = diagnose_closing_history(snapshot, subset, minute_data, period=period, min_samples=min_samples,
                                         capital_per_trade=capital_per_trade, thresholds=changes, lookback_days=lookback_days)
        report["threshold_sensitivity"].append({"label": label, "thresholds": trial["thresholds"], "sample_count": trial["sample_count"], "summary": trial["summary"]})
    report.update(raw_minutes_path=str(raw_path), raw_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                  experiment_input_path=str(input_path),
                  dataset_end_time=minute_dataset_end_time(minute_data), selection_observed_at=raw["selection_observed_at"],
                  source=raw["source"], minute_source=source_name, errors=errors, retrieved_at=raw["retrieved_at"], report_path=str(directory / "report.json"))
    if daily_data is not None:
        report["daily_proxy"] = diagnose_daily_gap_proxy(snapshot, subset, daily_data, capital_per_trade)
        daily_path = directory / "daily-proxy-input.json"
        daily_path.write_text(json.dumps(daily_data, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        report["daily_proxy"]["raw_daily_path"] = str(daily_path)
    Path(report["report_path"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
