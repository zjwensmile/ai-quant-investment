from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .securities import Exchange, normalize_symbol


SSE_FUND_LIST_URL = "https://etf.sse.com.cn/fundlist/"
SSE_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_ETF_LIST_URL = "https://fund.szse.cn/marketdata/etf/"
SZSE_QUERY_URL = "https://www.szse.cn/api/report/ShowReport/data"


@dataclass(frozen=True)
class EtfTradeAttribute:
    symbol: str
    exchange: Exchange
    same_day_turnaround: bool | None
    price_limit_ratio: Decimal | None
    listing_date: date | None
    fund_category: str | None
    source_url: str
    available_at: str
    raw: dict[str, object]


class ExchangeReferenceProvider:
    """Reads public exchange product lists without inferring rules from fund names."""

    def __init__(
        self,
        json_loader: Callable[[str, dict[str, str], str], object] | None = None,
    ) -> None:
        self._json_loader = json_loader or _load_json

    def fetch_sse_etf_attributes(self) -> list[EtfTradeAttribute]:
        all_rows = self._sse_rows({"SUBCLASS": "", "SWING_TRADE": ""})
        t0_symbols = {
            normalize_symbol(row["FUND_CODE"])
            for row in self._sse_rows({"SUBCLASS": "", "SWING_TRADE": "是"})
        }
        twenty_percent_symbols = {
            normalize_symbol(row["FUND_CODE"])
            for row in self._sse_rows(
                {"SUBCLASS": "09,31,15", "SWING_TRADE": ""}
            )
        }
        available_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return [
            EtfTradeAttribute(
                symbol=normalize_symbol(row["FUND_CODE"]),
                exchange=Exchange.SSE,
                same_day_turnaround=normalize_symbol(row["FUND_CODE"]) in t0_symbols,
                price_limit_ratio=(
                    Decimal("0.20")
                    if normalize_symbol(row["FUND_CODE"]) in twenty_percent_symbols
                    else Decimal("0.10")
                ),
                listing_date=_date(row.get("LISTING_DATE")),
                fund_category=_text(row.get("CATEGORY")),
                source_url=SSE_FUND_LIST_URL,
                available_at=available_at,
                raw=row,
            )
            for row in all_rows
            if row.get("FUND_CODE")
        ]

    def fetch_szse_etf_list(self) -> list[EtfTradeAttribute]:
        """Loads the official ETF list; unavailable rule flags deliberately stay unknown."""
        first = self._szse_page(1)
        if not first:
            return []
        page_count = int(first["metadata"].get("pagecount") or 1)
        pages = [first]
        pages.extend(self._szse_page(page) for page in range(2, page_count + 1))
        available_at = datetime.now().astimezone().isoformat(timespec="seconds")
        result: list[EtfTradeAttribute] = []
        for page in pages:
            for row in page.get("data", []):
                symbol = _html_code(row.get("sys_key"))
                if not symbol:
                    continue
                result.append(
                    EtfTradeAttribute(
                        symbol=symbol,
                        exchange=Exchange.SZSE,
                        same_day_turnaround=None,
                        price_limit_ratio=None,
                        listing_date=None,
                        fund_category=None,
                        source_url=SZSE_ETF_LIST_URL,
                        available_at=available_at,
                        raw=row,
                    )
                )
        return result

    def _sse_rows(self, filters: dict[str, str]) -> list[dict[str, object]]:
        params = {
            "sqlId": "COMMON_JJZWZ_JJLB_L",
            "isPagination": "false",
            "CATEGORY": "F000",
            "type": "inParams",
            **filters,
        }
        payload = self._json_loader(SSE_QUERY_URL, params, SSE_FUND_LIST_URL)
        return list(payload.get("result", []))  # type: ignore[union-attr]

    def _szse_page(self, page: int) -> dict[str, object]:
        params = {
            "SHOWTYPE": "JSON",
            "CATALOGID": "fund_etf",
            "TABKEY": "tab1",
            "tab1PAGENO": str(page),
            "PAGENO": str(page),
        }
        payload = self._json_loader(SZSE_QUERY_URL, params, SZSE_ETF_LIST_URL)
        for item in payload:  # type: ignore[union-attr]
            if item.get("metadata", {}).get("tabkey") == "tab1":
                return item
        return {}


def _load_json(url: str, params: dict[str, str], referer: str) -> object:
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={"Referer": referer, "User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _html_code(value: object) -> str | None:
    match = re.search(r"\b(1\d{5})\b", str(value or ""))
    return normalize_symbol(match.group(1)) if match else None


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
