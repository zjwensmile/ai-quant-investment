"""Bounded public ETF evidence, without inferred undisclosed holdings or live NAV."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations
import re
from zoneinfo import ZoneInfo

from ..etf_strategy import etf_buy_excluded, etf_market_region


# Exact aliases only; domestic manufacturing cannot be split into technology.
SECTOR_LABELS = {
    "信息技术": "technology", "资讯科技": "technology",
    "金融": "financials", "金融业": "financials",
    "医疗保健": "healthcare", "医疗保健业": "healthcare", "保健": "healthcare",
    "电信业务": "communications", "通讯服务": "communications", "通讯业务": "communications", "通信服务": "communications",
    "日常消费品": "consumer_staples", "日常消费": "consumer_staples", "必需消费品": "consumer_staples",
    "非日常生活消费品": "consumer_discretionary", "非必需性消费": "consumer_discretionary",
    "非必需消费品": "consumer_discretionary",
    "能源": "energy", "能源业": "energy", "工业": "industrials",
    "公用事业": "utilities", "原材料": "materials", "原材料业": "materials", "材料": "materials",
    "房地产": "real_estate", "房地产业": "real_estate",
}


def _number(value):
    try:
        result = Decimal(str(value).replace(",", "").replace("%", ""))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _coverage(rows):
    covered = sum((Decimal(row["weight"]) for row in rows), Decimal(0))
    if covered > Decimal("1.005"):
        raise ValueError("disclosed_weights_exceed_net_assets")
    return {"disclosed_weight": str(covered), "undisclosed_or_non_equity_weight": str(max(Decimal(0), 1-covered)),
            "complete_portfolio_verified": False,
            "coverage_note": "残余可能包括未披露股票、现金或其他资产，不能当成零暴露；权重未归一化。"}


def parse_holdings(html, symbol, observed_at):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    periods = []
    for heading in soup.select("h4.t"):
        matched = re.search(r"截止至[：:]?\s*(\d{4}-\d{2}-\d{2})", heading.get_text(" ", strip=True))
        if not matched or matched[1] > observed_at[:10]:
            continue
        table = heading.find_next("table")
        if table is None:
            continue
        headers = [re.sub(r"\s+", "", cell.get_text()) for cell in table.select("thead th")]
        if not {"股票代码", "股票名称", "占净值比例"}.issubset(headers):
            continue
        rows, seen = [], set()
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td")
            if len(cells) != len(headers):
                continue
            data = dict(zip(headers, cells))
            weight = _number(data["占净值比例"].get_text())
            if weight is None or weight < 0:
                raise ValueError("invalid_disclosed_holding_weight")
            code = data["股票代码"].get_text(strip=True)
            link = data["股票代码"].find("a", href=True)
            identity = re.search(r"/r/([0-9]+\.[A-Za-z0-9._-]+)", link["href"]) if link else None
            key = identity[1].upper() if identity else None
            if key and key in seen:
                raise ValueError("duplicate_disclosed_security")
            seen.add(key)
            rows.append({"security_id": key, "symbol": code, "name": data["股票名称"].get_text(strip=True),
                         "weight": str(weight / 100)})
        if rows:
            periods.append((matched[1], rows))
    if not periods:
        raise ValueError("no_dated_stock_holdings_available")
    period, rows = max(periods, key=lambda item: item[0])
    return {"report_date": period, "available_at": observed_at, "source_url": f"https://fundf10.eastmoney.com/ccmx_{symbol}.html",
            "source_quality": "public_fund_report_aggregator", "holdings": rows, **_coverage(rows),
            "identity_note": "仅按来源证券市场代码匹配；不同股类及A/H同发行人未合并，不代表发行人层面的全部重叠。"}


def parse_industries(payload, symbol, observed_at):
    data = payload.get("Data") or {}
    if str(data.get("FundCode")) != symbol:
        raise ValueError("industry_fund_identity_mismatch")
    periods = [q for q in data.get("QuarterInfos", []) if q.get("JZRQ", "9999") <= observed_at[:10] and q.get("HYPZInfo")]
    if not periods:
        raise ValueError("no_dated_industry_allocation_available")
    quarter = max(periods, key=lambda q: q["JZRQ"])
    rows = []
    for item in quarter["HYPZInfo"]:
        if str(item.get("BZDM")) != symbol or str(item.get("FSRQ"))[:10] != quarter["JZRQ"]:
            raise ValueError("industry_report_identity_or_period_mismatch")
        weight = _number(item.get("ZJZBL"))
        if weight is None or weight < 0:
            raise ValueError("invalid_industry_weight")
        name = str(item.get("HYMC", ""))
        rows.append({"industry_code": item.get("HYDM"), "industry_name": name,
                     "sector": SECTOR_LABELS.get(name), "weight": str(weight / 100)})
    return {"report_date": quarter["JZRQ"], "available_at": observed_at,
            "source_url": f"https://fundf10.eastmoney.com/hytz_{symbol}.html", "industries": rows,
            "mapped_sector_weight": str(sum((Decimal(r["weight"]) for r in rows if r["sector"]), Decimal(0))),
            "unmapped_disclosed_sector_weight": str(sum((Decimal(r["weight"]) for r in rows if not r["sector"]), Decimal(0))),
            "classification_note": "保留原行业口径，仅精确别名可比；未映射行业不推算成科技等细分行业。",
            **_coverage(rows)}


def nav_comparison(payload, symbol, quote, region, observed_at, quote_source=None):
    rows = [r for r in (payload.get("Data") or {}).get("LSJZList", [])
            if r.get("FSRQ", "9999") <= observed_at[:10] and (_number(r.get("DWJZ")) or 0) > 0]
    if not rows:
        raise ValueError("reported_unit_nav_unavailable")
    nav = max(rows, key=lambda row: row["FSRQ"])
    result = {"nav_date": nav["FSRQ"], "unit_nav": str(_number(nav["DWJZ"])), "available_at": observed_at,
              "source_url": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html", "nav_basis": "reported_unit_nav_not_accumulated",
              "quote_price": quote.get("last"), "quote_trade_date": quote.get("source_trade_date"),
              "quote_source_at": quote.get("source_quote_at"), "quote_source": quote_source,
              "execution_usable": False, "premium_rate": None, "price_to_dated_nav_gap": None}
    price = _number(quote.get("last"))
    if price is None or price <= 0:
        result["status"] = "quote_price_unavailable"
    elif quote.get("source_trade_date") != nav["FSRQ"]:
        result["status"] = "quote_date_unknown_or_different_from_nav"
    elif region != "a_share":
        result["status"] = "cross_border_or_other_asset_valuation_not_synchronous"
        result["price_to_dated_nav_gap"] = str(price / _number(nav["DWJZ"]) - 1)
        result["note"] = "境外资产、外汇与沪深收盘错时；同一日期不代表同一估值时点，差值不是实时溢价或交易门槛。"
    else:
        stamp = quote.get("source_quote_at")
        parsed = datetime.fromisoformat(stamp) if stamp else None
        local = parsed.astimezone(ZoneInfo("Asia/Shanghai")) if parsed and parsed.tzinfo else None
        close_quote = (local and local.date().isoformat() == nav["FSRQ"]
                       and local.strftime("%H:%M") >= "15:00"
                       and parsed <= datetime.fromisoformat(observed_at))
        if not close_quote:
            result["status"] = "same_date_but_closing_quote_unverified"
        else:
            result["status"] = "historical_same_day_close_nav_comparison"
            result["price_to_dated_nav_gap"] = str(price / _number(nav["DWJZ"]) - 1)
            result["note"] = "同日收盘价与已披露单位净值比较，仅为历史证据，不能替代下次开盘实时溢价。"
    return result


def pairwise_overlaps(funds):
    result = []
    for (left, a), (right, b) in combinations(funds.items(), 2):
        holdings_a, holdings_b = a.get("holdings", {}), b.get("holdings", {})
        row = {"symbols": [left, right], "holdings_overlap_lower_bound": None,
               "full_portfolio_overlap": None, "common_holdings": [], "sector_overlap_lower_bound": None,
               "report_dates": [holdings_a.get("report_date"), holdings_b.get("report_date")],
               "limitations": ["未披露权重不能视为零；股类重叠不等于同发行人或风险因子重叠。"]}
        if holdings_a and holdings_b and holdings_a.get("report_date") == holdings_b.get("report_date"):
            aa = {r["security_id"]: r for r in holdings_a["holdings"] if r.get("security_id")}
            bb = {r["security_id"]: r for r in holdings_b["holdings"] if r.get("security_id")}
            common = [{"security_id": key, "name": aa[key]["name"], "left_weight": aa[key]["weight"],
                       "right_weight": bb[key]["weight"], "minimum_weight": str(min(Decimal(aa[key]["weight"]), Decimal(bb[key]["weight"])))}
                      for key in sorted(aa.keys() & bb.keys())]
            row.update(common_holdings=common, holdings_overlap_lower_bound=str(sum((Decimal(r["minimum_weight"]) for r in common), Decimal(0))))
        else:
            row["limitations"].append("持仓报告期不同或缺失，未生成同步重叠比例。")
        ia, ib = a.get("industries", {}), b.get("industries", {})
        if ia and ib and ia.get("report_date") == ib.get("report_date"):
            def sectors(items):
                weights = {}
                for item in items:
                    if item.get("sector"):
                        weights[item["sector"]] = weights.get(item["sector"], Decimal(0)) + Decimal(item["weight"])
                return weights
            sa, sb = sectors(ia["industries"]), sectors(ib["industries"])
            row["common_sectors"] = [{"sector": key, "left_weight": str(sa[key]), "right_weight": str(sb[key])} for key in sorted(sa.keys() & sb.keys())]
            row["sector_overlap_lower_bound"] = str(sum((min(sa[key], sb[key]) for key in sa.keys() & sb.keys()), Decimal(0)))
            row["sector_report_date"] = ia["report_date"]
            row["limitations"].append("行业重叠仅按可映射披露类别计算，不能代替个股持仓交集；未映射类别仍未知。")
        result.append(row)
    return result


def compact_etf_evidence(snapshot):
    """Trim display rows only after full-input coverage and intersection calculation."""
    funds = {}
    for symbol, full in snapshot.get("etf_evidence", {}).items():
        item = dict(full)
        if full.get("holdings"):
            holdings = full["holdings"]
            rows = sorted(holdings["holdings"], key=lambda row: Decimal(row["weight"]), reverse=True)
            item["holdings"] = {**holdings, "holdings": rows[:10], "disclosed_holding_count": len(rows),
                                "display_truncated_count": max(0, len(rows)-10)}
        funds[symbol] = item
    overlaps = []
    for full in snapshot.get("etf_overlap_evidence", []):
        rows = sorted(full.get("common_holdings", []), key=lambda row: Decimal(row["minimum_weight"]), reverse=True)
        overlaps.append({**full, "common_holdings": rows[:10], "common_holding_count": len(rows),
                         "display_truncated_count": max(0, len(rows)-10)})
    return funds, overlaps


def enrich_etf_evidence(snapshot, symbols, *, timeout=10, max_symbols=12):
    """Return a snapshot copy with etf_evidence and etf_overlap_evidence.

    At most 3 bounded requests per allowed listed symbol; caller freezes as_of_time
    after capture. No account writes, cache records or inferred live premium.
    """
    import requests
    from akshare.utils import demjson
    securities = {row["symbol"]: row for row in snapshot.get("securities", [])}
    selected = list(dict.fromkeys(symbols))
    if len(selected) > max_symbols:
        raise ValueError("etf_evidence_shortlist_exceeds_limit")
    funds = {}
    headers = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
    rate_limited = False
    for symbol in selected:
        security = securities.get(symbol, {})
        fact = snapshot.get("etf_facts", {}).get(symbol, {})
        if security.get("security_type") != "etf" or security.get("exchange") not in {"SSE", "SZSE"}:
            continue
        if etf_buy_excluded({**security, "facts": fact}):
            continue
        captured = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        start = (date.fromisoformat(captured[:10]) - timedelta(days=30)).isoformat()
        specs = [
            ("holdings", "https://fundf10.eastmoney.com/FundArchivesDatas.aspx", {"type": "jjcc", "code": symbol, "topline": "100", "year": captured[:4], "month": ""}),
            ("industries", "https://api.fund.eastmoney.com/f10/HYPZ/", {"fundCode": symbol, "year": captured[:4]}),
            ("nav_comparison", "https://api.fund.eastmoney.com/f10/lsjz", {"fundCode": symbol, "pageIndex": "1", "pageSize": "20", "startDate": start, "endDate": captured[:10]})]
        evidence = {"symbol": symbol, "evidence_gaps": []}
        for kind, url, params in specs:
            try:
                if rate_limited:
                    raise ValueError("source_rate_limited_not_requested")
                response = requests.get(url, params=params, headers=headers, timeout=timeout)
                if response.status_code in {429, 514}:
                    rate_limited = True
                response.raise_for_status()
                text = response.text
                payload = demjson.decode(text[text.find("{"):text.rfind("}")+1])
                observed = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
                if kind == "holdings":
                    evidence[kind] = parse_holdings(payload["content"], symbol, observed)
                elif kind == "industries":
                    evidence[kind] = parse_industries(payload, symbol, observed)
                else:
                    quote = snapshot.get("quotes", {}).get(symbol, {})
                    evidence[kind] = nav_comparison(payload, symbol, quote,
                        etf_market_region({**security, "facts": fact}), observed,
                        quote.get("source_url") or quote.get("source")
                        or snapshot.get("data_quality", {}).get("source_details", {}).get("etf_spot"))
            except (requests.RequestException, ValueError, KeyError, TypeError, demjson.JSONDecodeError) as exc:
                evidence["evidence_gaps"].append({"component": kind, "source_url": url,
                    "available_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                    "error": f"{type(exc).__name__}: {exc}"})
        funds[symbol] = evidence
    return {**snapshot, "etf_evidence": funds, "etf_overlap_evidence": pairwise_overlaps(funds),
            "etf_evidence_observed_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")}
