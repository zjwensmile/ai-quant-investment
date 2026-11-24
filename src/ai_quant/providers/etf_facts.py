from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from html import unescape
import json
from pathlib import Path
import re
import threading
import time
from zoneinfo import ZoneInfo

from ..securities import normalize_symbol
from ..etf_strategy import etf_buy_excluded, etf_market_region, etf_sector, select_etf_research_candidates


def parse_etf_facts(symbol: str, html: str, available_at: str) -> dict:
    """Parse reported net assets, never spot market capitalization or turnover."""
    code = normalize_symbol(symbol)
    # The live page omits some closing </td> tags; stop at the next cell too.
    cells = re.findall(r"<(?:th|td)\b[^>]*>(.*?)(?=</?(?:th|td|tr)\b)", html, re.S | re.I)
    cells = [" ".join(unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
    fields = {cells[i]: cells[i + 1] for i in range(0, len(cells) - 1, 2)}
    return _facts_from_fields(code, fields, available_at)


def _facts_from_fields(code: str, fields: dict, available_at: str) -> dict:
    source = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
    result = {"symbol": code, "metadata_source_url": source, "metadata_available_at": available_at,
              "source_fields": fields, "evidence_gaps": []}
    if not fields.get("基金代码", "").startswith(code):
        raise ValueError("基金基本概况证券代码不匹配")
    full_name = fields.get("基金全称", "")
    result["is_etf"] = ("交易型开放式" in full_name or ("交易型货币市场" in full_name and "ETF" in fields.get("基金简称", ""))) and "联接" not in full_name
    result["fund_type"] = fields.get("基金类型", "")
    scale = re.search(r"([\d,.]+)\s*亿元.*?(\d{4})年(\d{2})月(\d{2})日", fields.get("净资产规模", ""))
    if scale:
        result.update(aum_cny=str(Decimal(scale[1].replace(",", "")) * 100000000),
                      aum_as_of=f"{scale[2]}-{scale[3]}-{scale[4]}", aum_source_url=source,
                      aum_available_at=available_at, aum_basis="reported_net_assets")
    else:
        result["evidence_gaps"].append("reported_aum_unavailable")
    index = fields.get("跟踪标的", "")
    if index and index not in {"该基金无跟踪标的", "无", "--"}:
        result["index_id"] = re.sub(r"\s+", "", index)
    description = " ".join((full_name, result["fund_type"], index))
    if "货币" in description:
        group = "money"
        result.setdefault("index_id", "money_market")
    elif "黄金" in description or "上海金" in description:
        group = "gold"
    elif "固收" in result["fund_type"] or any(word in description for word in ("债", "短融", "融资券")):
        group = "bond"
    elif any(word in description for word in ("港股", "恒生", "香港")):
        group = "hong_kong_equity"
    elif any(word in description for word in ("QDII", "纳斯达克", "纳指", "标普", "美国", "日本", "日经", "德国", "法国", "欧洲", "印度", "海外", "中韩")):
        group = "overseas_equity"
    elif "股票" in result["fund_type"]:
        group = "a_share_equity"
    else:
        group = "unknown"
    result.update(asset_group=group, classification_basis="source_fund_type_and_tracking_description")
    result["market_region"] = etf_market_region(result)
    result["sector"] = etf_sector(result)
    if group == "a_share_equity":
        broad_indices = {"沪深300指数", "中证500指数", "中证1000指数", "中证2000指数", "中证A500指数",
                         "上证50指数", "上证180指数", "中证全指指数", "创业板指数", "创业板50指数",
                         "上证科创板50成份指数", "上证科创板100指数", "深证成份指数"}
        result["concentration_group"] = "a_share_broad_equity" if index in broad_indices else "a_share_sector_equity"
    return result


def fetch_etf_facts(symbols: list[str], *, workers: int = 3, timeout: float = 15,
                    request_interval_seconds: float = 2, cache_path: str | Path | None = None) -> dict:
    """Bounded verification of an explicitly chosen shortlist; no database writes."""
    import requests
    request_lock, rate_limited = threading.Lock(), threading.Event()
    next_request_at = 0.0
    cache_file = Path(cache_path) if cache_path is not None else None
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file and cache_file.exists() else {}

    def cached_fact(code):
        fact = cache.get(code, {})
        try:
            observed = datetime.fromisoformat(fact["metadata_available_at"])
            age = (datetime.now(ZoneInfo("Asia/Shanghai")) - observed).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 <= age <= 86400 or not fact.get("aum_cny") or fact.get("evidence_gaps") or fact.get("is_etf") is not True:
            return None
        # Reapply deterministic classification to the original captured source fields.
        result = _facts_from_fields(code, fact["source_fields"], fact["metadata_available_at"]) if fact.get("source_fields") else dict(fact)
        result.update(aum_available_at=fact.get("aum_available_at"), cache_reused=True)
        return result

    def fetch(symbol: str) -> tuple[str, dict]:
        nonlocal next_request_at
        code = normalize_symbol(symbol)
        if len(code) != 6 or code[0] not in "15":
            return code, {"symbol": code, "evidence_gaps": ["not_sse_szse_etf_code"]}
        known = cached_fact(code)
        if known is not None:
            return code, known
        url = f"https://fundf10.eastmoney.com/jbgk_{code}.html"
        try:
            with request_lock:
                if rate_limited.is_set():
                    return code, {"symbol": code, "metadata_source_url": url,
                                  "evidence_gaps": ["fund_facts_rate_limited_not_requested"]}
                time.sleep(max(0, next_request_at - time.monotonic()))
                if rate_limited.is_set():
                    return code, {"symbol": code, "metadata_source_url": url,
                                  "evidence_gaps": ["fund_facts_rate_limited_not_requested"]}
                next_request_at = time.monotonic() + max(0, request_interval_seconds)
            response = requests.get(url, timeout=timeout)
            if response.status_code in {429, 514}:
                rate_limited.set()
            response.raise_for_status()
            response.encoding = "utf-8"
            return code, parse_etf_facts(code, response.text, datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"))
        except (requests.RequestException, ValueError) as exc:
            return code, {"symbol": code, "metadata_source_url": url,
                          "evidence_gaps": [f"fund_facts_unavailable: {type(exc).__name__}: {exc}"]}

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 3))) as pool:
        result = dict(pool.map(fetch, dict.fromkeys(symbols)))
    successes = {code: fact for code, fact in result.items()
                 if fact.get("aum_cny") and fact.get("is_etf") is True and not fact.get("evidence_gaps")}
    if cache_file and successes:
        cache.update(successes)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(cache_file.suffix + ".tmp")
        temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(cache_file)
    return result


def select_etf_fact_symbols(snapshot: dict, limit: int = 30) -> list[str]:
    """Market and sector verification targets; this selection makes no AUM claim."""
    quotes = snapshot.get("quotes", {})
    rows = [row for row in snapshot.get("securities", [])
            if row.get("security_type") == "etf" and row.get("exchange") in {"SSE", "SZSE"}]

    def turnover(row: dict) -> Decimal:
        try:
            value = Decimal(str(quotes.get(row["symbol"], {}).get("turnover", 0)))
            return value if value.is_finite() and value > 0 else Decimal(0)
        except Exception:
            return Decimal(0)

    allowed = sorted((row for row in rows if not etf_buy_excluded(row)),
                     key=lambda row: (-turnover(row), row["symbol"]))
    ranked = [dict(row, score=len(allowed) - rank) for rank, row in enumerate(allowed)]
    selected = [row["symbol"] for row in select_etf_research_candidates(ranked, limit)]
    # These are verification targets, not endorsed holdings or inferred fund facts.
    representatives = ("510300", "510500", "510050", "159915", "588000",
                       "513100", "513500", "159920", "513660", "513330")
    listed = {row["symbol"] for row in rows}
    held = [row["symbol"] for row in snapshot.get("account", {}).get("positions", []) if row.get("symbol") in listed]
    allowed_symbols = {row["symbol"] for row in allowed}
    return list(dict.fromkeys(held + [symbol for symbol in representatives if symbol in allowed_symbols] + selected))
