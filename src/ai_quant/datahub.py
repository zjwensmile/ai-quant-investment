from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Protocol, Sequence
from zoneinfo import ZoneInfo

from .securities import (
    Board,
    Security,
    SecurityType,
    TradingStatus,
    classify_etf_exchange,
    classify_main_board_stock,
    classify_research_stock,
    normalize_symbol,
)
from .sessions import session_state


class MarketDataProvider(Protocol):
    name: str

    def fetch_stock_spot(self) -> Sequence[dict[str, object]]: ...

    def fetch_etf_spot(self) -> Sequence[dict[str, object]]: ...

    def fetch_index_spot(self) -> Sequence[dict[str, object]]: ...

    def fetch_risk_warning(self) -> Sequence[dict[str, object]]: ...

    def fetch_suspensions(self, trade_date: date) -> Sequence[dict[str, object]]: ...

    def fetch_trading_calendar(self) -> Sequence[dict[str, object]]: ...


class SnapshotBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuiltSnapshot:
    data: dict[str, object]

    def write(self, path: Path) -> None:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class DataHub:
    """Creates one point-in-time snapshot or fails without producing a partial one."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    def build_snapshot(
        self,
        as_of: datetime,
        account: dict[str, object] | None = None,
        *,
        include_growth_boards: bool = False,
    ) -> BuiltSnapshot:
        if as_of.tzinfo is None:
            raise ValueError("as_of 必须包含时区")
        try:
            stocks = list(self.provider.fetch_stock_spot())
            etfs = list(self.provider.fetch_etf_spot())
            risk_rows = list(self.provider.fetch_risk_warning())
            suspension_rows = list(self.provider.fetch_suspensions(as_of.date()))
            calendar_loader = getattr(self.provider, "fetch_trading_calendar", None)
            calendar_rows = list(calendar_loader()) if calendar_loader else []
        except Exception as exc:
            raise SnapshotBuildError(f"关键行情源读取失败: {exc}") from exc

        if not stocks:
            raise SnapshotBuildError("股票行情为空，拒绝生成快照")
        if not etfs:
            raise SnapshotBuildError("ETF 行情为空，拒绝生成快照")

        index_rows: list[dict[str, object]] = []
        index_warning: str | None = None
        index_loader = getattr(self.provider, "fetch_index_spot", None)
        if index_loader is not None:
            try:
                index_rows = list(index_loader())
            except Exception as exc:
                index_warning = f"三大指数行情不可用: {exc}"

        fetched_datetime = datetime.now(ZoneInfo("Asia/Shanghai"))
        fetched_at = fetched_datetime.isoformat(timespec="seconds")
        indices = _index_snapshots(index_rows, fetched_at)
        if index_loader is not None and len(indices) < 3 and index_warning is None:
            index_warning = f"三大指数行情不完整: {len(indices)}/3"
        risk_warning_complete = bool(
            getattr(self.provider, "risk_warning_complete", True)
        )
        suspension_complete = bool(
            getattr(self.provider, "suspension_complete", True)
        )
        trading_calendar_complete = bool(
            getattr(self.provider, "trading_calendar_complete", True)
        )
        risk_symbols = _symbol_set(risk_rows)
        suspended_symbols = _symbol_set(suspension_rows)
        securities: list[Security] = []
        quotes: dict[str, dict[str, object]] = {}
        warnings: list[str] = []
        excluded_risk_warning_count = 0

        for row in stocks:
            try:
                symbol = normalize_symbol(_required(row, "代码", "symbol", "code"))
            except (KeyError, ValueError) as exc:
                warnings.append(f"忽略无法识别的股票记录: {exc}")
                continue
            classification = (classify_research_stock(symbol) if include_growth_boards
                              else classify_main_board_stock(symbol))
            if classification is None:
                continue
            exchange, board = classification
            name = str(_first(row, "名称", "name", default=symbol)).strip()
            risk_warning = (
                True
                if symbol in risk_symbols or _looks_risk_warning(name)
                else False if risk_warning_complete else None
            )
            if risk_warning is True:
                excluded_risk_warning_count += 1
                continue
            security = Security(
                symbol=symbol,
                name=name,
                exchange=exchange,
                security_type=SecurityType.STOCK,
                board=board,
                risk_warning=risk_warning,
                trading_status=_trading_status(
                    symbol, suspended_symbols, suspension_complete
                ),
                price_tick=Decimal("0.01"),
                buy_lot_size=1 if board == Board.STAR else 100,
                minimum_buy_quantity=200 if board == Board.STAR else 100,
                same_day_turnaround=False,
                source=self.provider.name,
                available_at=fetched_at,
            )
            securities.append(security)
            quotes[symbol] = _quote(row, fetched_at)

        for row in etfs:
            try:
                symbol = normalize_symbol(_required(row, "代码", "symbol", "code"))
                exchange = classify_etf_exchange(symbol)
            except (KeyError, ValueError) as exc:
                warnings.append(f"忽略无法识别的 ETF 记录: {exc}")
                continue
            name = str(_first(row, "名称", "name", default=symbol)).strip()
            security = Security(
                symbol=symbol,
                name=name,
                exchange=exchange,
                security_type=SecurityType.ETF,
                board=Board.NOT_APPLICABLE,
                risk_warning=False,
                trading_status=_trading_status(
                    symbol, suspended_symbols, suspension_complete
                ),
                price_tick=Decimal("0.001"),
                same_day_turnaround=None,
                source=self.provider.name,
                available_at=fetched_at,
            )
            securities.append(security)
            quotes[symbol] = _quote(row, fetched_at)

        if not securities:
            raise SnapshotBuildError("过滤后没有沪深主板股票或 ETF，拒绝生成快照")

        effective_as_of = max(
            as_of.astimezone(ZoneInfo("Asia/Shanghai")), fetched_datetime
        )
        timestamp = effective_as_of.isoformat(timespec="seconds")
        calendar_dates = sorted(_date_set(calendar_rows)) if trading_calendar_complete else []
        calendar_evidence = dict(getattr(self.provider, "trading_calendar_evidence", {}))
        coverage_start = calendar_evidence.get("coverage_start", calendar_dates[0].isoformat() if calendar_dates else None)
        coverage_end = calendar_evidence.get("coverage_end", calendar_dates[-1].isoformat() if calendar_dates else None)
        calendar_known = bool(calendar_dates and coverage_start <= effective_as_of.date().isoformat() <= coverage_end)
        is_trading_day = effective_as_of.date() in calendar_dates if calendar_known else None
        trading_calendar_complete = trading_calendar_complete and calendar_known
        if not calendar_known:
            calendar_dates = []
        market_session = session_state(effective_as_of, is_trading_day is True)
        snapshot = {
            "snapshot_id": f"market-{effective_as_of.strftime('%Y%m%dT%H%M%S')}",
            "as_of_time": timestamp,
            "purpose": "live_decision",
            "account": account
            or {"version": "unconfigured", "cash": None, "positions": []},
            "market": {
                **_calendar_context(calendar_dates, effective_as_of),
                "session": market_session.session.value if calendar_known else "unknown",
                "is_trading_day": is_trading_day,
                "trading_calendar_evidence": calendar_evidence,
                "execution_eligible": (
                    market_session.execution_eligible
                    and risk_warning_complete
                    and suspension_complete
                    and trading_calendar_complete
                ),
                "indices": indices,
                "all_stock_distribution": _stock_distribution(stocks),
                "breadth": None,
                "turnover": None,
            },
            "events": [],
            "candidates": {
                "HOLDINGS": [], "QUALITY_VALUE": [],
                "DEFENSIVE": [], "TACTICAL": [],
            },
            "securities": [item.to_dict() for item in securities],
            "quotes": quotes,
            "data_quality": {
                "status": (
                    "valid"
                    if risk_warning_complete
                    and suspension_complete
                    and trading_calendar_complete
                    and is_trading_day
                    else "research_only"
                ),
                "provider": self.provider.name,
                "stock_research_scope": "main_star_chinext" if include_growth_boards else "main",
                "source_details": dict(getattr(self.provider, "sources", {})),
                "available_at": fetched_at,
                "fetched_at": fetched_at,
                "quote_time_semantics": "available_at/fetched_at为本次获取时间，不是成交时间；source_quote_at/source_trade_date仅取源字段。交易日历最近交易日不能证明报价所属日期。",
                "quotes_without_source_date": sum(q["source_trade_date"] is None for q in quotes.values()),
                "quote_max_age_seconds": market_session.quote_max_age_seconds,
                "critical_sources": ["stock_spot", "etf_spot"],
                "execution_safety_sources": [
                    "risk_warning",
                    "suspensions",
                    "trading_calendar",
                ],
                "warnings": list(getattr(self.provider, "warnings", []))
                + warnings
                + ([index_warning] if index_warning else [])
                + (
                    [f"已按账户规则排除 {excluded_risk_warning_count} 只 ST/*ST 股票。"]
                    if excluded_risk_warning_count
                    else []
                )
                + (["交易日历缺失或未覆盖当前日期，交易日状态未知，快照仅供研究。"] if is_trading_day is None
                   else [] if is_trading_day else ["当前日期不是 A 股交易日，快照仅供研究。"])
                + ["报价是源站本次返回值；抓取日期不代表成交日期。缺少源报价日期的价格仅作条件研究参考，执行前重新核验。"]
                + ["ETF 的 T+0/T+1 属性尚未由权威证券主数据确认，当前值为 unknown。"],
            },
        }
        return BuiltSnapshot(snapshot)


def _first(row: dict[str, object], *names: str, default: object = None) -> object:
    for name in names:
        if name in row:
            return row[name]
    return default


def _required(row: dict[str, object], *names: str) -> object:
    value = _first(row, *names)
    if value is None:
        raise KeyError("/".join(names))
    return value


def _symbol_set(rows: Sequence[dict[str, object]]) -> set[str]:
    symbols: set[str] = set()
    for row in rows:
        value = _first(row, "代码", "股票代码", "symbol", "code")
        if value is None:
            continue
        try:
            symbols.add(normalize_symbol(value))
        except ValueError:
            continue
    return symbols


def _date_set(rows: Sequence[dict[str, object]]) -> set[date]:
    values: set[date] = set()
    for row in rows:
        value = _first(row, "trade_date", "交易日", "date")
        if isinstance(value, datetime):
            values.add(value.date())
        elif isinstance(value, date):
            values.add(value)
        elif value:
            try:
                values.add(date.fromisoformat(str(value)[:10]))
            except ValueError:
                continue
    return values


def _looks_risk_warning(name: str) -> bool:
    compact = name.upper().replace(" ", "")
    return compact.startswith("ST") or compact.startswith("*ST")


def _trading_status(
    symbol: str,
    suspended_symbols: set[str],
    suspension_complete: bool,
) -> TradingStatus:
    if symbol in suspended_symbols:
        return TradingStatus.SUSPENDED
    if suspension_complete:
        return TradingStatus.TRADABLE
    return TradingStatus.UNKNOWN


def _number(value: object) -> float | None:
    if value is None or value in ("", "-"):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _calendar_context(trading_dates: Sequence[date], as_of: datetime) -> dict[str, object]:
    completed = [item for item in trading_dates if item < as_of.date()
                 or (item == as_of.date() and as_of.timetz().replace(tzinfo=None) > time(15))]
    next_start = None
    for trading_date in trading_dates:
        for start, end in ((time(9, 30), time(11, 30)), (time(13), time(15))):
            opened = datetime.combine(trading_date, start, tzinfo=as_of.tzinfo)
            closed = datetime.combine(trading_date, end, tzinfo=as_of.tzinfo)
            if as_of <= closed:
                next_start = max(opened, as_of).isoformat(timespec="seconds")
                break
        if next_start is not None:
            break
    return {
        "latest_completed_trade_date": completed[-1].isoformat() if completed else None,
        "recent_completed_trade_dates": [item.isoformat() for item in completed[-61:]],
        "next_trading_session_open_at": next_start,
    }


def _quote_time(row: dict[str, object], fetched_at: str) -> dict[str, object]:
    # In particular, Sina's clock-only timestamp cannot establish a trade date.
    fields = ("时间戳", "更新时间", "quote_time", "trade_time", "ticktime", "数据日期", "交易日期", "trade_date")
    raw = {key: str(row[key]) for key in fields if row.get(key) is not None}
    stamp, source_date, clock_only = None, None, False
    for key, value in raw.items():
        value = value.strip()
        try:
            if key in ("数据日期", "交易日期", "trade_date"):
                parsed_date = datetime.strptime(value, "%Y%m%d").date() if len(value) == 8 and value.isdigit() else date.fromisoformat(value[:10])
                source_date = parsed_date.isoformat()
            elif len(value) >= 19 and value[4:5] == "-" and value[7:8] == "-":
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                stamp = parsed.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
            else:
                time.fromisoformat(value)
                clock_only = True
        except (ValueError, TypeError):
            continue
    if source_date is None and stamp is not None:
        source_date = stamp[:10]
    status = "source_datetime" if stamp else "source_date_only" if source_date else "time_only_without_date" if clock_only else "unavailable"
    if stamp and datetime.fromisoformat(stamp) > datetime.fromisoformat(fetched_at):
        status = "source_time_after_fetch"
    if stamp and source_date != stamp[:10]:
        status = "source_date_conflict"
    return {"fetched_at": fetched_at, "source_quote_at": stamp, "source_trade_date": source_date,
            "source_quote_time_raw": raw, "quote_time_status": status}


def _quote(row: dict[str, object], available_at: str) -> dict[str, object]:
    return {
        "last": _number(_first(row, "最新价", "last", "price")),
        "open": _number(_first(row, "今开", "open")),
        "high": _number(_first(row, "最高", "high")),
        "low": _number(_first(row, "最低", "low")),
        "previous_close": _number(_first(row, "昨收", "previous_close")),
        "volume": _number(_first(row, "成交量", "volume")),
        "turnover": _number(_first(row, "成交额", "turnover", "amount")),
        "pe_ratio": _number(_first(row, "市盈率-动态", "市盈率", "per", "pe")),
        "pb_ratio": _number(_first(row, "市净率", "pb")),
        "market_cap": _number(_first(row, "总市值", "mktcap", "market_cap")),
        "float_market_cap": _number(
            _first(row, "流通市值", "nmc", "float_market_cap")
        ),
        "turnover_rate": _number(_first(row, "换手率", "turnoverratio")),
        "available_at": available_at,
        **_quote_time(row, available_at),
    }


def _index_snapshots(
    rows: Sequence[dict[str, object]], available_at: str
) -> list[dict[str, object]]:
    targets = {
        "sse": ({"sh000001", "000001"}, ("上证指数",)),
        "szse": ({"sz399001", "399001"}, ("深证成指", "深证综指")),
        "chinext": ({"sz399006", "399006"}, ("创业板指",)),
    }
    found: dict[str, dict[str, object]] = {}
    for row in rows:
        code = str(_first(row, "代码", "symbol", "code", default="")).lower()
        name = str(_first(row, "名称", "name", default="")).strip()
        target_id = next(
            (
                target
                for target, (codes, names) in targets.items()
                if code in codes or any(value in name for value in names)
            ),
            None,
        )
        if target_id is None or target_id in found:
            continue
        last = _number(_first(row, "最新价", "last", "price"))
        previous_close = _number(_first(row, "昨收", "previous_close"))
        change_percent = _number(_first(row, "涨跌幅", "change_percent"))
        if change_percent is None and last is not None and previous_close:
            change_percent = (last / previous_close - 1.0) * 100.0
        change_amount = _number(_first(row, "涨跌额", "change_amount"))
        if change_amount is None and last is not None and previous_close is not None:
            change_amount = last - previous_close
        found[target_id] = {
            "id": target_id,
            "symbol": code,
            "name": name or {
                "sse": "上证指数",
                "szse": "深证成指",
                "chinext": "创业板指",
            }[target_id],
            "last": last,
            "change_amount": change_amount,
            "change_percent": change_percent,
            "available_at": available_at,
            **_quote_time(row, available_at),
        }
    return [found[target] for target in ("sse", "szse", "chinext") if target in found]


def _stock_distribution(rows: Sequence[dict[str, object]]) -> dict[str, object]:
    definitions = (
        (">20%", lambda value: value > 20),
        ("10~20%", lambda value: 10 < value <= 20),
        ("7~10%", lambda value: 7 < value <= 10),
        ("4~7%", lambda value: 4 < value <= 7),
        ("2~4%", lambda value: 2 < value <= 4),
        ("0~2%", lambda value: 0 < value <= 2),
        ("平盘", lambda value: value == 0),
        ("-2~0%", lambda value: -2 <= value < 0),
        ("-4~-2%", lambda value: -4 <= value < -2),
        ("-7~-4%", lambda value: -7 <= value < -4),
        ("-10~-7%", lambda value: -10 <= value < -7),
        ("-20~-10%", lambda value: -20 <= value < -10),
        ("<-20%", lambda value: value < -20),
    )
    counts = {label: 0 for label, _ in definitions}
    changes: list[float] = []
    for row in rows:
        change = _number(_first(row, "涨跌幅", "change_percent"))
        if change is None:
            last = _number(_first(row, "最新价", "last", "price"))
            previous_close = _number(_first(row, "昨收", "previous_close"))
            if last is None or previous_close is None or previous_close <= 0:
                continue
            change = (last / previous_close - 1.0) * 100.0
        changes.append(change)
        for label, predicate in definitions:
            if predicate(change):
                counts[label] += 1
                break
    return {
        "coverage": len(changes),
        "advancers": sum(value > 0 for value in changes),
        "decliners": sum(value < 0 for value in changes),
        "unchanged": sum(value == 0 for value in changes),
        "limit_up_estimated": sum(value >= 9.5 for value in changes),
        "limit_down_estimated": sum(value <= -9.5 for value in changes),
        "bins": [
            {
                "label": label,
                "count": counts[label],
                "direction": (
                    "up" if index < 6 else "flat" if index == 6 else "down"
                ),
            }
            for index, (label, _) in enumerate(definitions)
        ],
    }
