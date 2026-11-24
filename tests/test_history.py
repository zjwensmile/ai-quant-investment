from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_quant.history import HistorySyncService
from ai_quant.market_store import DailyBar, MarketStore
from ai_quant.securities import Exchange, SecurityType


class ProviderWithoutDividends:
    name = "fake"
    sources = {"history:600000:raw": "fake"}

    def fetch_daily_history(
        self, symbol, exchange_prefix, security_type, start_date, end_date, adjustment
    ):
        return [
            {
                "date": "2026-08-21",
                "open": 10,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 100,
                "amount": 1010,
            }
        ]

    def fetch_dividends(self, symbol):
        raise RuntimeError("No tables found")


class HistoryTests(unittest.TestCase):
    def test_missing_dividend_table_does_not_discard_price_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            result = HistorySyncService(ProviderWithoutDividends(), store).sync_symbol(
                "600000",
                Exchange.SSE,
                SecurityType.STOCK,
                date(2026, 8, 20),
                date(2026, 8, 23),
                include_qfq=False,
            )
            self.assertEqual(1, result["raw_bars"])
            self.assertIn("No tables found", result["warnings"][0])
            self.assertEqual(1, len(store.bars("600000", "raw")))

    def test_empty_increment_preserves_existing_history_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            store.upsert_bars(
                [DailyBar("600000", date(2026, 8, 21), 10, 10, 10, 10, 1, 10)],
                "raw", "fake", "2026-08-21T18:00:00+08:00",
            )
            provider = ProviderWithoutDividends()
            provider.fetch_daily_history = lambda *args: []
            result = HistorySyncService(provider, store).sync_symbol(
                "600000", Exchange.SSE, SecurityType.STOCK,
                date(2026, 8, 22), date(2026, 8, 24),
            )
            self.assertEqual(0, result["raw_bars"])
            self.assertIn("保留现有历史", result["warnings"][0])
            self.assertEqual(date(2026, 8, 21), store.latest_date("600000"))


if __name__ == "__main__":
    unittest.main()
