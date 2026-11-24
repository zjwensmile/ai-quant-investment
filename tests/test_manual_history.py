import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from ai_quant.cli import _sync_history_target, build_parser, command_history_sync
from ai_quant.config import Settings
from ai_quant.datahub import _calendar_context
from ai_quant.market_store import DailyBar, MarketStore
from ai_quant.store import AuditStore
from ai_quant.tasks import TaskController


class ManualHistoryTests(unittest.TestCase):
    dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root, self.root / "runtime", self.root / "audit.sqlite3")
        self.market = MarketStore(self.settings.resolved_market_database_path)
        self.market.initialize()
        self.audit = AuditStore(self.settings.database_path)
        self.audit.initialize()
        self.controller = TaskController(self.settings, self.audit, self.market)
        self.securities = [{"symbol": code, "name": code, "exchange": "SSE", "security_type": "stock",
                            "board": "main", "trading_status": "tradable", "price_tick": "0.01",
                            "risk_warning": False, "buy_lot_size": 100, "same_day_turnaround": False,
                            "price_limit_ratio": 0.1, "source": "test"}
                           for code in ("600000", "600001")]
        self.snapshots = self.settings.runtime_dir / "snapshots"
        self.snapshots.mkdir(parents=True)

    def bars(self, symbol, dates, available_at="2026-09-05T10:00:00+08:00"):
        self.market.upsert_bars([DailyBar(symbol, date.fromisoformat(day), 10, 10, 10, 10, 1, 10)
                                for day in dates], "raw", "test", available_at)

    def snapshot(self, name="market-before.json", as_of="2026-09-06T10:00:00+08:00"):
        data = {"snapshot_id": name, "as_of_time": as_of, "securities": self.securities,
                "account": {"version": "v1", "positions": []}, "quotes": {},
                "market": {"latest_completed_trade_date": self.dates[-1],
                           "recent_completed_trade_dates": self.dates},
                "data_quality": {"available_at": as_of, "status": "research_only"}}
        path = self.snapshots / name
        path.write_text(json.dumps(data), encoding="utf-8")
        moment = datetime.fromisoformat(as_of).timestamp()
        os.utime(path, (moment, moment))
        return path

    def test_current_last_bar_does_not_hide_missing_middle_trading_day(self):
        self.bars("600000", [self.dates[0], self.dates[2], self.dates[3]])
        self.bars("600001", self.dates)
        plan = self.market.history_refresh_plan({"600000", "600001"}, self.dates)
        self.assertEqual([{"symbol": "600000", "missing_dates": ["2026-09-02"],
                           "bootstrap": False, "start": "2026-09-02"}], plan["needed"])

    def test_complete_same_day_history_skips_download(self):
        for security in self.securities:
            self.bars(security["symbol"], self.dates)
        self.snapshot()
        with patch.object(self.controller, "_run_workflow_command") as run:
            result = self.controller._refresh_history("test")
        run.assert_not_called()
        self.assertEqual(0, json.loads(result.stdout)["requested_symbols"])

    def test_same_cutoff_success_without_new_bars_is_visible_not_retried(self):
        self.bars("600000", [self.dates[0]])
        self.market.seed_history_targets(self.securities[:1], set(), {}, date(2024, 9, 4), date(2026, 9, 4))
        sync = self.market.begin_history_sync("600000")
        self.market.finish_history_sync(sync, "600000", {"warnings": ["数据源暂无新增日线"]})
        plan = self.market.history_refresh_plan({"600000"}, self.dates)
        self.assertEqual([], plan["needed"])
        self.assertEqual(1, len(plan["unavailable"]))
        self.assertEqual(self.dates[1:], plan["unavailable"][0]["missing_dates"])
        self.assertIn("暂无新增", plan["unavailable"][0]["reason"])

    def test_failed_same_cutoff_is_retried(self):
        self.bars("600000", [self.dates[0]])
        self.market.seed_history_targets(self.securities[:1], set(), {}, date(2024, 9, 4), date(2026, 9, 4))
        sync = self.market.begin_history_sync("600000")
        self.market.fail_history_sync(sync, "600000", "network failure")
        self.assertEqual("600000", self.market.history_refresh_plan({"600000"}, self.dates)["needed"][0]["symbol"])

    def test_unknown_calendar_stops_before_history_or_research(self):
        path = self.snapshot()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["market"] = {}
        path.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(self.controller, "_run_workflow_command") as run:
            with self.assertRaisesRegex(ValueError, "权威交易日历不足"):
                self.controller._refresh_history("test")
        run.assert_not_called()

    def test_full_workflow_refreshes_only_gap_then_publishes_later_snapshot(self):
        self.bars("600000", [self.dates[0], self.dates[2], self.dates[3]])
        self.bars("600001", self.dates)
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[0] == "snapshot_raw":
                self.snapshot()
            elif "history-sync" in command:
                subset = json.loads(Path(command[command.index("--snapshot") + 1]).read_text(encoding="utf-8"))
                self.assertEqual(["600000"], [item["symbol"] for item in subset["securities"]])
                self.assertEqual("2026-09-02", command[command.index("--start") + 1])
                self.assertIn("--raw-only", command)
                self.assertIn("--incremental-window", command)
                self.assertNotIn("--no-dividends", command)
                self.bars("600000", ["2026-09-02"], "2026-09-06T10:01:00+08:00")
            elif command[0] == "snapshot_for_analysis":
                self.snapshot("market-after.json", "2026-09-06T10:03:00+08:00")
            elif command[0] == "enrich":
                _, data, plan = self.controller._history_plan()
                self.assertGreater(data["as_of_time"], plan["latest_available_at"])
                self.assertEqual("current", data["data_quality"]["history_status"])
            return subprocess.CompletedProcess(command, 0, "done", "")
        with patch.object(self.controller, "_command", side_effect=lambda action: [action]), \
             patch.object(self.controller, "_run_command", side_effect=run):
            result = self.controller.run_sync("full_workflow")
        self.assertEqual("completed", result["status"], result)
        self.assertEqual(1, sum("history-sync" in call for call in calls))
        self.assertEqual(["snapshot_raw", "fundamentals", "snapshot_for_analysis", "enrich", "analyze"],
                         [call[0] for call in calls if "history-sync" not in call])

    def test_new_snapshot_must_see_history_ingestion(self):
        for security in self.securities:
            self.bars(security["symbol"], self.dates, "2026-09-06T10:01:00+08:00")
        self.snapshot(as_of="2026-09-06T10:00:00+08:00")
        with self.assertRaisesRegex(ValueError, "早于历史行情入库时间"):
            self.controller._assert_history_visible()

    def test_explicit_incremental_window_does_not_reopen_failed_two_year_target(self):
        self.bars("600000", [self.dates[0], self.dates[3]])
        self.market.seed_history_targets(self.securities[:1], set(), {}, date(2024, 9, 4), date(2026, 9, 4))
        item = self.market.pending_history_targets(1, 3)[0]
        item.update(status="failed", last_error="previous interruption")
        with patch("ai_quant.cli.AkshareProvider"), patch("ai_quant.cli.HistorySyncService") as service:
            service.return_value.sync_symbol.return_value = {"raw_bars": 1}
            _, error = _sync_history_target(self.market, item, date(2026, 9, 4), False, True, 0,
                                           incremental_start=date(2026, 9, 2))
        self.assertIsNone(error)
        self.assertEqual(date(2026, 9, 2), service.return_value.sync_symbol.call_args.args[3])
        self.assertTrue(service.return_value.sync_symbol.call_args.kwargs["include_dividends"])

    def test_subset_cli_does_not_consume_other_global_queued_targets(self):
        self.market.seed_history_targets(self.securities, set(), {}, date(2024, 9, 4), date(2026, 9, 4))
        self.market.upsert_security_master("full", self.securities, "2026-09-06T09:00:00+08:00")
        self.securities = self.securities[:1]
        snapshot = self.snapshot()
        args = build_parser().parse_args([
            "history-sync", "--snapshot", str(snapshot), "--limit", "0", "--workers", "1",
            "--start", "2026-09-02", "--end", "2026-09-04", "--incremental-window", "--delay", "0",
        ])
        with patch("ai_quant.cli._settings", return_value=self.settings), \
             patch("ai_quant.cli._market_store", return_value=self.market), \
             patch("ai_quant.cli._sync_history_target", return_value=({"symbol": "600000"}, None)) as sync:
            self.assertEqual(0, command_history_sync(args))
        self.assertEqual(["600000"], [call.args[1]["symbol"] for call in sync.call_args_list])
        self.assertIn("600001", [item["symbol"] for item in self.market.pending_history_targets(0, 3)])
        with self.market._connect() as connection:
            self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM security_master WHERE active = 1").fetchone()[0])

    def test_calendar_context_observes_holidays_completed_bars_and_next_session(self):
        days = [date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 9)]
        cases = [
            ("2026-09-06T10:00:00+08:00", "2026-09-04", "2026-09-07T09:30:00+08:00"),
            ("2026-09-07T10:00:00+08:00", "2026-09-04", "2026-09-07T10:00:00+08:00"),
            ("2026-09-07T12:00:00+08:00", "2026-09-04", "2026-09-07T13:00:00+08:00"),
            ("2026-09-07T16:00:00+08:00", "2026-09-07", "2026-09-09T09:30:00+08:00"),
        ]
        for now, last, next_open in cases:
            with self.subTest(now=now):
                context = _calendar_context(days, datetime.fromisoformat(now))
                self.assertEqual(last, context["latest_completed_trade_date"])
                self.assertEqual(next_open, context["next_trading_session_open_at"])
        self.assertIsNone(_calendar_context([], datetime.fromisoformat(cases[0][0]))["latest_completed_trade_date"])


if __name__ == "__main__":
    unittest.main()
