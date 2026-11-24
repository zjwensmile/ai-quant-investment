import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_quant.market_store import FundamentalSnapshot, MarketStore
from ai_quant.fundamentals import FundamentalSyncService, _normalize_report


class FundamentalPointInTimeTests(unittest.TestCase):
    def test_report_normalization_covers_growth_boards_and_retains_announcement_guard(self):
        period = date(2026, 6, 30)
        for symbol in ("600000", "688256", "300308", "300502", "301001"):
            with self.subTest(symbol=symbol):
                row = {"股票代码": symbol, "最新公告日期": "2026-08-20",
                       "营业总收入-营业总收入": 100, "净利润-净利润": 10}
                result = _normalize_report(row, period)
                self.assertEqual(symbol, result.symbol)
                self.assertEqual(100, result.revenue)
                row["最新公告日期"] = "2026-05-20"
                self.assertIsNone(_normalize_report(row, period))
        self.assertIsNone(_normalize_report({"股票代码": "510300", "最新公告日期": "2026-08-20"}, period))

    def test_growth_reports_persist_with_point_in_time_availability(self):
        class Provider:
            def fetch_financial_reports(self, period):
                return [{"股票代码": symbol, "最新公告日期": "2026-08-20",
                         "营业总收入-营业总收入": 100} for symbol in ("688256", "300308", "300502")]

        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            result = FundamentalSyncService(Provider(), store).sync([date(2026, 6, 30)])
            self.assertEqual(3, result["rows"])
            known = store.fundamental_map_as_known(date(2099, 1, 1), "2099-01-01T00:00:00+08:00")
            self.assertEqual({"688256", "300308", "300502"}, set(known))
            self.assertEqual({}, store.fundamental_map_as_known(date(2026, 8, 19), "2026-08-19T00:00:00+08:00"))

    def test_future_announcement_and_late_local_revision_are_not_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            old = FundamentalSnapshot(
                "600000", date(2025, 12, 31), date(2026, 3, 20), 100, 5,
                10, 4, 1, 8, 2, 10, 20, "银行", "https://example.com/old", {},
            )
            future = FundamentalSnapshot(
                "600000", date(2026, 6, 30), date(2026, 8, 30), 120, 10,
                12, 8, 1.2, 8.2, 2.2, 11, 21, "银行", "https://example.com/new", {},
            )
            store.upsert_fundamental_snapshots(
                [old], "test", "2026-03-21T10:00:00+08:00"
            )
            store.upsert_fundamental_snapshots(
                [future], "test", "2026-08-31T10:00:00+08:00"
            )
            known = store.fundamental_map_as_known(
                date(2026, 8, 25), "2026-08-25T15:00:00+08:00"
            )
            self.assertEqual("2025-12-31", known["600000"]["report_period"])


if __name__ == "__main__":
    unittest.main()
