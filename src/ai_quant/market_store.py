from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Sequence


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    amount: float | None
    outstanding_share: float | None = None
    turnover: float | None = None


@dataclass(frozen=True)
class CorporateAction:
    symbol: str
    announcement_date: date | None
    ex_date: date | None
    record_date: date | None
    cash_dividend_per_10: float | None
    bonus_shares_per_10: float | None
    transfer_shares_per_10: float | None
    status: str | None
    raw: dict[str, object]


@dataclass(frozen=True)
class IndustryMembership:
    symbol: str
    security_name: str | None
    effective_from: date
    standard: str | None
    standard_code: str | None
    industry_code: str | None
    section: str | None
    major: str | None
    middle: str | None
    minor: str | None
    organization: str | None
    raw: dict[str, object]


@dataclass(frozen=True)
class StoredEtfTradeAttribute:
    symbol: str
    exchange: str
    same_day_turnaround: bool | None
    price_limit_ratio: str | None
    listing_date: date | None
    fund_category: str | None
    source_url: str
    available_at: str
    raw: dict[str, object]


@dataclass(frozen=True)
class FundamentalSnapshot:
    symbol: str
    report_period: date
    announcement_date: date
    revenue: float | None
    revenue_growth: float | None
    net_profit: float | None
    net_profit_growth: float | None
    earnings_per_share: float | None
    book_value_per_share: float | None
    operating_cash_flow_per_share: float | None
    return_on_equity: float | None
    gross_margin: float | None
    industry_name: str | None
    source_url: str
    raw: dict[str, object]


class MarketStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS raw_daily_bars (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    amount REAL,
                    outstanding_share REAL,
                    turnover REAL,
                    source TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    record_hash TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date)
                );

                CREATE TABLE IF NOT EXISTS raw_daily_bar_revisions (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    amount REAL,
                    outstanding_share REAL,
                    turnover REAL,
                    source TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    record_hash TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date, record_hash)
                );

                CREATE TABLE IF NOT EXISTS display_qfq_bars (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    amount REAL,
                    source TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, trade_date)
                );

                CREATE TABLE IF NOT EXISTS corporate_actions (
                    action_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    announcement_date TEXT,
                    ex_date TEXT,
                    record_date TEXT,
                    cash_dividend_per_10 REAL,
                    bonus_shares_per_10 REAL,
                    transfer_shares_per_10 REAL,
                    status TEXT,
                    source TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_syncs (
                    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS security_master (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    security_type TEXT NOT NULL,
                    board TEXT NOT NULL,
                    risk_warning INTEGER,
                    trading_status TEXT NOT NULL,
                    price_tick TEXT NOT NULL,
                    buy_lot_size INTEGER NOT NULL,
                    same_day_turnaround INTEGER,
                    price_limit_ratio TEXT,
                    price_limit_exempt INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    available_at TEXT,
                    last_snapshot_id TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_sync_targets (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    security_type TEXT NOT NULL,
                    held INTEGER NOT NULL,
                    priority REAL NOT NULL,
                    requested_start TEXT NOT NULL,
                    requested_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_started_at TEXT,
                    last_completed_at TEXT,
                    last_error TEXT,
                    raw_bars INTEGER NOT NULL DEFAULT 0,
                    qfq_bars INTEGER NOT NULL DEFAULT 0,
                    corporate_actions INTEGER NOT NULL DEFAULT 0,
                    latest_trade_date TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS industry_memberships (
                    membership_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    security_name TEXT,
                    effective_from TEXT NOT NULL,
                    standard TEXT,
                    standard_code TEXT,
                    industry_code TEXT,
                    section TEXT,
                    major TEXT,
                    middle TEXT,
                    minor TEXT,
                    organization TEXT,
                    source TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_industry_memberships_symbol_date
                ON industry_memberships(symbol, effective_from);

                CREATE TABLE IF NOT EXISTS industry_sync_targets (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_started_at TEXT,
                    last_completed_at TEXT,
                    last_error TEXT,
                    membership_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS industry_syncs (
                    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS etf_trade_attributes (
                    symbol TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    same_day_turnaround INTEGER,
                    price_limit_ratio TEXT,
                    listing_date TEXT,
                    fund_category TEXT,
                    source_url TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS etf_trade_attribute_revisions (
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    same_day_turnaround INTEGER,
                    price_limit_ratio TEXT,
                    listing_date TEXT,
                    fund_category TEXT,
                    source_url TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY(symbol, available_at)
                );

                CREATE TABLE IF NOT EXISTS fundamental_snapshots (
                    symbol TEXT NOT NULL,
                    report_period TEXT NOT NULL,
                    announcement_date TEXT NOT NULL,
                    revenue REAL,
                    revenue_growth REAL,
                    net_profit REAL,
                    net_profit_growth REAL,
                    earnings_per_share REAL,
                    book_value_per_share REAL,
                    operating_cash_flow_per_share REAL,
                    return_on_equity REAL,
                    gross_margin REAL,
                    industry_name TEXT,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    record_hash TEXT NOT NULL,
                    PRIMARY KEY(symbol, report_period, announcement_date, record_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_fundamental_snapshot_pit
                ON fundamental_snapshots(symbol, announcement_date, available_at, report_period);

                INSERT OR IGNORE INTO etf_trade_attribute_revisions
                SELECT symbol, exchange, same_day_turnaround, price_limit_ratio,
                       listing_date, fund_category, source_url, available_at, raw_json
                FROM etf_trade_attributes;
                """
            )

    def upsert_fundamental_snapshots(
        self,
        rows: Sequence[FundamentalSnapshot],
        source: str,
        available_at: str,
    ) -> int:
        values = []
        for item in rows:
            raw_json = json.dumps(item.raw, ensure_ascii=False, sort_keys=True, default=str)
            record_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            values.append((
                item.symbol, item.report_period.isoformat(),
                item.announcement_date.isoformat(), item.revenue,
                item.revenue_growth, item.net_profit, item.net_profit_growth,
                item.earnings_per_share, item.book_value_per_share,
                item.operating_cash_flow_per_share, item.return_on_equity,
                item.gross_margin, item.industry_name, source, item.source_url,
                available_at, raw_json, record_hash,
            ))
        if not values:
            return 0
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO fundamental_snapshots VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return len(values)

    def fundamental_map_as_known(
        self, as_of: date, known_at: str
    ) -> dict[str, dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY symbol
                        ORDER BY report_period DESC, announcement_date DESC,
                                 available_at DESC
                    ) AS revision_rank
                    FROM fundamental_snapshots
                    WHERE report_period <= ? AND announcement_date <= ?
                      AND available_at <= ?
                )
                SELECT * FROM ranked WHERE revision_rank = 1 ORDER BY symbol
                """,
                (as_of.isoformat(), as_of.isoformat(), known_at),
            ).fetchall()
        return {
            str(row["symbol"]): {
                **dict(row),
                "raw": json.loads(str(row["raw_json"])),
            }
            for row in rows
        }

    def upsert_etf_trade_attributes(
        self, attributes: Sequence[StoredEtfTradeAttribute]
    ) -> int:
        rows = [
            (
                item.symbol,
                str(item.exchange),
                _bool_int(item.same_day_turnaround),
                str(item.price_limit_ratio) if item.price_limit_ratio is not None else None,
                _iso(item.listing_date),
                item.fund_category,
                item.source_url,
                item.available_at,
                json.dumps(item.raw, ensure_ascii=False, sort_keys=True, default=str),
            )
            for item in attributes
        ]
        if rows:
            with self._connect() as connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO etf_trade_attribute_revisions "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO etf_trade_attributes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        return len(rows)

    def etf_attribute_map(
        self, known_at: str | None = None
    ) -> dict[str, dict[str, object]]:
        with self._connect() as connection:
            if known_at is None:
                rows = connection.execute(
                    "SELECT * FROM etf_trade_attributes ORDER BY symbol"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    WITH ranked AS (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY symbol ORDER BY available_at DESC
                        ) AS revision_rank
                        FROM etf_trade_attribute_revisions
                        WHERE available_at <= ?
                    )
                    SELECT symbol, exchange, same_day_turnaround, price_limit_ratio,
                           listing_date, fund_category, source_url, available_at, raw_json
                    FROM ranked WHERE revision_rank = 1 ORDER BY symbol
                    """,
                    (known_at,),
                ).fetchall()
        return {str(row["symbol"]): dict(row) for row in rows}

    def apply_etf_attributes(self, snapshot: dict[str, object]) -> dict[str, object]:
        attributes = self.etf_attribute_map(
            str(snapshot.get("as_of_time")) if snapshot.get("as_of_time") else None
        )
        etfs = [
            item
            for item in snapshot.get("securities", [])  # type: ignore[union-attr]
            if item.get("security_type") == "etf"
        ]
        known_t0 = 0
        known_limit = 0
        for item in etfs:
            attribute = attributes.get(str(item["symbol"]))
            if not attribute:
                continue
            if attribute["same_day_turnaround"] is not None:
                item["same_day_turnaround"] = bool(attribute["same_day_turnaround"])
                known_t0 += 1
            if attribute["price_limit_ratio"] is not None:
                item["price_limit_ratio"] = str(attribute["price_limit_ratio"])
                known_limit += 1
            if attribute["listing_date"]:
                item["listing_date"] = attribute["listing_date"]
            item["exchange_reference"] = {
                "source_url": attribute["source_url"],
                "available_at": attribute["available_at"],
                "fund_category": attribute["fund_category"],
            }
        quality = snapshot.setdefault("data_quality", {})
        warnings = [
            item
            for item in quality.setdefault("warnings", [])
            if not str(item).startswith((
                "ETF 的 T+0/T+1 属性尚未", "ETF权威交易属性覆盖不完整",
            ))
        ]
        if known_t0 < len(etfs) or known_limit < len(etfs):
            warnings.append(
                f"ETF权威交易属性覆盖不完整: T+0/T+1 {known_t0}/{len(etfs)}, "
                f"涨跌幅 {known_limit}/{len(etfs)}；未知证券禁止生成可执行订单。"
            )
        quality["warnings"] = warnings
        quality["etf_reference_coverage"] = {
            "total": len(etfs),
            "same_day_turnaround_known": known_t0,
            "price_limit_known": known_limit,
        }
        return snapshot

    def upsert_security_master(
        self,
        snapshot_id: str,
        securities: Sequence[dict[str, object]],
        observed_at: str,
        *, deactivate_missing: bool = True,
    ) -> int:
        now = _now()
        rows = [
            (
                str(item["symbol"]),
                str(item["name"]),
                str(item["exchange"]),
                str(item["security_type"]),
                str(item["board"]),
                _bool_int(item.get("risk_warning")),
                str(item["trading_status"]),
                str(item["price_tick"]),
                int(item.get("buy_lot_size", 100)),
                _bool_int(item.get("same_day_turnaround")),
                (
                    str(item["price_limit_ratio"])
                    if item.get("price_limit_ratio") is not None
                    else None
                ),
                int(bool(item.get("price_limit_exempt", False))),
                str(item.get("source", "unknown")),
                str(item.get("available_at") or observed_at),
                snapshot_id,
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
                1,
                now,
            )
            for item in securities
        ]
        with self._connect() as connection:
            if deactivate_missing:
                connection.execute("UPDATE security_master SET active = 0")
            connection.executemany(
                """
                INSERT INTO security_master VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name,
                    exchange=excluded.exchange,
                    security_type=excluded.security_type,
                    board=excluded.board,
                    risk_warning=excluded.risk_warning,
                    trading_status=excluded.trading_status,
                    price_tick=excluded.price_tick,
                    buy_lot_size=excluded.buy_lot_size,
                    same_day_turnaround=excluded.same_day_turnaround,
                    price_limit_ratio=excluded.price_limit_ratio,
                    price_limit_exempt=excluded.price_limit_exempt,
                    source=excluded.source,
                    available_at=excluded.available_at,
                    last_snapshot_id=excluded.last_snapshot_id,
                    raw_json=excluded.raw_json,
                    active=1,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def seed_history_targets(
        self,
        securities: Sequence[dict[str, object]],
        held_symbols: set[str],
        quotes: dict[str, dict[str, object]],
        start_date: date,
        end_date: date,
    ) -> int:
        now = _now()
        rows = []
        for item in securities:
            symbol = str(item["symbol"])
            turnover = float((quotes.get(symbol) or {}).get("turnover") or 0)
            rows.append(
                (
                    symbol,
                    str(item["name"]),
                    str(item["exchange"]),
                    str(item["security_type"]),
                    int(symbol in held_symbols),
                    turnover,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    "queued",
                    now,
                )
            )
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO history_sync_targets(
                    symbol, name, exchange, security_type, held, priority,
                    requested_start, requested_end, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    name=excluded.name,
                    exchange=excluded.exchange,
                    security_type=excluded.security_type,
                    held=excluded.held,
                    priority=excluded.priority,
                    requested_start=MIN(history_sync_targets.requested_start, excluded.requested_start),
                    requested_end=MAX(history_sync_targets.requested_end, excluded.requested_end),
                    status=CASE
                        WHEN history_sync_targets.requested_start > excluded.requested_start THEN 'queued'
                        WHEN history_sync_targets.requested_end < excluded.requested_end THEN 'queued'
                        ELSE history_sync_targets.status
                    END,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def queue_history_targets(self, symbols: set[str]) -> None:
        if not symbols:
            return
        placeholders = ",".join("?" for _ in symbols)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE history_sync_targets SET status = CASE "
                f"WHEN status = 'running' THEN 'running' "
                f"WHEN status = 'failed' THEN 'failed' ELSE 'queued' END, "
                f"attempt_count = CASE WHEN status = 'running' THEN attempt_count ELSE 0 END, "
                f"updated_at = ? WHERE symbol IN ({placeholders})",
                (_now(), *sorted(symbols)),
            )

    def recover_interrupted_history_syncs(self, stale_after_minutes: int = 60) -> int:
        cutoff = (
            datetime.now().astimezone() - timedelta(minutes=stale_after_minutes)
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE history_syncs
                SET status = 'failed', completed_at = ?, error = '同步进程在完成前中断'
                WHERE status = 'running' AND started_at <= ?
                """,
                (_now(), cutoff),
            )
            cursor = connection.execute(
                """
                UPDATE history_sync_targets
                SET status = 'queued', last_error = '上次同步进程中断，已自动重新排队', updated_at = ?
                WHERE status = 'running' AND NOT EXISTS (
                    SELECT 1 FROM history_syncs
                    WHERE history_syncs.symbol = history_sync_targets.symbol
                      AND history_syncs.status = 'running'
                )
                """,
                (_now(),),
            )
        return cursor.rowcount

    def pending_history_targets(
        self,
        limit: int,
        max_attempts: int,
        symbols: set[str] | None = None,
    ) -> list[dict[str, object]]:
        where = "status IN ('queued', 'failed') AND attempt_count < ?"
        parameters: list[object] = [max_attempts]
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            where += f" AND symbol IN ({placeholders})"
            parameters.extend(sorted(symbols))
        sql = (
            "SELECT * FROM history_sync_targets WHERE "
            + where
            + " ORDER BY held DESC, priority DESC, attempt_count ASC, symbol ASC"
        )
        if limit > 0:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def begin_history_sync(self, symbol: str) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO history_syncs(symbol, started_at, status) VALUES (?, ?, 'running')",
                (symbol, now),
            )
            connection.execute(
                """
                UPDATE history_sync_targets
                SET status = 'running', attempt_count = attempt_count + 1,
                    last_started_at = ?, last_error = NULL, updated_at = ?
                WHERE symbol = ?
                """,
                (now, now, symbol),
            )
        return int(cursor.lastrowid)

    def finish_history_sync(
        self,
        sync_id: int,
        symbol: str,
        result: dict[str, object],
    ) -> None:
        now = _now()
        latest = self.latest_date(symbol)
        warning = "; ".join(str(item) for item in result.get("warnings", [])) or None
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE history_syncs
                SET status = 'completed', completed_at = ?, error = ? WHERE sync_id = ?
                """,
                (now, warning, sync_id),
            )
            connection.execute(
                """
                UPDATE history_sync_targets
                SET status = 'completed', attempt_count = 0, last_completed_at = ?,
                    last_error = ?, raw_bars = ?, qfq_bars = ?, corporate_actions = ?,
                    latest_trade_date = ?, updated_at = ?
                WHERE symbol = ?
                """,
                (
                    now,
                    warning,
                    int(result.get("raw_bars", 0)),
                    int(result.get("qfq_bars", 0)),
                    int(result.get("corporate_actions", 0)),
                    latest.isoformat() if latest else None,
                    now,
                    symbol,
                ),
            )

    def fail_history_sync(self, sync_id: int, symbol: str, error: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE history_syncs
                SET status = 'failed', completed_at = ?, error = ? WHERE sync_id = ?
                """,
                (now, error, sync_id),
            )
            connection.execute(
                """
                UPDATE history_sync_targets
                SET status = 'failed', last_completed_at = ?, last_error = ?, updated_at = ?
                WHERE symbol = ?
                """,
                (now, error, now, symbol),
            )

    def upsert_bars(
        self,
        bars: Sequence[DailyBar],
        adjustment: str,
        source: str,
        available_at: str,
    ) -> int:
        if adjustment not in {"raw", "qfq"}:
            raise ValueError("adjustment must be raw or qfq")
        table = "raw_daily_bars" if adjustment == "raw" else "display_qfq_bars"
        rows = []
        for bar in bars:
            core = (
                bar.symbol,
                bar.trade_date.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.amount,
            )
            if adjustment == "raw":
                digest = hashlib.sha256(
                    json.dumps(core, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                rows.append(
                    core
                    + (
                        bar.outstanding_share,
                        bar.turnover,
                        source,
                        available_at,
                        digest,
                    )
                )
            else:
                rows.append(core + (source, available_at))
        if not rows:
            return 0
        with self._connect() as connection:
            if adjustment == "raw":
                connection.executemany(
                    "INSERT OR IGNORE INTO raw_daily_bar_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                connection.executemany(
                    """
                    INSERT INTO raw_daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, trade_date) DO UPDATE SET
                      open=excluded.open, high=excluded.high, low=excluded.low,
                      close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                      outstanding_share=excluded.outstanding_share, turnover=excluded.turnover,
                      source=excluded.source, available_at=excluded.available_at,
                      record_hash=excluded.record_hash
                    """,
                    rows,
                )
            else:
                connection.executemany(
                    """
                    INSERT INTO display_qfq_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, trade_date) DO UPDATE SET
                      open=excluded.open, high=excluded.high, low=excluded.low,
                      close=excluded.close, volume=excluded.volume, amount=excluded.amount,
                      source=excluded.source, available_at=excluded.available_at
                    """,
                    rows,
                )
        return len(rows)

    def upsert_actions(
        self,
        actions: Sequence[CorporateAction],
        source: str,
        available_at: str,
    ) -> int:
        rows = []
        for action in actions:
            raw_json = json.dumps(action.raw, ensure_ascii=False, sort_keys=True, default=str)
            identity = "|".join(
                [
                    action.symbol,
                    action.announcement_date.isoformat() if action.announcement_date else "",
                    action.ex_date.isoformat() if action.ex_date else "",
                    raw_json,
                ]
            )
            action_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            rows.append(
                (
                    action_id,
                    action.symbol,
                    _iso(action.announcement_date),
                    _iso(action.ex_date),
                    _iso(action.record_date),
                    action.cash_dividend_per_10,
                    action.bonus_shares_per_10,
                    action.transfer_shares_per_10,
                    action.status,
                    source,
                    available_at,
                    raw_json,
                )
            )
        if rows:
            with self._connect() as connection:
                connection.executemany(
                    "INSERT OR IGNORE INTO corporate_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        return len(rows)

    def upsert_industry_memberships(
        self,
        memberships: Sequence[IndustryMembership],
        source: str,
        available_at: str,
    ) -> int:
        rows = []
        for item in memberships:
            raw_json = json.dumps(item.raw, ensure_ascii=False, sort_keys=True, default=str)
            identity = "|".join(
                [
                    item.symbol,
                    item.effective_from.isoformat(),
                    item.standard_code or "",
                    item.industry_code or "",
                    raw_json,
                ]
            )
            rows.append(
                (
                    hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    item.symbol,
                    item.security_name,
                    item.effective_from.isoformat(),
                    item.standard,
                    item.standard_code,
                    item.industry_code,
                    item.section,
                    item.major,
                    item.middle,
                    item.minor,
                    item.organization,
                    source,
                    available_at,
                    raw_json,
                )
            )
        if rows:
            with self._connect() as connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO industry_memberships VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        return len(rows)

    def industry_at(self, symbol: str, as_of: date) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM industry_memberships
                WHERE symbol = ? AND effective_from <= ?
                ORDER BY effective_from DESC, standard_code
                """,
                (symbol, as_of.isoformat()),
            ).fetchall()
        latest_by_standard: dict[str, dict[str, object]] = {}
        for row in rows:
            item = dict(row)
            key = str(item.get("standard_code") or item.get("standard") or "unknown")
            latest_by_standard.setdefault(key, item)
        return list(latest_by_standard.values())

    def primary_industry_map(
        self, as_of: date, known_at: str | None = None
    ) -> dict[str, dict[str, object]]:
        available_filter = " AND available_at <= ?" if known_at else ""
        parameters: tuple[object, ...] = (
            (as_of.isoformat(), known_at) if known_at else (as_of.isoformat(),)
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM industry_memberships
                WHERE effective_from <= ?{available_filter}
                ORDER BY symbol, effective_from DESC
                """,
                parameters,
            ).fetchall()
        priority = {
            "申银万国行业分类标准": 0,
            "巨潮行业分类标准": 1,
            "证监会行业分类标准": 2,
        }
        choices: dict[str, dict[str, object]] = {}
        ranks: dict[str, tuple[int, str]] = {}
        for row in rows:
            item = dict(row)
            symbol = str(item["symbol"])
            rank = (
                priority.get(str(item.get("standard")), 99),
                str(item["effective_from"]),
            )
            current = ranks.get(symbol)
            if current is None or rank[0] < current[0] or (
                rank[0] == current[0] and rank[1] > current[1]
            ):
                ranks[symbol] = rank
                choices[symbol] = item
        return choices

    def seed_industry_targets(self, securities: Sequence[dict[str, object]]) -> int:
        now = _now()
        rows = [
            (str(item["symbol"]), str(item["name"]), "queued", now)
            for item in securities
            if item.get("security_type") == "stock"
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO industry_sync_targets(symbol, name, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def queue_industry_targets(self, symbols: set[str]) -> None:
        if not symbols:
            return
        placeholders = ",".join("?" for _ in symbols)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE industry_sync_targets SET status = CASE "
                f"WHEN status = 'failed' THEN 'failed' ELSE 'queued' END, "
                f"attempt_count = 0, updated_at = ? WHERE symbol IN ({placeholders})",
                (_now(), *sorted(symbols)),
            )

    def recover_interrupted_industry_syncs(self) -> int:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE industry_syncs SET status = 'failed', completed_at = ?,
                    error = '行业同步进程在完成前中断' WHERE status = 'running'
                """,
                (now,),
            )
            cursor = connection.execute(
                """
                UPDATE industry_sync_targets SET status = 'queued',
                    last_error = '上次行业同步进程中断，已自动重新排队', updated_at = ?
                WHERE status = 'running'
                """,
                (now,),
            )
        return cursor.rowcount

    def pending_industry_targets(
        self, limit: int, max_attempts: int, symbols: set[str] | None = None
    ) -> list[dict[str, object]]:
        where = "status IN ('queued', 'failed') AND attempt_count < ?"
        parameters: list[object] = [max_attempts]
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            where += f" AND symbol IN ({placeholders})"
            parameters.extend(sorted(symbols))
        sql = (
            "SELECT * FROM industry_sync_targets WHERE "
            + where
            + " ORDER BY attempt_count ASC, symbol ASC"
        )
        if limit > 0:
            sql += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def begin_industry_sync(self, symbol: str) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO industry_syncs(symbol, started_at, status) VALUES (?, ?, 'running')",
                (symbol, now),
            )
            connection.execute(
                """
                UPDATE industry_sync_targets SET status = 'running',
                    attempt_count = attempt_count + 1, last_started_at = ?,
                    last_error = NULL, updated_at = ? WHERE symbol = ?
                """,
                (now, now, symbol),
            )
        return int(cursor.lastrowid)

    def finish_industry_sync(self, sync_id: int, symbol: str, count: int) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE industry_syncs SET status = 'completed', completed_at = ? WHERE sync_id = ?",
                (now, sync_id),
            )
            connection.execute(
                """
                UPDATE industry_sync_targets SET status = 'completed', attempt_count = 0,
                    last_completed_at = ?, last_error = NULL, membership_count = ?, updated_at = ?
                WHERE symbol = ?
                """,
                (now, count, now, symbol),
            )

    def fail_industry_sync(self, sync_id: int, symbol: str, error: str) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE industry_syncs SET status = 'failed', completed_at = ?, error = ?
                WHERE sync_id = ?
                """,
                (now, error, sync_id),
            )
            connection.execute(
                """
                UPDATE industry_sync_targets SET status = 'failed', last_completed_at = ?,
                    last_error = ?, updated_at = ? WHERE symbol = ?
                """,
                (now, error, now, symbol),
            )

    def bars(
        self,
        symbol: str,
        adjustment: str = "raw",
        limit: int = 120,
        end_date: date | None = None,
    ) -> list[dict[str, object]]:
        table = "raw_daily_bars" if adjustment == "raw" else "display_qfq_bars"
        date_filter = " AND trade_date <= ?" if end_date else ""
        parameters: tuple[object, ...] = (
            (symbol, end_date.isoformat(), limit) if end_date else (symbol, limit)
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE symbol = ?{date_filter} "
                "ORDER BY trade_date DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def bars_between(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM raw_daily_bars WHERE symbol = ? AND trade_date >= ? "
                "AND trade_date <= ? ORDER BY trade_date",
                (symbol, start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    def bars_as_known(
        self,
        symbol: str,
        limit: int,
        end_date: date,
        known_at: str,
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY symbol, trade_date
                        ORDER BY available_at DESC, record_hash DESC
                    ) AS revision_rank
                    FROM raw_daily_bar_revisions
                    WHERE symbol = ? AND trade_date <= ? AND available_at <= ?
                )
                SELECT symbol, trade_date, open, high, low, close, volume, amount,
                       outstanding_share, turnover, source, available_at, record_hash
                FROM ranked WHERE revision_rank = 1
                ORDER BY trade_date DESC LIMIT ?
                """,
                (symbol, end_date.isoformat(), known_at, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def corporate_actions(
        self,
        symbol: str,
        known_by: date | None = None,
        available_by: str | None = None,
    ) -> list[dict[str, object]]:
        conditions = ["symbol = ?", "ex_date IS NOT NULL"]
        parameters: list[object] = [symbol]
        if known_by is not None:
            conditions.append("announcement_date IS NOT NULL")
            conditions.append("announcement_date <= ?")
            conditions.append("ex_date <= ?")
            parameters.extend((known_by.isoformat(), known_by.isoformat()))
        if available_by is not None:
            conditions.append("available_at <= ?")
            parameters.append(available_by)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM corporate_actions WHERE "
                + " AND ".join(conditions)
                + " ORDER BY ex_date, announcement_date, action_id",
                tuple(parameters),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = json.loads(str(item.pop("raw_json")))
            result.append(item)
        return result

    def symbols(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM raw_daily_bars ORDER BY symbol"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def security_master_rows(self, *, active_only: bool = True) -> list[dict[str, object]]:
        where = "WHERE active = 1" if active_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM security_master {where} ORDER BY symbol"
            ).fetchall()
        return [
            {**dict(row), "raw": json.loads(str(row["raw_json"]))}
            for row in rows
        ]

    def trading_dates(self, start: date, end: date) -> list[date]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT trade_date FROM raw_daily_bars "
                "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [date.fromisoformat(str(row[0])) for row in rows]

    def point_in_time_bar_revision_count(self, end: date, known_at: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM raw_daily_bar_revisions "
                "WHERE trade_date <= ? AND available_at <= ?",
                (end.isoformat(), known_at),
            ).fetchone()
        return int(row[0])

    def latest_date(self, symbol: str) -> date | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) FROM raw_daily_bars WHERE symbol = ?", (symbol,)
            ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def history_refresh_plan(self, symbols: set[str], completed_dates: list[str]) -> dict[str, object]:
        """Inspect recent authoritative trading dates, not only each symbol's latest bar."""
        if not symbols or not completed_dates:
            raise ValueError("历史增量检查需要证券池和权威的最近完整交易日")
        dates = sorted(set(completed_dates))
        cutoff = dates[-1]
        placeholders = ",".join("?" for _ in symbols)
        ordered = sorted(symbols)
        with self._connect() as connection:
            summaries = {row["symbol"]: dict(row) for row in connection.execute(
                f"SELECT symbol, MIN(trade_date) AS earliest, MAX(available_at) AS available_at "
                f"FROM raw_daily_bars WHERE symbol IN ({placeholders}) AND trade_date <= ? GROUP BY symbol",
                (*ordered, cutoff),
            )}
            rows = connection.execute(
                f"SELECT symbol, trade_date FROM raw_daily_bars WHERE symbol IN ({placeholders}) "
                "AND trade_date >= ? AND trade_date <= ?", (*ordered, dates[0], cutoff),
            ).fetchall()
            targets = {row["symbol"]: dict(row) for row in connection.execute(
                f"SELECT symbol, status, requested_end, last_completed_at, last_error FROM history_sync_targets "
                f"WHERE symbol IN ({placeholders})", ordered,
            )}
        present = {symbol: set() for symbol in symbols}
        for row in rows:
            present[row["symbol"]].add(row["trade_date"])
        needed, unavailable = [], []
        for symbol in ordered:
            summary = summaries.get(symbol)
            target = targets.get(symbol, {})
            required = [item for item in dates if summary is None or item >= summary["earliest"]]
            missing = [item for item in required if item not in present[symbol]]
            bootstrap = summary is None or (not target and summary["earliest"] > dates[0])
            if not missing and not bootstrap:
                continue
            item = {"symbol": symbol, "missing_dates": missing, "bootstrap": bootstrap,
                    "start": (date.fromisoformat(cutoff) - timedelta(days=730)).isoformat()
                    if bootstrap else missing[0]}
            if target.get("status") == "completed" and str(target.get("requested_end") or "") >= cutoff:
                unavailable.append({**item, "reason": target.get("last_error") or "同一截止日已请求，数据源未提供缺失日线"})
            else:
                needed.append(item)
        return {
            "through": cutoff, "needed": needed, "unavailable": unavailable,
            "latest_available_at": max((str(item["available_at"]) for item in summaries.values()), default=None),
        }

    def earliest_date(self, symbol: str) -> date | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(trade_date) FROM raw_daily_bars WHERE symbol = ?", (symbol,)
            ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def bar_revision_count(self, symbol: str, trade_date: date) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM raw_daily_bar_revisions WHERE symbol = ? AND trade_date = ?",
                (symbol, trade_date.isoformat()),
            ).fetchone()
        return int(row[0])

    def status(self) -> dict[str, object]:
        with self._connect() as connection:
            raw = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(trade_date), MAX(trade_date) FROM raw_daily_bars"
            ).fetchone()
            qfq = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM display_qfq_bars"
            ).fetchone()
            actions = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM corporate_actions"
            ).fetchone()
            master = connection.execute(
                "SELECT COUNT(*), SUM(active) FROM security_master"
            ).fetchone()
            target_rows = connection.execute(
                "SELECT status, COUNT(*) FROM history_sync_targets GROUP BY status"
            ).fetchall()
            attempts = connection.execute(
                "SELECT COUNT(*), SUM(status = 'failed') FROM history_syncs"
            ).fetchone()
            warnings = connection.execute(
                """
                SELECT COUNT(*) FROM history_sync_targets
                WHERE status = 'completed' AND last_error IS NOT NULL
                """
            ).fetchone()
            industry = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM industry_memberships"
            ).fetchone()
            industry_target_rows = connection.execute(
                "SELECT status, COUNT(*) FROM industry_sync_targets GROUP BY status"
            ).fetchall()
            etf_attributes = connection.execute(
                """
                SELECT COUNT(*),
                       SUM(same_day_turnaround IS NOT NULL),
                       SUM(price_limit_ratio IS NOT NULL)
                FROM etf_trade_attributes
                """
            ).fetchone()
            fundamentals = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(report_period), "
                "MAX(available_at) FROM fundamental_snapshots"
            ).fetchone()
        target_status = {str(row[0]): int(row[1]) for row in target_rows}
        target_total = sum(target_status.values())
        completed = target_status.get("completed", 0)
        base_coverage = min(int(raw[1]), target_total)
        industry_target_status = {
            str(row[0]): int(row[1]) for row in industry_target_rows
        }
        return {
            "raw_bars": int(raw[0]),
            "raw_symbols": int(raw[1]),
            "raw_start": raw[2],
            "raw_end": raw[3],
            "qfq_bars": int(qfq[0]),
            "qfq_symbols": int(qfq[1]),
            "corporate_actions": int(actions[0]),
            "action_symbols": int(actions[1]),
            "security_master_total": int(master[0]),
            "security_master_active": int(master[1] or 0),
            "history_targets": target_total,
            "history_target_status": target_status,
            "history_coverage_ratio": base_coverage / target_total if target_total else 0.0,
            "history_fresh_ratio": completed / target_total if target_total else 0.0,
            "history_sync_attempts": int(attempts[0]),
            "history_sync_failures": int(attempts[1] or 0),
            "history_targets_with_warnings": int(warnings[0]),
            "industry_memberships": int(industry[0]),
            "industry_symbols": int(industry[1]),
            "industry_target_status": industry_target_status,
            "etf_trade_attributes": int(etf_attributes[0]),
            "etf_same_day_turnaround_known": int(etf_attributes[1] or 0),
            "etf_price_limit_known": int(etf_attributes[2] or 0),
            "fundamental_snapshots": int(fundamentals[0]),
            "fundamental_symbols": int(fundamentals[1]),
            "fundamental_latest_period": fundamentals[2],
            "fundamental_latest_available_at": fundamentals[3],
        }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _bool_int(value: object) -> int | None:
    return None if value is None else int(bool(value))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
