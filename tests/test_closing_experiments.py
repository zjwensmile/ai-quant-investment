from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from ai_quant.closing_experiments import diagnose_closing_history, normalize_minutes, run_closing_experiments, diagnose_daily_gap_proxy, fetch_sina_five_minutes, minute_dataset_end_time


def fixtures(period=1):
    snapshot = {"market": {"recent_completed_trade_dates": ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07"]},
                "securities": [{"symbol": "600001", "security_type": "stock"}]}
    scan = {"scan_id": "today-selected", "as_of_time": "2026-09-08T14:00:00+08:00",
            "candidates": [{"symbol": "600001", "security": snapshot["securities"][0]}]}
    rows = []
    for day, price in (("2026-09-02", "10.0"), ("2026-09-03", "10.2"), ("2026-09-04", "10.3")):
        for hour in ("09:30:00", "13:00:00"):
            start = datetime.fromisoformat(day + "T" + hour + "+08:00")
            for n in range(period, 121, period):
                rows.append({"time": (start + timedelta(minutes=n)).isoformat(), "open": price, "high": price,
                             "low": price, "close": price, "volume": str(1000 * period),
                             "amount": str(Decimal(price) * 100000 * period)})
    return snapshot, scan, {"600001": rows}


class ClosingExperimentsTests(unittest.TestCase):
    def test_dataset_end_uses_all_raw_bar_times_not_only_trade_samples(self):
        data = {"600001": [{"time": "2026-09-08T15:00:00+08:00"}],
                "600002": [{"时间": "2026-09-09 09:35:00"},
                           {"time": "2026-09-09T01:40:00+00:00"}, {"time": "invalid"}]}
        self.assertEqual("2026-09-09T09:40:00+08:00", minute_dataset_end_time(data))
        self.assertIsNone(minute_dataset_end_time({"600001": []}))

    def test_report_records_future_dataset_without_backdating_it_to_old_selection(self):
        snapshot, scan, data = fixtures()
        data["600001"].append({"time": "2026-09-09T09:35:00+08:00"})
        with tempfile.TemporaryDirectory() as directory:
            result = run_closing_experiments(snapshot, scan, directory, minute_loader=lambda symbol: data[symbol])
            raw = json.loads(Path(result["raw_minutes_path"]).read_text(encoding="utf-8"))
            saved = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual("2026-09-09T09:35:00+08:00", result["dataset_end_time"])
        self.assertEqual(scan["as_of_time"], result["selection_observed_at"])
        self.assertEqual(raw["retrieved_at"], result["retrieved_at"])
        self.assertEqual(result, saved)
        self.assertGreater(datetime.fromisoformat(result["dataset_end_time"]),
                           datetime.fromisoformat(result["selection_observed_at"]))

    def test_sina_uses_real_amount_and_verifies_share_units(self):
        rows = [{"day": f"2026-09-03 10:{n:02d}:00", "open": "10", "high": "10.1", "low": "9.9", "close": "10",
                 "volume": "10000", "amount": "100000"} for n in range(30)]
        response = Mock(text="=(" + json.dumps(rows) + ");")
        with patch("requests.get", return_value=response):
            result = fetch_sina_five_minutes("600001")
        self.assertEqual("100", result[0]["volume"])
        self.assertEqual("10000", result[0]["provider_volume"])
        self.assertEqual("100000", result[0]["amount"])
        for row in rows:
            del row["amount"]
        response.text = "=(" + json.dumps(rows) + ");"
        with patch("requests.get", return_value=response), self.assertRaisesRegex(ValueError, "不伪造VWAP"):
            fetch_sina_five_minutes("600001")

    def test_all_eastmoney_failures_can_fall_back_without_mixing_periods(self):
        snapshot, scan, data = fixtures(5)
        with tempfile.TemporaryDirectory() as directory:
            with patch("akshare.stock_zh_a_hist_min_em", side_effect=TimeoutError("eastmoney unavailable")), patch(
                "ai_quant.closing_experiments.fetch_sina_five_minutes", return_value=data["600001"]):
                result = run_closing_experiments(snapshot, scan, directory)
        self.assertEqual("sina", result["minute_source"])
        self.assertEqual(5, result["period_minutes"])
        self.assertIn("eastmoney:600001", result["errors"])

    def test_daily_proxy_cannot_replace_minute_validation(self):
        snapshot, scan, _ = fixtures()
        daily = {"600001": [{"trade_date": "2026-09-03", "close": 10, "source": "real_daily_source"},
                             {"trade_date": "2026-09-04", "open": 10.2, "close": 10.1}]}
        proxy = diagnose_daily_gap_proxy(snapshot, scan, daily)
        self.assertEqual("daily_close_next_open_proxy", proxy["experiment_type"])
        self.assertFalse(proxy["validates_tail_session_strategy"])
        self.assertFalse(proxy["execution_model_applied"])
        self.assertEqual(1, proxy["sample_count"])
        minute_result = diagnose_closing_history(snapshot, scan, {})
        self.assertEqual(0, minute_result["sample_count"])
        self.assertEqual(5, len(minute_result["days"]))
        self.assertTrue(all(day["coverage"] == 0 for day in minute_result["days"]))

    def test_current_selection_is_never_reported_as_pit_or_stable(self):
        result = diagnose_closing_history(*fixtures())
        self.assertEqual("selected_today_retrospective", result["experiment_type"])
        self.assertFalse(result["pit_selection_backtest"])
        self.assertFalse(result["stable_profitability_validated"])
        self.assertEqual("insufficient_history", result["status"])
        self.assertEqual(1, result["sample_count"])
        self.assertTrue(result["assumptions"]["simultaneous_5000_cny_trades_must_not_be_reported_as_module_returns"])
        self.assertEqual(2, len(result["temporal_stability_observation"]))

    def test_signal_cannot_see_post_1450_prices(self):
        snapshot, scan, data = fixtures()
        baseline = diagnose_closing_history(snapshot, scan, data)
        mutated = deepcopy(data)
        for row in mutated["600001"]:
            if row["time"].startswith("2026-09-03T14:55"):
                row.update(high="100", close="100")
        result = diagnose_closing_history(snapshot, scan, mutated)
        before = next(r for r in baseline["details"] if r["trade_date"] == "2026-09-03")
        after = next(r for r in result["details"] if r["trade_date"] == "2026-09-03")
        self.assertEqual(before["signal"], after["signal"])
        self.assertTrue(after["entry_bar_end"].startswith("2026-09-03T14:51"))

    def test_t1_costs_and_exit_deadline_are_explicit(self):
        result = diagnose_closing_history(*fixtures())
        trade = next(r for r in result["details"] if r["status"] == "simulated_bar_proxy")
        self.assertGreater(trade["exit_bar_end"][:10], trade["entry_bar_end"][:10])
        self.assertLessEqual(trade["exit_bar_end"][11:16], "10:00")
        buy = Decimal("10.2") * Decimal("1.001") * 400 + 1
        proceeds = Decimal("10.3") * Decimal(".999") * 400 * Decimal(".9995") - 1
        self.assertAlmostEqual(float(proceeds - buy), trade["net_pnl_cny"])
        self.assertEqual("high", trade["opening_branch"])

    def test_high_open_close_trigger_exits_only_on_following_bar(self):
        snapshot, scan, data = fixtures()
        for row in data["600001"]:
            if row["time"].startswith("2026-09-04T09:31"):
                row.update(close="10.1", low="10.1")
        trade = next(r for r in diagnose_closing_history(snapshot, scan, data)["details"] if r["trade_date"] == "2026-09-03")
        self.assertTrue(trade["exit_bar_end"].startswith("2026-09-04T09:32"))

    def test_t1_uses_next_calendar_session_not_next_calendar_date(self):
        snapshot, scan, data = fixtures()
        for row in data["600001"]:
            if row["time"].startswith("2026-09-04"):
                row.update(open="10.5", close="10.5", high="10.5", low="10.5", amount="1050000")
        trade = next(r for r in diagnose_closing_history(snapshot, scan, data)["details"] if r["trade_date"] == "2026-09-04")
        self.assertEqual("2026-09-07", trade["next_trade_date"])
        self.assertEqual("unresolved_position", trade["status"])

    def test_missing_signal_minute_is_unavailable(self):
        snapshot, scan, data = fixtures()
        data["600001"] = [r for r in data["600001"] if not r["time"].startswith("2026-09-03T10:05")]
        record = next(r for r in diagnose_closing_history(snapshot, scan, data)["details"] if r["trade_date"] == "2026-09-03")
        self.assertEqual("unavailable", record["status"])
        self.assertIsNone(record["net_return"])

    def test_locked_limit_down_does_not_assume_exit(self):
        snapshot, scan, data = fixtures()
        for row in data["600001"]:
            if row["time"].startswith("2026-09-04T09:") or row["time"].startswith("2026-09-04T10:00"):
                row.update(open="9.18", close="9.18", low="9.18", high="9.18")
        trade = next(r for r in diagnose_closing_history(snapshot, scan, data)["details"] if r["trade_date"] == "2026-09-03")
        self.assertEqual("unresolved_position", trade["status"])
        self.assertIsNone(trade["net_return"])

    def test_next_day_suspension_is_unresolved_not_hindsight_entry_filter(self):
        snapshot, scan, data = fixtures()
        snapshot["historical_trade_rules"] = {"600001": {"2026-09-04": {"trading_status": "suspended"}}}
        trade = next(r for r in diagnose_closing_history(snapshot, scan, data)["details"] if r["trade_date"] == "2026-09-03")
        self.assertEqual("next_day_suspended_or_rules_changed", trade["reason"])
        self.assertEqual("unresolved_position", trade["status"])
        self.assertTrue(trade["entry_bar_end"].startswith("2026-09-03"))
        self.assertIsNone(trade["net_return"])

    def test_five_minute_precision_is_separate(self):
        result = diagnose_closing_history(*fixtures(5), period=5)
        self.assertEqual(5, result["period_minutes"])
        trade = next(r for r in result["details"] if r["status"] == "simulated_bar_proxy")
        self.assertTrue(trade["entry_bar_end"].startswith("2026-09-03T14:55"))

    def test_missing_open_and_zero_volume_not_filled(self):
        snapshot, scan, data = fixtures()
        for row in data["600001"]:
            if row["time"].startswith("2026-09-03T14:51"):
                row["volume"] = "0"
        trade = next(r for r in diagnose_closing_history(snapshot, scan, data)["details"] if r["trade_date"] == "2026-09-03")
        self.assertEqual("entry_lot_volume_or_limit_blocked", trade["reason"])
        self.assertEqual([], normalize_minutes([{"time": "2026-09-03T09:31:00", "open": 0}]))

    def test_runner_saves_actual_rows_sensitivity_and_errors(self):
        snapshot, scan, data = fixtures()
        scan["candidates"].append({"symbol": "600002"})
        def loader(symbol):
            if symbol == "600002":
                raise TimeoutError("source unavailable")
            return data[symbol]
        with tempfile.TemporaryDirectory() as directory:
            result = run_closing_experiments(snapshot, scan, directory, minute_loader=loader)
            self.assertEqual(2, len(result["threshold_sensitivity"]))
            self.assertIn("600002", result["errors"])
            raw = json.loads(Path(result["raw_minutes_path"]).read_text(encoding="utf-8"))
            self.assertEqual(data["600001"], raw["minutes"]["600001"])
            self.assertTrue(Path(result["report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
