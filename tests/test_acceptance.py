import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_quant.acceptance import AcceptanceAuditor
from ai_quant.config import Settings
from ai_quant.market_store import MarketStore
from ai_quant.scheduler import SchedulePlan
from ai_quant.store import AuditStore


class AcceptanceTests(unittest.TestCase):
    def test_explicit_manual_mode_makes_schedule_optional_without_hiding_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "system.json").write_text(json.dumps({
                "schedule": SchedulePlan.recommended().to_dict(),
            }), encoding="utf-8")
            settings = Settings(root, root / "runtime", root / "audit.sqlite3")
            audit = AuditStore(settings.database_path); audit.initialize()
            market = MarketStore(settings.resolved_market_database_path); market.initialize()
            with patch("ai_quant.acceptance.WindowsTaskScheduler.status", return_value={
                "installed": False, "enabled": False, "live_query_available": True, "tasks": [],
            }):
                result = AcceptanceAuditor(settings, audit, market).report()
            self.assertEqual("manual", result["operating_mode"])
            for name in ("daily_schedule_configured", "daily_schedule_installed", "daily_schedule_enabled"):
                self.assertFalse(result["checks"][name])
                self.assertIn(name, result["optional_checks"])
                self.assertNotIn(name, result["blockers"])
            self.assertFalse(result["schedule_status"]["installed"])
            self.assertEqual(
                all(value for name, value in result["checks"].items() if name not in result["optional_checks"]),
                result["manual_real_funds_trial_ready"],
            )

    def test_installed_disabled_or_unverified_schedule_is_a_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root, root / "runtime", root / "audit.sqlite3")
            audit = AuditStore(settings.database_path); audit.initialize()
            market = MarketStore(settings.resolved_market_database_path); market.initialize()
            for schedule_status in (
                {"installed": True, "enabled": False, "live_query_available": True},
                {"installed": True, "enabled": True, "live_query_available": False},
            ):
                with self.subTest(schedule_status=schedule_status), patch(
                    "ai_quant.acceptance.WindowsTaskScheduler.status", return_value=schedule_status,
                ):
                    result = AcceptanceAuditor(settings, audit, market).report()
                self.assertTrue(result["checks"]["daily_schedule_installed"])
                self.assertIn("daily_schedule_enabled", result["blockers"])

    def test_report_names_unmet_gates_instead_of_claiming_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "system.json").write_text("{}", encoding="utf-8")
            settings = Settings(root, root / "runtime", root / "runtime" / "audit.sqlite3")
            audit = AuditStore(settings.database_path); audit.initialize()
            market = MarketStore(settings.resolved_market_database_path); market.initialize()
            report = AcceptanceAuditor(settings, audit, market).report()
            self.assertFalse(report["research_ready"])
            self.assertIn("history_coverage_80pct", report["blockers"])
            self.assertIn("shadow_acceptance_days_configured", report["blockers"])
            self.assertIn("daily_schedule_configured", report["blockers"])
            self.assertIn("daily_schedule_installed", report["blockers"])

    def test_completed_no_action_shadow_settlement_counts_as_a_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "system.json").write_text("{}", encoding="utf-8")
            settings = Settings(root, root / "runtime", root / "runtime" / "audit.sqlite3")
            audit = AuditStore(settings.database_path); audit.initialize()
            market = MarketStore(settings.resolved_market_database_path); market.initialize()
            audit.create_run("run-1", "2026-08-01T15:30:00+08:00", "hash")
            plan = {
                "plan_id": "plan-1", "run_id": "run-1", "account_version": "v1",
                "status": "no_action", "daily_bar_cutoff": "2026-08-01", "orders": [],
            }
            audit.record_order_plan(plan)
            audit.record_shadow_settlement({
                "run_id": "run-1", "plan_id": "plan-1", "as_of_date": "2026-08-01",
                "model_version": "test", "status": "completed", "orders": 0,
                "completed": 0, "pending": 0,
            })
            self.assertEqual(1, AcceptanceAuditor(settings, audit, market)._shadow_days())

    def test_web_trace_gate_checks_only_agents_executed_in_this_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root, root / "runtime", root / "runtime" / "audit.sqlite3")
            audit = AuditStore(settings.database_path); audit.initialize()
            market = MarketStore(settings.resolved_market_database_path); market.initialize()
            trace = root / "news-trace.jsonl"
            trace.write_text("{}", encoding="utf-8")
            audit.get_run = Mock(return_value={"executions": [{
                "agent_id": "news_event", "trace_path": str(trace),
            }]})
            self.assertTrue(AcceptanceAuditor(settings, audit, market)._web_traces_present("run-1"))


if __name__ == "__main__":
    unittest.main()
