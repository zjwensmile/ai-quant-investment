from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .market_store import IndustryMembership, MarketStore
from .securities import normalize_symbol


class IndustryProvider(Protocol):
    name: str
    sources: dict[str, str]

    def fetch_industry_changes(
        self, symbol: str, start_date: date, end_date: date
    ) -> Sequence[dict[str, object]]: ...


class IndustrySyncService:
    def __init__(self, provider: IndustryProvider, store: MarketStore) -> None:
        self.provider = provider
        self.store = store

    def sync_symbol(
        self, symbol: str, start_date: date, end_date: date
    ) -> dict[str, object]:
        code = normalize_symbol(symbol)
        records = self.provider.fetch_industry_changes(code, start_date, end_date)
        memberships = _memberships(code, records)
        if not memberships:
            raise ValueError(f"{code} 没有可用的行业归属记录")
        available_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        )
        count = self.store.upsert_industry_memberships(
            memberships,
            self.provider.sources.get(f"industry:{code}", self.provider.name),
            available_at,
        )
        return {"symbol": code, "memberships": count, "available_at": available_at}


def _memberships(
    symbol: str, rows: Sequence[dict[str, object]]
) -> list[IndustryMembership]:
    result: list[IndustryMembership] = []
    for row in rows:
        effective_from = _as_date(row.get("变更日期"))
        if effective_from is None:
            continue
        result.append(
            IndustryMembership(
                symbol=symbol,
                security_name=_text(row.get("新证券简称")),
                effective_from=effective_from,
                standard=_text(row.get("分类标准")),
                standard_code=_text(row.get("分类标准编码")),
                industry_code=_text(row.get("行业编码")),
                section=_text(row.get("行业门类")),
                major=_text(row.get("行业大类")),
                middle=_text(row.get("行业中类")),
                minor=_text(row.get("行业次类")),
                organization=_text(row.get("机构名称")),
                raw=dict(row),
            )
        )
    return result


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _text(value: object) -> str | None:
    if value is None or str(value) in {"", "nan", "NaN", "None"}:
        return None
    return str(value)
