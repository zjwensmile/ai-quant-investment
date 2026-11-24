"""Point-in-time value research candidates, independent of entry signals.

Returns are fractions; financial growth, ROE and margins are percentage points,
matching MarketStore's financial-report input. Scores are research priorities,
not probabilities, intrinsic values, or permission to buy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime
import math
import sqlite3
from typing import Any

from .market_store import MarketStore


SOURCES = [
    {"title": "MSCI Quality Indexes", "url": "https://www.msci.com/indexes/group/quality-indexes",
     "use": "ROE、盈利稳定性和杠杆是质量证据；本模块不复制指数或其阈值。"},
    {"title": "S&P 500 Quality Index", "url": "https://www.spglobal.com/spdji/en/indices/dividends-factors/sp-500-quality-index/",
     "use": "质量分析结合盈利、应计项目与杠杆；缺少债务数据不能认定财务稳健。"},
    {"title": "MSCI Enhanced Value Methodology (June 2017)", "url": "https://www.msci.com/indexes/documents/methodology/2_MSCI_Enhanced_Value_Indexes_Methodology_20170622.pdf",
     "use": "历史方法区分金融企业的现金流估值；本模块不对金融股应用工业企业 OCF 门槛。"},
]

METHODOLOGY = [
    "独立扫描完整 securities 股票集合，不使用缠论入场池；eligible 仅表示值得价值研究，不表示商业模式完好、低估或可以买入。",
    "价格承压：20 日收益<=-8%，或 60 日收益<=-12%，或当日个股<=-1%且对应指数<=-0.5%；历史最大回撤只展示，不当作当前折价。",
    "共跌仅比较同日可用报价；上海主板对应上证，深圳主板对应深证成指，创业板对应创业板指。共跌不是因果；无可比指数时只标价格回撤、市场归因待研究。",
    "经营初筛：营收和净利润为正，营收同比>=-10%、净利润同比>=-20%；非金融经营现金流每股为正且与正 EPS 之比>=0.5。每股口径差异及季节性仍需原报表核实。",
    "已知 ROE 必须为正。同报告月的上一年比较：ROE 同时下降超过 3 个百分点及 30%，或毛利率下降超过 5 个百分点则不入选；缺多期报表不宣称稳定。",
    "金融行业不使用通用 OCF/EPS 和毛利率门槛，另列资产质量、资本充足及偿付能力待核验；未知行业按普通企业规则并披露分类缺口。",
    "PB 来自当前价格/已披露每股净资产；仅明确标为 TTM 的提供方 PE 可作为 TTM。半年报年化 PE 单独标注，绝不用于 TTM 判断。估值低不等于存在安全边际。",
    "0–100 分为可解释研究排序：经营证据 40，同期共跌 15 或中期回撤 8，OCF 质量/金融 ROE 最高 15，同期 ROE/毛利稳定各 5，PB<=3 加 10、明确 TTM PE<=25 加 10。阈值是本系统启发式默认，非指数供应商标准。",
]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _known(value: Any, as_of: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.tzinfo is not None and parsed <= as_of
    except (TypeError, ValueError):
        return False


def _financial_known(row: dict, as_of: datetime) -> bool:
    try:
        return (
            date.fromisoformat(str(row.get("report_period"))) <= as_of.date()
            and date.fromisoformat(str(row.get("announcement_date"))) <= as_of.date()
            and _known(row.get("available_at"), as_of)
        )
    except (TypeError, ValueError):
        return False


def _reports(snapshot: dict, rows: dict, store: MarketStore | None, as_of: datetime) -> tuple[dict, int]:
    collected = defaultdict(list)
    for symbol, row in rows.items():
        value = row.get("fundamentals")
        if isinstance(value, dict):
            collected[symbol].append(value)
        collected[symbol].extend(row.get("fundamental_history") or [])
    for symbol, values in (snapshot.get("financial") or {}).items():
        collected[symbol].extend(values if isinstance(values, list) else [values])
    if store is not None:
        # Existing read APIs set journal mode; this scan opens an explicit read-only
        # connection and fetches multi-period evidence once for the full universe.
        uri = store.path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            for record in connection.execute("SELECT * FROM fundamental_snapshots"):
                item = dict(record)
                collected[item["symbol"]].append({k: v for k, v in item.items() if k not in ("raw_json", "record_hash")})
    result, discarded = {}, 0
    for symbol, items in collected.items():
        periods = {}
        for item in items:
            if not isinstance(item, dict) or not _financial_known(item, as_of):
                discarded += 1
                continue
            period = item["report_period"]
            old = periods.get(period)
            if old is None or datetime.fromisoformat(item["available_at"]) > datetime.fromisoformat(old["available_at"]):
                periods[period] = dict(item)
        result[symbol] = sorted(periods.values(), key=lambda x: x["report_period"], reverse=True)
    return result, discarded


def build_value_candidates(snapshot: dict, market_store: MarketStore | None = None, limit: int = 20) -> dict:
    """Build a bounded, auditable research menu without network or database writes.

    ``candidates`` contains only eligible rows, sorted by score; ``eligible_count``
    is before truncation. ``excluded_count`` counts screened-out stocks, not ETFs
    or otherwise eligible rows beyond ``limit``. Snapshot inputs are never changed.
    """
    as_of = datetime.fromisoformat(snapshot["as_of_time"])
    if as_of.tzinfo is None:
        raise ValueError("Value scan requires a timezone-aware as_of_time")
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = {item["symbol"]: item for item in snapshot.get("research_universe", [])}
    securities = {item["symbol"]: item for item in snapshot.get("securities", []) if item.get("security_type") == "stock"}
    reports, discarded = _reports(snapshot, rows, market_store, as_of)
    market = snapshot.get("market", {})
    indices = {item.get("id"): item for item in market.get("indices", []) if _known(item.get("available_at"), as_of)}
    exclusions, gaps_count = Counter(), Counter()
    candidates = []
    for symbol, security in securities.items():
        row = rows.get(symbol, {})
        features = dict(row.get("features") or snapshot.get("features", {}).get(symbol) or {})
        quote = dict(snapshot.get("quotes", {}).get(symbol) or row.get("quote") or {})
        history_date = features.get("history_as_of_date")
        failures, reasons, risks, gaps = [], [], [], ["商业模式、竞争优势及需求是否受损需一手来源研究", "债务期限、ROIC、自由现金流与长期盈利历史未完整核验"]
        if security.get("risk_warning") is True or security.get("trading_status") != "tradable":
            failures.append("security_not_eligible")
        price = _number(quote.get("last"))
        if price is None or price <= 0 or not _known(quote.get("available_at"), as_of):
            failures.append("missing_or_unavailable_quote")
        cutoff = str(market.get("latest_completed_trade_date") or as_of.date().isoformat())
        if not history_date or str(history_date) > cutoff:
            features = {}
            failures.append("missing_or_future_history")
        elif str(history_date) < cutoff:
            gaps.append("历史特征尚未覆盖最近完成交易日")
            failures.append("stale_history")
        period_reports = reports.get(symbol, [])
        fundamental = period_reports[0] if period_reports else {}
        if not fundamental:
            failures.append("missing_point_in_time_financials")
        elif (as_of.date() - date.fromisoformat(fundamental["report_period"])).days > 220:
            failures.append("stale_financial_report")
        industry = row.get("industry") or {}
        industry_text = " ".join(str(industry.get(k) or "") for k in ("section", "major", "middle", "minor")) + str(fundamental.get("industry_name") or "")
        financial = any(word in industry_text for word in ("银行", "证券", "保险", "多元金融", "非银金融", "bank", "insurance", "financial"))
        if not industry_text.strip():
            gaps.append("行业分类缺失，现金流按普通企业口径初筛")
        benchmark_id = "chinext" if security.get("board") == "chinext" or symbol.startswith(("300", "301")) else ("sse" if security.get("exchange") == "SSE" and security.get("board") != "star" else "szse" if security.get("exchange") == "SZSE" else None)
        index = indices.get(benchmark_id, {})
        benchmark_return = _number(index.get("change_percent"))
        benchmark_return = benchmark_return / 100 if benchmark_return is not None else None
        previous = _number(quote.get("previous_close"))
        daily = price / previous - 1 if price and previous and previous > 0 and _known(quote.get("available_at"), as_of) else None
        # Pre-open quotes can still carry yesterday's move; only a completed,
        # explicitly dated session supports the daily co-decline claim.
        comparable = (market.get("session") == "post_market" and market.get("is_trading_day") is True
                      and cutoff == as_of.date().isoformat() and str(history_date) == cutoff
                      and _known(index.get("available_at"), as_of)
                      and datetime.fromisoformat(index["available_at"]).astimezone(as_of.tzinfo).date() == as_of.date()
                      and _known(quote.get("available_at"), as_of)
                      and datetime.fromisoformat(quote["available_at"]).astimezone(as_of.tzinfo).date() == as_of.date())
        co_decline = bool(comparable and daily is not None and benchmark_return is not None and daily < 0 and benchmark_return < 0)
        r20, r60 = _number(features.get("return_20d")), _number(features.get("return_60d"))
        pressure = (r20 is not None and r20 <= -.08) or (r60 is not None and r60 <= -.12) or (co_decline and daily <= -.01 and benchmark_return <= -.005)
        if not pressure:
            failures.append("no_price_pressure")
        if co_decline:
            reasons.append("当日与对应指数同时下跌；仅同期现象，不证明市场导致下跌")
        else:
            gaps.append("缺少同期间指数共跌证据，不能认定被大盘带跌")
        if pressure:
            reasons.append("价格承压满足研究门槛，尚未证明低估")
        values = {key: _number(fundamental.get(key)) for key in ("revenue", "net_profit", "revenue_growth", "net_profit_growth", "earnings_per_share", "operating_cash_flow_per_share", "return_on_equity", "gross_margin", "book_value_per_share")}
        for key, minimum in (("revenue", 0), ("net_profit", 0), ("revenue_growth", -10), ("net_profit_growth", -20)):
            val = values[key]
            if val is None:
                failures.append("missing_" + key)
                gaps.append("缺少 " + key)
            elif (key in ("revenue", "net_profit") and val <= minimum) or val < minimum:
                failures.append("weak_" + key)
        eps, ocf = values["earnings_per_share"], values["operating_cash_flow_per_share"]
        cash_conversion = ocf / eps if eps is not None and eps > 0 and ocf is not None else None
        if financial:
            gaps.append("金融企业需核验资产质量、拨备、资本充足率或偿付能力；通用经营现金流比率不适用")
        elif cash_conversion is None:
            failures.append("missing_cash_quality")
            gaps.append("经营现金流/EPS 质量证据不足")
        elif ocf <= 0 or cash_conversion < .5:
            failures.append("weak_operating_cash_flow")
        else:
            risks.append("OCF/EPS 为每股近似，须核对股数口径、季节性及非经常项目")
        roe = values["return_on_equity"]
        if roe is None:
            gaps.append("ROE 缺失")
        elif roe <= 0:
            failures.append("nonpositive_roe")
        comparable_report = next((old for old in period_reports[1:] if old["report_period"][5:] == fundamental["report_period"][5:] and int(old["report_period"][:4]) == int(fundamental["report_period"][:4]) - 1), None)
        stability = {}
        for key, drop in (("return_on_equity", 3), ("gross_margin", 5)):
            if financial and key == "gross_margin":
                continue
            current = values[key]
            prior = _number(comparable_report.get(key)) if comparable_report else None
            delta = current - prior if current is not None and prior is not None else None
            stability[key + "_yoy_change_pp"] = delta
            if delta is None:
                gaps.append(key + " 缺少同报告期跨年比较，不能认定稳定")
            elif delta < -drop and (key != "return_on_equity" or prior > 0 and delta / prior < -.3):
                failures.append("deteriorating_" + key)
        bvps = values["book_value_per_share"]
        pb = price / bvps if price and bvps and bvps > 0 else None
        valuation_quote = quote
        row_quote = row.get("quote") or {}
        if quote.get("pe_ratio") is None and _known(row_quote.get("available_at"), as_of):
            valuation_quote = row_quote
        supplied_pe = _number(valuation_quote.get("pe_ratio"))
        pe_method = valuation_quote.get("pe_ratio_method")
        ttm_pe = supplied_pe if pe_method in ("ttm", "price_divided_by_ttm_eps") else None
        if pb is None:
            gaps.append("缺少有效每股净资产，PB 不可计算")
        if ttm_pe is None:
            gaps.append("缺少确认口径的 TTM PE；单期年化 PE 不替代 TTM")
        gaps.append("缺少同行估值分布及内在价值测算，尚未证明安全边际")
        metrics = {**values, "return_1d": daily, "return_20d": r20, "return_60d": r60,
                   "maximum_drawdown_120d": features.get("maximum_drawdown_120d"),
                   "benchmark_id": benchmark_id, "benchmark_return_1d": benchmark_return,
                   "relative_return_1d": daily - benchmark_return if comparable and daily is not None and benchmark_return is not None else None,
                   "daily_co_decline": co_decline, "cash_conversion_per_share": None if financial else cash_conversion,
                   "financial_sector": financial, "pb_ratio": pb, "pe_ttm": ttm_pe,
                   "reported_pe": supplied_pe, "reported_pe_method": pe_method or "unspecified",
                   **stability}
        gaps_count.update(gaps)
        if failures:
            exclusions.update(set(failures))
            continue
        reasons.append("最新可用财报营收、利润及已知质量指标通过初筛；持续性仍待研究")
        score = 40 + (15 if co_decline else 8)
        score += min(15, max(0, (roe or 0) * 1.5)) if financial else min(15, cash_conversion * 10)
        score += sum(5 for delta in stability.values() if delta is not None and delta >= -1)
        score += 10 if pb is not None and 0 < pb <= 3 else 0
        score += 10 if ttm_pe is not None and 0 < ttm_pe <= 25 else 0
        candidates.append({"symbol": symbol, "name": security.get("name", symbol), "security_type": "stock",
                           "price": price, "score": round(min(100, score), 2), "eligible": True,
                           "reasons": reasons, "risks": risks, "metrics": metrics, "evidence_gaps": gaps,
                           "history_as_of_date": history_date, "security": dict(security), "features": features,
                           "quote": quote, "industry": industry, "fundamentals": fundamental,
                           "fundamental_context": {"latest": fundamental, "reports": period_reports,
                               "comparable_prior_year": comparable_report, "financial_sector": financial,
                               "stability": stability, "evidence_gaps": gaps}})
    candidates.sort(key=lambda item: (-item["score"], item["symbol"]))
    return {"module": "value", "as_of_time": snapshot["as_of_time"], "candidates": candidates[:limit],
            "universe_count": len(securities), "eligible_count": len(candidates),
            "excluded_count": len(securities) - len(candidates), "truncated_count": max(0, len(candidates) - limit),
            "quality": {"status": "research_only", "financial_records_rejected_by_time": discarded,
                "stocks_with_features": sum(bool(rows.get(s, {}).get("features")) for s in securities),
                "stocks_with_financials": sum(bool(reports.get(s)) for s in securities),
                "exclusion_reasons": dict(sorted(exclusions.items())), "evidence_gaps": dict(gaps_count),
                "co_decline_eligible_count": sum(c["metrics"]["daily_co_decline"] for c in candidates),
                "universe_scope": "snapshot securities stocks; not all A shares and not the Chanlun pool"},
            "methodology": list(METHODOLOGY), "sources": [dict(item) for item in SOURCES]}


def build_growth_candidates(snapshot: dict, market_store: MarketStore | None = None,
                            limit: int = 20, priority_symbols=()) -> dict:
    """Rank company research evidence without imposing price or entry conditions.

    Priority requests reserve discussion places, not quality endorsements. All
    scores use only point-in-time reports; financial growth units are percentage
    points. Permission and position sizing belong to the execution layer.
    """
    as_of = datetime.fromisoformat(snapshot["as_of_time"])
    if as_of.tzinfo is None:
        raise ValueError("Growth scan requires a timezone-aware as_of_time")
    if limit < 1:
        raise ValueError("limit must be positive")
    priorities = {str(symbol): index for index, symbol in enumerate(dict.fromkeys(priority_symbols))}
    rows = {item["symbol"]: item for item in snapshot.get("research_universe", [])}
    securities = {item["symbol"]: item for item in snapshot.get("securities", [])
                  if item.get("security_type") == "stock"}
    known_nonstocks = {item["symbol"] for item in snapshot.get("securities", [])
                       if item.get("security_type") != "stock"}
    for symbol, row in rows.items():
        security = row.get("security") or {}
        if symbol not in known_nonstocks and security.get("security_type") == "stock":
            securities.setdefault(symbol, dict(security, symbol=symbol))
    for symbol in priorities:
        if symbol not in known_nonstocks:
            securities.setdefault(symbol, {"symbol": symbol, "security_type": "stock",
                                           "metadata_missing": True})
    reports, discarded = _reports(snapshot, rows, market_store, as_of)
    candidates, exclusions, gaps_count = [], Counter(), Counter()
    for symbol, security in securities.items():
        row = rows.get(symbol, {})
        period_reports = reports.get(symbol, [])
        fundamental = period_reports[0] if period_reports else {}
        period = fundamental.get("report_period")
        fresh = bool(period and (as_of.date() - date.fromisoformat(period)).days <= 220)
        prior = next((item for item in period_reports[1:]
                      if item["report_period"][5:] == period[5:]
                      and int(item["report_period"][:4]) == int(period[:4]) - 1), None)
        keys = ("revenue", "net_profit", "revenue_growth", "net_profit_growth",
                "return_on_equity", "gross_margin", "earnings_per_share",
                "operating_cash_flow_per_share", "book_value_per_share")
        values = {key: _number(fundamental.get(key)) for key in keys}
        gaps = ["商业模式、竞争优势、未来需求、客户集中度与订单兑现尚需一手来源核验",
                "债务期限、ROIC、自由现金流和估值安全边际未完整核验；可研究不等于可买"]
        risks, reasons = [], []
        if not fundamental:
            gaps.append("缺少截至研究时点已披露的财务报告")
        elif not fresh:
            gaps.append("最新可用财报已超过220天，成长证据过期")
        for key in keys:
            if values[key] is None:
                gaps.append("缺少 " + key)
        industry = row.get("industry") or {}
        industry_text = " ".join(str(industry.get(k) or "") for k in ("section", "major", "middle", "minor")) + str(fundamental.get("industry_name") or "")
        financial = any(word in industry_text.lower() for word in
                        ("银行", "证券", "保险", "多元金融", "非银金融", "bank", "insurance", "financial"))
        if not industry_text.strip():
            gaps.append("行业分类缺失，现金流暂按普通企业口径观察")
        growth = {}
        for key, supplied in (("revenue", "revenue_growth"), ("net_profit", "net_profit_growth")):
            current = values[key]
            base = _number(prior.get(key)) if prior else None
            verified = (current / base - 1) * 100 if current is not None and base is not None and base > 0 else None
            growth[supplied + "_base"] = base
            growth[supplied + "_verified"] = verified
            growth[supplied + "_basis"] = "positive_prior_year_same_period" if verified is not None else "unverified_provider_yoy"
            # A known nonpositive base cannot establish ordinary earnings growth.
            growth[supplied + "_used"] = verified if verified is not None else values[supplied] if base is None else None
            if base is None:
                gaps.append(key + " 同比缺少上年同报告期基数，提供方同比尚未核实")
            elif base <= 0:
                risks.append(key + " 上年同期基数非正，增长百分比不能证明持续成长")
            if verified is not None and values[supplied] is not None and abs(verified - values[supplied]) > 5:
                risks.append(supplied + " 提供方同比与同报告期金额计算不一致，以金额计算初筛并待核对报表口径")
        revenue_growth = growth["revenue_growth_used"]
        profit_growth = growth["net_profit_growth_used"]
        qualifies = fresh and (values["revenue"] or 0) > 0 and revenue_growth is not None and revenue_growth >= 10
        priority = symbol in priorities
        if not qualifies and not priority:
            exclusions.update(["missing_or_stale_financials" if not fresh else "insufficient_revenue_growth_evidence"])
            gaps_count.update(gaps)
            continue
        if priority:
            reasons.append("用户点名优先讨论；不提高质量分，也不构成买入建议")
        if qualifies:
            reasons.append("最新可用报告营收为正且同比增长至少10%，进入成长研究")
        profit, roe = values["net_profit"], values["return_on_equity"]
        eps, ocf = values["earnings_per_share"], values["operating_cash_flow_per_share"]
        conversion = ocf / eps if not financial and eps is not None and eps > 0 and ocf is not None else None
        if financial:
            gaps.append("金融企业不套用工业企业OCF/EPS；需核验资产质量、资本充足及偿付能力")
        elif ocf is not None and ocf <= 0:
            risks.append("经营现金流非正，需核验回款、营运资本与融资依赖")
        elif conversion is not None and conversion < .5:
            risks.append("经营现金流/EPS低于0.5，盈利现金含量偏弱")
        if not financial:
            gaps.append("OCF/EPS为每股近似，需核对股数口径、季节性和非经常损益")
        if profit is not None and profit <= 0:
            risks.append("仍处亏损阶段，只作为高风险成长研究，不能认定优质盈利公司")
        if profit_growth is not None and profit_growth < 0:
            risks.append("净利润同比下滑，营收扩张尚未兑现为利润成长")
        if roe is not None and roe <= 0:
            risks.append("报告期ROE非正")
        stability = {}
        for key, threshold in (("return_on_equity", 3), ("gross_margin", 5)):
            previous = _number(prior.get(key)) if prior else None
            delta = values[key] - previous if values[key] is not None and previous is not None else None
            stability[key + "_yoy_change_pp"] = delta
            if delta is None:
                gaps.append(key + " 缺少同报告期跨年比较，不能认定稳定")
            elif delta < -threshold:
                risks.append(key + " 较上年同报告期明显下降")
        gaps.append("ROE仅为所示报告期间原值，不将半年或季度ROE当作全年；历史趋势不能证明未来稳定")
        # Compare consecutive years for the same reporting period, never H1 to FY.
        comparable = [item for item in period_reports if period and item["report_period"][5:] == period[5:]]
        comparisons = []
        for current, previous in zip(comparable, comparable[1:]):
            if int(current["report_period"][:4]) != int(previous["report_period"][:4]) + 1:
                continue
            comparison = {"report_period": current["report_period"], "base_period": previous["report_period"]}
            for key in ("revenue", "net_profit"):
                amount, base = _number(current.get(key)), _number(previous.get(key))
                comparison[key + "_yoy"] = (amount / base - 1) * 100 if amount is not None and base is not None and base > 0 else None
            for key in ("gross_margin", "return_on_equity"):
                amount, base = _number(current.get(key)), _number(previous.get(key))
                comparison[key + "_change_pp"] = amount - base if amount is not None and base is not None else None
            comparisons.append(comparison)
        sustained = len(comparisons) >= 2 and all(
            item["revenue_yoy"] is not None and item["revenue_yoy"] >= 10
            and item["net_profit_yoy"] is not None and item["net_profit_yoy"] > 0
            for item in comparisons[:2]) and comparisons[0]["report_period"] == period and (
                comparisons[0]["base_period"] == comparisons[1]["report_period"])
        prior_profit = growth["net_profit_growth_base"]
        prior_revenue = growth["revenue_growth_base"]
        low_base = (prior_profit is not None and prior_profit <= 0) or (
            prior_profit is not None and prior_revenue is not None and prior_revenue > 0
            and prior_profit / prior_revenue < .03 and (profit_growth or 0) >= 100) or (
            (profit_growth or 0) >= 100 and (profit_growth or 0) > max(0, revenue_growth or 0) * 2)
        industry_group = str(fundamental.get("industry_name") or
                             next((industry.get(key) for key in ("middle", "major", "minor", "section") if industry.get(key)), "")).strip() or None
        cyclical = any(word in industry_text for word in ("煤炭", "石油", "钢铁", "有色", "黄金", "采矿", "化肥", "基础化工"))
        exceptional_growth = (revenue_growth or 0) >= 200 and (profit_growth or 0) >= 200
        three_years = len(comparable) >= 3 and all(
            int(comparable[index]["report_period"][:4]) == int(comparable[index + 1]["report_period"][:4]) + 1
            for index in (0, 1))
        growth_type = ("evidence_pending" if not qualifies or profit is None else
                       "loss_making_growth" if profit <= 0 else
                       "cyclical_or_low_base_recovery" if low_base or cyclical else
                       "sustained_profitable_growth" if sustained else
                       "profitable_growth_evidence_pending" if not three_years else "profitability_improvement")
        if low_base or cyclical:
            risks.append("周期行业或低基数修复启发式标记：高同比不能证明持续优质成长，需拆分价格、销量、重组及非经常损益")
        if exceptional_growth:
            gaps.append("营收与利润同比同时至少200%，需核实并表、重组、口径及有机增长；仅为异常增速疑点，不断言已发生重组或低基数，也不因此降级")
        if not sustained:
            gaps.append("尚未取得连续两次同报告期年度营收和利润同步增长证据，不认定持续盈利成长")
        thesis = {
            "sustained_profitable_growth": "同报告期连续增长能否由销量、产品升级和客户拓展延续，并保持毛利与现金回款？",
            "profitability_improvement": "营收扩张能否持续转化为扣非利润和现金流，改善来自经营效率还是一次性因素？",
            "profitable_growth_evidence_pending": "已披露营收成长能否由产品、订单和客户拓展延续？补全历史财报与前瞻业务证据，不因缺第三年记录否定潜力。",
            "loss_making_growth": "收入成长能否形成可验证的盈亏平衡路径，资金是否足以支持达到该节点？",
            "cyclical_or_low_base_recovery": "剔除商品涨价、低基数、重组及一次性收益后，正常化利润是否仍能增长？",
            "evidence_pending": "补全财报及同比基数后，是否存在可验证的经营成长？",
        }[growth_type]
        quality_caution = (not financial and ((ocf is not None and ocf <= 0) or
                           (conversion is not None and conversion < .5))) or any(
            stability[key + "_yoy_change_pp"] is not None and stability[key + "_yoy_change_pp"] < -threshold
            for key, threshold in (("return_on_equity", 3), ("gross_margin", 5)))
        components = {
            "revenue_growth": min(20, max(0, revenue_growth or 0) * .4) if fresh else 0,
            "profitable": 15 if fresh and profit is not None and profit > 0 else 0,
            "profit_growth": min(10, max(0, profit_growth or 0) * .2) if fresh and profit is not None and profit > 0 else 0,
            "multiyear_growth_evidence": 15 if fresh and sustained and not low_base and not cyclical else 0,
            "positive_period_roe": min(10, max(0, roe or 0)) if fresh else 0,
            "cash_quality": min(20, max(0, conversion) * 20) if fresh and conversion is not None else 0,
            "comparable_evidence": 10 if fresh and all(v is not None and v >= -1 for v in stability.values()) else 0,
            "risk_penalty": -min(40, len(risks) * 5),
        }
        quote = dict(snapshot.get("quotes", {}).get(symbol) or row.get("quote") or {})
        if not _known(quote.get("available_at"), as_of):
            quote = {}
        price = _number(quote.get("last"))
        if price is None or price <= 0:
            price = None
            gaps.append("缺少研究时点有效报价，不影响公司研究，但无法执行交易或评估当前估值")
        trade_blocks = []
        if security.get("risk_warning") is True:
            trade_blocks.append("risk_warning")
        if security.get("trading_status") != "tradable":
            trade_blocks.append("not_tradable_or_unknown")
        if price is None:
            trade_blocks.append("missing_quote")
        if trade_blocks:
            risks.append("ST/风险警示、停牌或交易信息缺失时不可视为可执行标的：" + ", ".join(trade_blocks))
        if security.get("metadata_missing"):
            gaps.append("点名代码缺少证券元数据，名称、证券类型和上市状态待核验")
        status = ("priority_evidence_pending" if not qualifies else
                  "loss_making_growth_research" if profit is not None and profit <= 0 else
                  "profitable_growth_research" if profit is not None and profit > 0 else "growth_evidence_pending")
        features = dict(row.get("features") or snapshot.get("features", {}).get(symbol) or {})
        metrics = {**values, **growth, **stability, "roe_report_period": period,
                   "roe_annualized": False, "financial_sector": financial,
                   "cash_conversion_per_share": conversion, "daily_co_decline": False,
                   "same_period_growth_comparisons": comparisons,
                   "low_base_recovery_flag": low_base, "cyclical_industry_flag": cyclical,
                   "exceptional_growth_comparability_flag": exceptional_growth}
        gaps_count.update(gaps)
        candidates.append({"symbol": symbol, "name": security.get("name", symbol), "security_type": "stock",
                           "price": price, "score": round(max(0, min(100, sum(components.values()))), 2),
                           "score_components": components, "eligible": True, "execution_eligible": False,
                           "execution_blocks": trade_blocks, "research_status": status,
                           "priority_requested": priority,
                           "growth_type": growth_type, "industry_group": industry_group,
                           "quality_caution": quality_caution,
                           "growth_thesis_to_verify": thesis,
                           "expectation_gap_to_verify": "对照当前估值与可验证订单、产能、竞争及正常化利润情景，检验市场隐含预期；尚无证据证明市场未发现或低估。",
                           "candidate_source": "user_priority" if priority else "snapshot_financial_growth_scan",
                           "reasons": reasons, "risks": risks, "metrics": metrics, "evidence_gaps": gaps,
                           "history_as_of_date": features.get("history_as_of_date"), "features": features,
                           "security": dict(security), "quote": quote, "industry": industry,
                           "fundamentals": fundamental, "fundamental_context": {
                               "latest": fundamental, "reports": period_reports, "comparable_prior_year": prior,
                               "financial_sector": financial, "stability": stability, "evidence_gaps": gaps}})
    growth_order = {"sustained_profitable_growth": 0, "profitability_improvement": 1, "profitable_growth_evidence_pending": 1,
                    "cyclical_or_low_base_recovery": 2, "loss_making_growth": 3, "evidence_pending": 4}
    candidates.sort(key=lambda item: (
        priorities.get(item["symbol"], len(priorities)), item["quality_caution"],
        growth_order[item["growth_type"]], -item["score"],
        -date.fromisoformat(item["metrics"]["roe_report_period"]).toordinal()
        if item["metrics"]["roe_report_period"] else 0,
        -item["metrics"]["return_on_equity"] if item["metrics"]["return_on_equity"] is not None else math.inf,
        item["symbol"],
    ))
    # Reserve breadth in the first discussion batch; unknown sectors remain unknown.
    selected, deferred, industry_counts = [], [], Counter()
    for item in candidates:
        group = item["industry_group"]
        if len(selected) < min(12, limit) and not item["priority_requested"] and group and industry_counts[group] >= 2:
            deferred.append(item)
        else:
            selected.append(item)
            if group:
                industry_counts[group] += 1
    head_size = min(12, limit)
    candidates = selected[:head_size] + deferred + selected[head_size:]
    return {"module": "value", "strategy": "quality_growth_research", "as_of_time": snapshot["as_of_time"],
            "candidates": candidates[:limit], "universe_count": len(securities), "eligible_count": len(candidates),
            "excluded_count": len(securities) - len(candidates), "truncated_count": max(0, len(candidates) - limit),
            "quality": {"status": "research_only", "financial_records_rejected_by_time": discarded,
                        "stocks_with_features": sum(bool(rows.get(s, {}).get("features")) for s in securities),
                        "stocks_with_financials": sum(bool(reports.get(s)) for s in securities),
                        "exclusion_reasons": dict(exclusions), "evidence_gaps": dict(gaps_count),
                        "co_decline_eligible_count": 0,
                        "universe_scope": "snapshot stocks and explicit research priorities; not all A shares",
                        "priority_symbols_not_stock": sorted(set(priorities) & known_nonstocks)},
            "methodology": [
                "成长公司研究独立于入场池、下跌、均线、历史日线及账户交易权限；eligible仅代表研究讨论资格。",
                "自动发现：最新已披露财报距今不超过220天，营收为正、营收同比至少10%；亏损成长单列高风险研究。点名优先并保留证据缺口。",
                "同比优先使用上年同报告期正基数计算；缺同期报告时提供方同比仅作未核验初筛；非正基数不能证明普通盈利增长。",
                "研究评分0–100：营收增长20、盈利15、利润增长10、连续两次同报告期年度增长证据15、报告期ROE每1个百分点贡献1分（0至10分，不年化）、现金含量20、同期稳定证据10；每项经营风险扣5分至多40。启发式非收益预测，不同报告期间分数不可直接作为质量比较。",
                "先后置现金质量偏弱或同期毛利下降超过5个百分点、ROE下降超过3个百分点者，再按持续盈利成长、盈利改善或成长证据待补、周期或低基数修复、亏损成长、缺有效经营证据分型；同型按分数、最新报告期、同报告期ROE排序。连续成长要求连续三年同报告期收入至少增长10%且利润增长；缺第三年不否定未来潜力，改善仅为待证假设。正基数低净利率小于3%且利润翻倍，或利润翻倍且增速超过营收两倍，标记低基数疑点。商品周期分类仅据已有行业关键词，非确证。",
                "仅显式请求保留点名顺序；默认独立发现。前12个讨论位优先每个已知行业至多2家公司，再以其余候选补足；行业缺失不猜测或强行归组，覆盖不足时不承诺多行业。分型、覆盖和ROE均为研究启发式。价格不影响质量分，不证明合理买价、安全边际或市场尚未发现。金融企业不套用OCF/EPS。",
                "营收及利润同比同时至少200%仅增加口径待核标记，不改变分型或评分；异常增速须由后续研究区分真实早期增长、并表及重组。",
            ], "sources": [dict(item) for item in SOURCES[:2]]}
