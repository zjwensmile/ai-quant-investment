import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_quant.cli import _persist_post_market_bars
from ai_quant.market_store import (
    CorporateAction,
    DailyBar,
    MarketStore,
    StoredEtfTradeAttribute,
)


class MarketStoreTests(unittest.TestCase):
    def test_post_market_snapshot_is_persisted_as_daily_bars_in_one_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            snapshot = {
                "as_of_time": "2026-08-26T15:20:29+08:00",
                "data_quality": {"available_at": "2026-08-26T15:20:29+08:00"},
                "market": {"is_trading_day": True, "session": "post_market"},
                "quotes": {
                    "600276": {
                        "open": 46.49, "high": 47.48, "low": 46.49,
                        "last": 46.74, "volume": 78295358,
                        "turnover": 3679128467,
                    },
                    "000001": {"open": None, "high": 1, "low": 1, "last": 1},
                },
            }
            self.assertEqual(1, _persist_post_market_bars(store, snapshot))
            bars = store.bars("600276", "raw")
            self.assertEqual("2026-08-26", bars[0]["trade_date"])
            self.assertEqual(46.74, bars[0]["close"])
            self.assertEqual("post_market_snapshot", bars[0]["source"])

            snapshot["market"]["session"] = "continuous"
            self.assertEqual(0, _persist_post_market_bars(store, snapshot))

    def test_exchange_attributes_are_applied_without_name_inference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            store.upsert_etf_trade_attributes(
                [
                    StoredEtfTradeAttribute(
                        symbol="563020",
                        exchange="SSE",
                        same_day_turnaround=False,
                        price_limit_ratio="0.10",
                        listing_date=date(2023, 12, 14),
                        fund_category="F112",
                        source_url="https://etf.sse.com.cn/fundlist/",
                        available_at="2026-08-23T22:00:00+08:00",
                        raw={"FUND_CODE": "563020"},
                    )
                ]
            )
            snapshot = {
                "securities": [
                    {
                        "symbol": "563020",
                        "security_type": "etf",
                        "same_day_turnaround": None,
                        "price_limit_ratio": None,
                    },
                    {
                        "symbol": "159001",
                        "security_type": "etf",
                        "same_day_turnaround": None,
                        "price_limit_ratio": None,
                    },
                ],
                "data_quality": {
                    "warnings": [
                        "ETF 的 T+0/T+1 属性尚未由权威证券主数据确认，当前值为 unknown。"
                    ]
                },
            }
            store.apply_etf_attributes(snapshot)
            store.apply_etf_attributes(snapshot)
            self.assertFalse(snapshot["securities"][0]["same_day_turnaround"])
            self.assertEqual("0.10", snapshot["securities"][0]["price_limit_ratio"])
            self.assertIsNone(snapshot["securities"][1]["same_day_turnaround"])
            self.assertEqual(
                1,
                snapshot["data_quality"]["etf_reference_coverage"][
                    "same_day_turnaround_known"
                ],
            )
            self.assertEqual(
                1,
                sum(
                    str(item).startswith("ETF权威交易属性覆盖不完整")
                    for item in snapshot["data_quality"]["warnings"]
                ),
            )

    def test_etf_attributes_use_version_known_at_snapshot_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            store.upsert_etf_trade_attributes([
                StoredEtfTradeAttribute(
                    symbol="563020", exchange="SSE", same_day_turnaround=False,
                    price_limit_ratio="0.10", listing_date=date(2023, 12, 14),
                    fund_category="old", source_url="https://example.com/old",
                    available_at="2026-08-23T12:00:00+08:00", raw={"version": 1},
                ),
                StoredEtfTradeAttribute(
                    symbol="563020", exchange="SSE", same_day_turnaround=True,
                    price_limit_ratio="0.20", listing_date=date(2023, 12, 14),
                    fund_category="new", source_url="https://example.com/new",
                    available_at="2026-08-24T12:00:00+08:00", raw={"version": 2},
                ),
            ])
            snapshot = {
                "as_of_time": "2026-08-23T18:00:00+08:00",
                "securities": [{
                    "symbol": "563020", "security_type": "etf",
                    "same_day_turnaround": None, "price_limit_ratio": None,
                }],
                "data_quality": {"warnings": []},
            }
            store.apply_etf_attributes(snapshot)
            security = snapshot["securities"][0]
            self.assertFalse(security["same_day_turnaround"])
            self.assertEqual("0.10", security["price_limit_ratio"])
            self.assertEqual("old", security["exchange_reference"]["fund_category"])

    def test_security_master_and_resumable_history_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            securities = [
                {
                    "symbol": "600000",
                    "name": "浦发银行",
                    "exchange": "SSE",
                    "security_type": "stock",
                    "board": "main",
                    "risk_warning": False,
                    "trading_status": "tradable",
                    "price_tick": "0.01",
                    "buy_lot_size": 100,
                    "same_day_turnaround": False,
                    "price_limit_ratio": None,
                    "price_limit_exempt": False,
                    "source": "fake",
                    "available_at": "2026-08-23T08:00:00+08:00",
                },
                {
                    "symbol": "563020",
                    "name": "红利低波ETF易方",
                    "exchange": "SSE",
                    "security_type": "etf",
                    "board": "not_applicable",
                    "risk_warning": False,
                    "trading_status": "tradable",
                    "price_tick": "0.001",
                    "buy_lot_size": 100,
                    "same_day_turnaround": None,
                    "price_limit_ratio": None,
                    "price_limit_exempt": False,
                    "source": "fake",
                    "available_at": "2026-08-23T08:00:00+08:00",
                },
            ]
            store.upsert_security_master(
                "snapshot-1", securities, "2026-08-23T08:00:00+08:00"
            )
            store.seed_history_targets(
                securities,
                {"563020"},
                {"600000": {"turnover": 1000}, "563020": {"turnover": 10}},
                date(2024, 8, 23),
                date(2026, 8, 23),
            )
            pending = store.pending_history_targets(10, 3)
            self.assertEqual("563020", pending[0]["symbol"])

            failed_sync = store.begin_history_sync("563020")
            store.fail_history_sync(failed_sync, "563020", "temporary error")
            retried = store.pending_history_targets(10, 3)
            self.assertIn("563020", {item["symbol"] for item in retried})

            successful_sync = store.begin_history_sync("600000")
            store.upsert_bars(
                [DailyBar("600000", date(2026, 8, 21), 9, 9.2, 8.9, 9.05, 100, 905)],
                "raw",
                "fake",
                "2026-08-23T08:00:00+08:00",
            )
            store.finish_history_sync(
                successful_sync,
                "600000",
                {
                    "raw_bars": 1,
                    "qfq_bars": 0,
                    "corporate_actions": 0,
                    "warnings": ["分红数据不可用"],
                },
            )
            status = store.status()
            self.assertEqual(2, status["security_master_active"])
            self.assertEqual(2, status["history_targets"])
            self.assertEqual(1, status["history_target_status"]["completed"])
            self.assertEqual(1, status["history_targets_with_warnings"])

            store.seed_history_targets(
                securities,
                {"563020"},
                {},
                date(2023, 8, 23),
                date(2026, 8, 23),
            )
            expanded = {
                item["symbol"]: item for item in store.pending_history_targets(10, 3)
            }
            self.assertEqual("queued", expanded["600000"]["status"])
            self.assertEqual("2023-08-23", expanded["600000"]["requested_start"])

    def test_interrupted_history_target_is_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            security = {
                "symbol": "563020",
                "name": "红利低波ETF易方",
                "exchange": "SSE",
                "security_type": "etf",
            }
            store.seed_history_targets(
                [security], set(), {}, date(2024, 8, 23), date(2026, 8, 23)
            )
            store.begin_history_sync("563020")
            store.seed_history_targets(
                [security], set(), {}, date(2024, 8, 23), date(2026, 8, 23)
            )
            store.queue_history_targets({"563020"})
            self.assertEqual([], store.pending_history_targets(10, 3))
            self.assertEqual(1, store.recover_interrupted_history_syncs(0))
            pending = store.pending_history_targets(10, 3)
            self.assertEqual("queued", pending[0]["status"])
            self.assertIn("中断", pending[0]["last_error"])

    def test_raw_revisions_are_preserved_and_qfq_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            first = DailyBar("600000", date(2026, 8, 21), 9, 9.2, 8.9, 9.05, 100, 905)
            corrected = DailyBar("600000", date(2026, 8, 21), 9, 9.2, 8.9, 9.06, 100, 906)
            qfq = DailyBar("600000", date(2026, 8, 21), 8, 8.2, 7.9, 8.05, 100, 905)
            store.upsert_bars([first], "raw", "sina", "2026-08-22T08:00:00+08:00")
            store.upsert_bars([corrected], "raw", "sina", "2026-08-23T08:00:00+08:00")
            store.upsert_bars([qfq], "qfq", "sina", "2026-08-23T08:00:00+08:00")
            self.assertEqual(2, store.bar_revision_count("600000", date(2026, 8, 21)))
            self.assertEqual(9.06, store.bars("600000", "raw")[0]["close"])
            self.assertEqual(8.05, store.bars("600000", "qfq")[0]["close"])

    def test_point_in_time_bar_query_uses_revision_known_at_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            first = DailyBar("600000", date(2026, 8, 21), 10, 10, 10, 10, 1, 10)
            revised = DailyBar("600000", date(2026, 8, 21), 11, 11, 11, 11, 1, 11)
            store.upsert_bars([first], "raw", "source", "2026-08-21T18:00:00+08:00")
            store.upsert_bars([revised], "raw", "source", "2026-08-23T18:00:00+08:00")
            old = store.bars_as_known(
                "600000", 1, date(2026, 8, 21), "2026-08-22T12:00:00+08:00"
            )
            new = store.bars_as_known(
                "600000", 1, date(2026, 8, 21), "2026-08-24T12:00:00+08:00"
            )
            self.assertEqual(10, old[0]["close"])
            self.assertEqual(11, new[0]["close"])
            self.assertEqual(
                [], store.bars("600000", "raw", end_date=date(2026, 8, 20))
            )

    def test_dividend_is_stored_with_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            action = CorporateAction(
                "600000", date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 7),
                1.5, 0, 0, "实施", {"公告日期": "2026-06-01", "派息": 1.5}
            )
            self.assertEqual(
                1,
                store.upsert_actions([action], "sina", "2026-06-01T18:00:00+08:00"),
            )


if __name__ == "__main__":
    unittest.main()
