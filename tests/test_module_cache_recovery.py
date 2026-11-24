"""Only intact research for the exact failed request can be reused."""

from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_quant.module_agents import module_agents
from ai_quant.module_pipeline import ModulePipeline
from ai_quant.module_service import ModuleStore
from ai_quant.schemas import schema_for
from tests.test_module_pipeline import AS_OF, ResumeContractRunner, account_audit, scan_data


class ModuleCacheRecoveryTests(unittest.TestCase):
    def run_pair(self, mutation):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, audit = ModuleStore(root), account_audit()
            settings = SimpleNamespace(workspace=root, max_parallel_agents=3,
                commission_fixed_per_order=1, commission_minimum=1, market_data_max_age_seconds=300,
                commission_rate=None, stock_sell_stamp_duty_rate=.0005)
            scan = scan_data()
            snapshot = {"purpose": "live_decision", "as_of_time": AS_OF,
                        "market": {"execution_eligible": True}, "data_quality": {"status": "healthy"}}
            with patch("ai_quant.module_pipeline.now_iso", return_value="2026-09-09T14:51:00+08:00"), \
                 patch("ai_quant.module_pipeline.source_tree_hash", return_value="isolated-fixture"), \
                 patch("ai_quant.module_library.publish_research_library"), \
                 patch("ai_quant.module_library.load_research_context", return_value={}) as library, \
                 patch("ai_quant.module_pipeline.module_agents", side_effect=module_agents) as agents, \
                 patch("ai_quant.module_pipeline.schema_for", side_effect=schema_for) as schemas:
                with self.assertRaisesRegex(RuntimeError, "controlled failure"):
                    ModulePipeline(settings, store, audit, ResumeContractRunner(True)).run("value", scan, snapshot)
                old = store.latest("value", "analysis")
                business = next(r for r in old["research"] if r["agent_id"] == "value_business")
                if mutation == "corrupt_json":
                    Path(business["output_path"]).write_text('{"broken":', encoding="utf-8")
                elif mutation == "invalid_contract":
                    Path(business["output_path"]).write_text('{}', encoding="utf-8")
                elif mutation == "missing_trace":
                    Path(business["trace_path"]).unlink()
                elif mutation == "legacy":
                    business.pop("request_sha256")
                    store.save("value", "analysis", old, old["run_id"])
                elif mutation == "library":
                    library.return_value = {"new_fact": "New evidence available before this frozen cutoff"}
                elif mutation == "scan_policy":
                    scan["policy"] = {"research_focus": "different business criteria"}
                elif mutation == "reasoning":
                    agents.side_effect = lambda module: [replace(a, reasoning_effort="low")
                        if a.agent_id == "value_business" else a for a in module_agents(module)]
                elif mutation == "schema":
                    schemas.side_effect = lambda kind: {**schema_for(kind), "description": "Revised output contract"}
                elif mutation == "paused":
                    old["status"] = "paused"
                    store.save("value", "analysis", old, old["run_id"])
                second = ResumeContractRunner()
                state = ModulePipeline(settings, store, audit, second).run("value", scan, snapshot)
                return state, {agent for agent, _ in second.calls}

    def test_bad_cache_retries_only_affected_specialist_and_finishes(self):
        for mutation in ("corrupt_json", "invalid_contract", "missing_trace"):
            with self.subTest(mutation=mutation):
                state, called = self.run_pair(mutation)
                self.assertEqual("completed", state["status"])
                self.assertEqual({"value_business", "value_accounts", "value_challenge",
                                  "value_portfolio", "value_risk"}, called)
                business = next(r for r in state["research"] if r["agent_id"] == "value_business")
                self.assertTrue(business["cache_rejection"])
                reused = [r["agent_id"] for r in state["research"] if r.get("reused_from_run_id")]
                self.assertEqual(["value_valuation"], reused)

    def test_complete_request_change_or_nonfailed_run_prevents_reuse(self):
        for mutation in ("library", "scan_policy", "schema", "paused"):
            with self.subTest(mutation=mutation):
                state, called = self.run_pair(mutation)
                self.assertEqual(6, len(called))
                self.assertFalse(any(r.get("reused_from_run_id") for r in state["research"]))

    def test_legacy_or_changed_agent_request_is_researched_again(self):
        for mutation in ("legacy", "reasoning"):
            with self.subTest(mutation=mutation):
                state, called = self.run_pair(mutation)
                self.assertEqual(5, len(called))
                self.assertIn("value_business", called)
                self.assertNotIn("value_valuation", called)
                self.assertTrue(all(len(r["request_sha256"]) == 64 for r in state["research"]))


if __name__ == "__main__":
    unittest.main()
