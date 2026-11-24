import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ai_quant.backtest import SourceBacktester
from ai_quant.market_store import DailyBar, MarketStore


class SourceBacktestTests(unittest.TestCase):
    def test_defensive_source_uses_next_open_and_reports_friction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2025, 1, 1)
            security = {
                "symbol": "510300", "name": "测试ETF", "exchange": "SSE",
                "security_type": "etf", "board": "not_applicable",
                "risk_warning": None, "trading_status": "tradable",
                "price_tick": "0.001", "buy_lot_size": 100,
                "same_day_turnaround": False, "price_limit_ratio": "0.10",
                "price_limit_exempt": False, "source": "test",
                "available_at": "2024-12-31T16:00:00+08:00",
            }
            store.upsert_security_master(
                "snapshot-test", [security], "2024-12-31T16:00:00+08:00"
            )
            for index in range(165):
                trade_date = start + timedelta(days=index)
                price = 10 + index * 0.005
                store.upsert_bars(
                    [DailyBar("510300", trade_date, price, price * 1.002,
                              price * 0.998, price, 1_000_000, 100_000_000)],
                    "raw", "test", f"{trade_date.isoformat()}T16:00:00+08:00",
                )
            report = SourceBacktester(store).run(start, start + timedelta(days=164))
            self.assertEqual("completed", report["status"])
            defensive = report["sources"]["DEFENSIVE"]
            self.assertGreater(defensive["filled_trades"], 0)
            self.assertGreater(defensive["fees"], 0)
            self.assertGreater(defensive["turnover_ratio"], 0)
            self.assertEqual(0, report["sources"]["QUALITY_VALUE"]["filled_trades"])


if __name__ == "__main__":
    unittest.main()
