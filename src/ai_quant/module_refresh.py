"""Bounded candidate refreshes; acquisition time never substitutes for source time."""
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from threading import Lock

import requests

from .datahub import _quote
from .features import _features
from .history import _bars
from .module_service import now_iso
from .providers import AkshareProvider


def fetch_dated_quotes(securities, get=requests.get):
    quotes, errors = {}, {}
    for offset in range(0, len(securities), 50):
        batch = securities[offset:offset + 50]
        codes = {row["symbol"]: ("sh" if row["exchange"] == "SSE" else "sz") + row["symbol"] for row in batch}
        url = "https://qt.gtimg.cn/q=" + ",".join(codes.values())
        try:
            response = get(url, timeout=20)
            response.raise_for_status()
            response.encoding = "gbk"
            fetched = now_iso()
            for line in response.text.split(";"):
                if '~' not in line:
                    continue
                fields = line.split('"')[1].split('~')
                if len(fields) < 36 or fields[2] not in codes:
                    continue
                symbol = fields[2]
                try:
                    stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                    price = float(fields[3])
                    if not math.isfinite(price) or price <= 0 or stamp > datetime.fromisoformat(fetched):
                        raise ValueError("invalid price or future source time")
                    # Field 35 includes raw turnover in CNY, unlike rounded field 37.
                    totals = fields[35].split('/')
                    amount = float(totals[2]) if len(totals) == 3 else None
                    row = {"最新价": price, "昨收": fields[4], "今开": fields[5],
                           "最高": fields[33], "最低": fields[34], "成交量": float(fields[6]) * 100,
                           "成交额": amount, "更新时间": stamp.isoformat()}
                    quotes[symbol] = {**_quote(row, fetched), "source_url": url,
                                      "source": "Tencent", "source_time_meaning": "provider_quote_update_not_individual_trade"}
                except (ValueError, IndexError) as exc:
                    errors[symbol] = str(exc)
            for symbol in codes.keys() - quotes.keys():
                errors.setdefault(symbol, "源站未返回带日期的有效报价")
        except (requests.RequestException, ValueError, IndexError) as exc:
            errors.update({symbol: str(exc) for symbol in codes})
    return {"quotes": quotes, "errors": errors, "completed_at": now_iso()}


def refresh_etf_history(snapshot, securities, store, provider_factory=AkshareProvider, workers=3):
    """Increment raw bars; refresh one coherent rolling qfq window for each ETF."""
    cutoff_text = snapshot.get("market", {}).get("latest_completed_trade_date")
    if not cutoff_text:
        return {"results": {}, "error": "缺少权威完整交易日，未请求历史或推断日期", "completed_at": now_iso()}
    cutoff = date.fromisoformat(cutoff_text)
    window_start = cutoff - timedelta(days=270)
    expected = set(snapshot["market"].get("recent_completed_trade_dates", []))
    providers = {row["symbol"]: provider_factory() for row in securities}
    raw_source_lock = Lock()

    def refresh(security):
        symbol = security["symbol"]
        result = {"symbol": symbol, "requested_through": cutoff_text, "features": {}, "warnings": []}
        provider = providers[symbol]
        prefix = "sh" if security["exchange"] == "SSE" else "sz"
        try:
            known = store.bars_as_known(symbol, 200, cutoff, now_iso())
            present = {str(bar["trade_date"]) for bar in known}
            missing = sorted(expected - present)
            latest = date.fromisoformat(known[-1]["trade_date"]) if known else None
            start = window_start if len(known) < 121 else min(date.fromisoformat(missing[0]), latest) if missing else latest
            if latest != cutoff or missing or len(known) < 121:
                # Sina's embedded JS decoder can terminate Windows Python when
                # initialized concurrently; qfq JSON requests can still overlap.
                with raw_source_lock:
                    raw = _bars(symbol, provider.fetch_daily_history(symbol, prefix, "etf", start, cutoff, "raw"), start, cutoff)
                if not raw:
                    raise ValueError("增量源未返回请求区间的原始日线")
                raw_at = now_iso()
                store.upsert_bars(raw, "raw", provider.sources.get(f"history:{symbol}:raw", provider.name), raw_at)
            raw_rows = store.bars_as_known(symbol, 200, cutoff, now_iso())
            raw_by_day = {str(bar["trade_date"]): bar for bar in raw_rows}
            result["raw_through"] = max(raw_by_day, default=None)
            result["missing_recent_dates"] = sorted(expected - raw_by_day.keys())
            if result["raw_through"] != cutoff_text or result["missing_recent_dates"]:
                raise ValueError("原始日线未覆盖完整截止日/最近交易日，不能声称已刷新")
            cached = [row for row in store.bars(symbol, "qfq", 320, cutoff)
                      if row["trade_date"] >= window_start.isoformat()]
            cache_batches = {(row["source"], row["available_at"]) for row in cached}
            cache_usable = (len(cached) >= 121 and len(cache_batches) == 1
                            and cached[-1]["trade_date"] == cutoff_text
                            and expected <= {row["trade_date"] for row in cached}
                            and datetime.fromisoformat(cached[-1]["available_at"]) <= datetime.fromisoformat(now_iso()))
            if cache_usable:
                qfq_source, qfq_at = next(iter(cache_batches))
                qfq = _bars(symbol, [{**row, "date": row["trade_date"]} for row in cached], window_start, cutoff)
                result["adjustment_cache"] = {"available_at": qfq_at, "source": qfq_source,
                                              "through": cutoff_text, "bars": len(qfq), "new_network_fetch": False}
            else:
                qfq = _bars(symbol, provider.fetch_daily_history(symbol, prefix, "etf", window_start, cutoff, "qfq"), window_start, cutoff)
                qfq_source = provider.sources.get(f"history:{symbol}:qfq", provider.name)
                qfq_at = now_iso()
            qfq = sorted(qfq, key=lambda bar: bar.trade_date)
            if not qfq or qfq[-1].trade_date != cutoff or len(qfq) < 121 or len({b.trade_date for b in qfq}) != len(qfq):
                raise ValueError("前复权滚动序列缺失、重复或不足121日，不用未复权价格替代收益指标")
            if any(not math.isfinite(b.close) or b.close <= 0 for b in qfq) or not expected <= {b.trade_date.isoformat() for b in qfq}:
                raise ValueError("前复权价格无效或缺最近交易日")
            if not cache_usable:
                store.upsert_bars(qfq, "qfq", qfq_source, qfq_at)
            rows = [{**asdict(bar), "trade_date": bar.trade_date.isoformat(),
                     "amount": raw_by_day.get(bar.trade_date.isoformat(), {}).get("amount")} for bar in qfq]
            if any(row["amount"] is None for row in rows[-20:]):
                raise ValueError("最近20日真实成交额缺失，不能伪造流动性指标")
            features = _features(rows, [])
            features.update(price_basis="provider_qfq_single_fetch_window", available_at=qfq_at,
                            adjustment_source=qfq_source,
                            last_raw_close=raw_rows[-1]["close"], previous_raw_close=raw_rows[-2]["close"],
                            corporate_actions_applied=None,
                            adjustment_note="供应商本次前复权整段序列，未与旧复权片段拼接；非历史时点可得回测数据，非已核验分红总回报")
            if cache_usable:
                features.update(price_basis="provider_qfq_cached_single_fetch_window",
                                adjustment_note="沿用同截止日、同来源和同一获取时间的完整复权窗口；available_at保留原获取时间，未重新抓取或拼接，非历史时点回测数据。")
            result.update(features=features, adjusted_bars=len(rows), adjustment_available_at=qfq_at,
                          adjustment_source=features["adjustment_source"],
                          status="cached_adjusted_window" if cache_usable else "refreshed")
        except Exception as exc:
            result.update(status="incomplete")
            result["warnings"].append(str(exc))
        return result

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 3))) as pool:
        results = list(pool.map(refresh, securities))
    return {"requested_symbols": [r["symbol"] for r in securities], "through": cutoff_text,
            "results": {r["symbol"]: r for r in results}, "completed_at": now_iso()}
