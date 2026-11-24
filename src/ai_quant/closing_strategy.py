"""Tail-session hypothesis screening, never a claim of validated overnight alpha."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time
from math import isfinite
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SOURCES = [
    {"title": "尾盘量价选股思路（社区经验，非回测证据）", "url": "https://www.tgb.cn/a/2hzumh04kqO-1"},
    {"title": "上交所交易规则（2026年修订）", "url": "https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml"},
    {"title": "AKShare 分钟行情接口", "url": "https://akshare.akfamily.xyz/data/stock/stock.html"},
]


def number(value, default=None):
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (ValueError, TypeError):
        return default


def build_closing_candidates(snapshot, market_store=None, limit=12):
    rows = snapshot.get("research_universe", [])
    candidates = []
    universe = 0
    for row in rows:
        security, features = row.get("security", {}), row.get("features", {})
        if security.get("security_type") != "stock":
            continue
        universe += 1
        if security.get("risk_warning") is True or "ST" in str(security.get("name", "")).upper() or security.get("trading_status") != "tradable":
            continue
        quote = row.get("quote", {})
        price = number(quote.get("last"))
        amount = number(features.get("amount_20d_average"), 0)
        ret20 = number(features.get("return_20d"), 0)
        ma = number(features.get("close_to_ma20"), 0)
        if not price or amount < 100_000_000 or not -.03 <= ma <= .12 or ret20 < -.08:
            continue
        score = round(50 + min(amount / 100_000_000, 20) + min(max(ret20, 0) * 100, 15), 2)
        candidates.append({**row, "price": price, "score": score, "eligible": True,
                           "signal_confirmed": False, "reasons": ["20日平均成交额至少1亿元", "价格邻近20日均线，排除明显弱势和过度乖离"],
                           "risks": ["研究准备池，需当日尾盘分钟复核", "T+1隔夜低开可能穿越止损价"],
                           "evidence_gaps": ["隔夜事件需Agent核验", "无已验证分钟历史胜率"] + (["风险警示名单尚未完整核验；仅可条件研究"] if security.get("risk_warning") is None else []),
                           "metrics": {"average_amount_20d": amount, "return_20d": ret20, "close_to_ma20": ma},
                           "history_as_of_date": features.get("history_as_of_date")})
    candidates.sort(key=lambda r: (-r["score"], r["symbol"]))
    return {"module": "closing", "as_of_time": snapshot["as_of_time"], "candidates": candidates[:limit],
            "universe_count": universe, "eligible_count": len(candidates), "excluded_count": universe - len(candidates),
            "quality": {"status": "research_only", "warnings": ["筛选阈值为待验证策略假设，不提供虚构收益率或胜率"]},
            "methodology": ["提前手动扫描和Agent讨论；14:45–14:55手动快速复核",
                            "复核日内涨幅1–5%、VWAP上方但不追高、接近截至当时高位及实际成交额",
                            "次日以高/平/低开分支退出；最迟10:00重新处理未成交，停牌/跌停不能假定卖出"],
            "sources": SOURCES}


def minute_check(candidate, rows, as_of, is_trading_day):
    now = datetime.fromisoformat(as_of).astimezone(TZ)
    by_minute = {}
    for row in rows:
        stamp = datetime.fromisoformat(str(row["time"]))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=TZ)
        if stamp.date() == now.date() and stamp <= now and time(9, 30) <= stamp.time() <= time(14, 55):
            values = [number(row.get(key)) for key in ("close", "high", "volume", "amount")]
            if all(v is not None for v in values) and values[1] >= values[0] > 0 and min(values[2:]) >= 0:
                by_minute[stamp.replace(second=0, microsecond=0)] = row
    valid = sorted(by_minute.items())
    blockers = []
    if not is_trading_day or not time(14, 45) <= now.time() <= time(14, 55):
        blockers.append("当前不在交易日14:45–14:55尾盘复核窗口；可保留条件计划")
    metrics = {}
    if len(valid) < 30:
        blockers.append("今日已观测分钟数据不足30条")
    else:
        latest, last = valid[-1]
        if (now - latest).total_seconds() > 120:
            blockers.append("分钟行情超过120秒未更新")
        volume = sum(float(r["volume"]) for _, r in valid)
        amount = sum(float(r["amount"]) for _, r in valid)
        vwap = amount / (volume * 100) if volume else 0  # provider unit: lots
        price = float(last["close"])
        previous = number(candidate.get("quote", {}).get("previous_close"))
        if not previous:
            previous = number(candidate.get("quote", {}).get("pre_close"))
        try:
            quote_time = datetime.fromisoformat(candidate.get("quote", {}).get("available_at", ""))
        except ValueError:
            quote_time = None
        if quote_time is None or quote_time.tzinfo is None or quote_time > now or quote_time.astimezone(TZ).date() != now.date():
            previous = None
        day_return = price / previous - 1 if previous else None
        high = max(float(r["high"]) for _, r in valid)
        metrics = {"price": price, "vwap": vwap, "return_today": day_return,
                   "amount_today": amount, "distance_from_high": price / high - 1,
                   "minute_as_of": latest.isoformat(), "minute_count": len(valid)}
        if day_return is None or not .01 <= day_return <= .05:
            blockers.append("日内涨幅未在1–5%或缺少有效前收盘价")
        if not vwap or not vwap <= price <= vwap * 1.025:
            blockers.append("未站稳VWAP或距VWAP超过2.5%")
        if price / high < .99:
            blockers.append("距离截至当前日内高点超过1%")
        if amount < 100_000_000:
            blockers.append("截至当前成交额不足1亿元")
    return {"symbol": candidate["symbol"], "name": candidate.get("name"),
            "checked_at": as_of, "signal_confirmed": not blockers,
            "metrics": metrics, "blockers": blockers}


def fetch_closing_minutes(candidates):
    import akshare as ak
    result, errors = {}, {}
    def fetch(row):
        frame = ak.stock_zh_a_hist_min_em(symbol=row["symbol"], period="1")
        return [{"time": r["时间"], "close": r["收盘"], "high": r["最高"],
                 "volume": r["成交量"], "amount": r["成交额"]} for r in frame.to_dict("records")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(fetch, row): row["symbol"] for row in candidates}
        for future in as_completed(pending):
            symbol = pending[future]
            try:
                result[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = str(exc)[:300]
    return result, errors
