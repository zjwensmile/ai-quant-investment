import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ai_quant.evaluation import EvaluationEngine, evaluation_report
from ai_quant.market_store import DailyBar, MarketStore
from ai_quant.store import AuditStore


class EvaluationTests(unittest.TestCase):
    def test_prediction_is_evaluated_after_trading_session_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = AuditStore(root / "audit.sqlite3")
            audit.initialize()
            market = MarketStore(root / "market.sqlite3")
            market.initialize()
            audit.create_run("run-1", "2026-08-01T15:30:00+08:00", "hash")
            audit.start_execution("exec-1", "run-1", "trend", "research", "terra", "medium", "hash")
            output = {
                "as_of_time": "2026-08-01T15:30:00+08:00",
                "decisions": [{
                    "decision_id": "d1", "subject": "test", "symbol": "600000",
                    "strategy": "DEFENSIVE", "direction": "bullish",
                    "forecast": {"horizon_days": 2, "benchmark": "cash",
                                 "reference_price": 10, "p_up": .6, "p_neutral": .3,
                                 "p_down": .1, "return_up": .1, "return_neutral": 0,
                                 "return_down": -.1, "signal_expires_at": None},
                }],
            }
            audit.record_agent_artifacts(
                "run-1", "exec-1", "trend", output,
                {"feature_coverage": {"daily_bar_cutoff": "2026-08-01"}},
            )
            bars = [
                DailyBar("600000", date(2026, 8, 1) + timedelta(days=i),
                         10 + i / 2, 10 + i / 2, 10 + i / 2, 10 + i / 2, 1, 10)
                for i in range(3)
            ]
            market.upsert_bars(bars, "raw", "fake", "2026-08-03T18:00:00+08:00")
            result = EvaluationEngine(audit, market).evaluate_due(date(2026, 8, 3))
            self.assertEqual(1, result["completed"])
            report = evaluation_report(audit.evaluation_rows())
            self.assertEqual(1, report["by_source"]["DEFENSIVE"]["evaluated"])
            self.assertAlmostEqual(
                .1, report["by_source"]["DEFENSIVE"]["mean_realized_return"]
            )


if __name__ == "__main__":
    unittest.main()
