"""Frozen ETF policy must govern prompts, nominal weights, and sized exposure."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ai_quant.etf_strategy import build_etf_candidates
from ai_quant.module_agents import module_agents
from ai_quant.module_pipeline import ModulePipeline, validate_module_output
from ai_quant.module_service import ModuleStore
from ai_quant.runner import RunnerResult
from test_etf_strategy import account, diversified_snapshot
from test_module_pipeline import AS_OF, plan_output, research_output


class EtfPipelinePolicyTests(unittest.TestCase):
    def setUp(self):
        self.scan = build_etf_candidates(diversified_snapshot(),
            policy={"etf_min_weight": ".50", "etf_max_weight": ".80", "etf_target_weight": ".70",
                    "sector_target_weight": ".30"})
        self.scan.update(scan_id="dynamic-etf", as_of_time=AS_OF)
        self.agent = module_agents("etf")[4]
        self.output = plan_output(self.agent.agent_id)
        self.output["valuations"] = []
        self.output["positions"] = [dict(self.output["positions"][0], symbol=symbol,
            target_weight=.30, entry_min=3.9, entry_max=4, stop_loss=3, take_profit=5,
            evidence_refs=["etf_research:E" + symbol]) for symbol in ("510300", "513330")]
        self.output["cash_weight"] = .40
        self.research = [{"agent_id": "etf_research", "findings": [
            {"symbol": symbol, "supporting_evidence": [{"evidence_id": "E" + symbol}],
             "opposing_evidence": []} for symbol in ("510300", "513330")]}]

    def validate(self):
        validate_module_output(self.agent, self.output, "etf", self.scan, account(), research=self.research)

    def test_dynamic_lower_bound_approves_below_legacy_floor(self):
        self.validate()
        self.scan["policy"]["etf_min_weight"] = ".90"
        self.scan["policy"]["etf_max_weight"] = ".98"
        with self.assertRaisesRegex(ValueError, "90%"):
            self.validate()

    def test_dynamic_upper_bound_rejects_and_post_lot_floor_matches(self):
        self.scan["policy"]["etf_max_weight"] = ".55"
        with self.assertRaisesRegex(ValueError, "55%"):
            self.validate()
        self.scan["policy"]["etf_max_weight"] = ".80"
        self.output["positions"][0]["target_weight"] = .251
        self.output["positions"][1]["target_weight"] = .249
        self.output["cash_weight"] = .50
        with self.assertRaisesRegex(ValueError, "整手"):
            self.validate()

    def test_decision_quantities_match_allocation_for_confirmed_and_planning_accounts(self):
        settings = SimpleNamespace(commission_fixed_per_order=1, commission_minimum=1)
        pipeline = ModulePipeline(settings, Mock(), Mock(), Mock())
        confirmed = dict(account(), funding_confirmed=True, positions_confirmed=True,
                         cash_available_cny="96000", positions=[{
                             "symbol": "510300", "quantity": 1000, "available_quantity": 1000}])
        for acct in (confirmed, account()):
            with self.subTest(confirmed=acct["positions_confirmed"]):
                decision = pipeline.build_decision("etf", self.scan, acct, copy.deepcopy(self.output))
                allocation = {row["symbol"]: row for row in decision["allocation"]["positions"]}
                self.assertEqual("planned", decision["allocation"]["status"])
                for position in decision["positions"]:
                    expected = allocation[position["symbol"]]
                    for field in ("target_quantity", "current_quantity", "delta_quantity"):
                        self.assertEqual(expected[field], position[field])
                    self.assertEqual(expected["target_quantity"], position["quantity"])
                first = decision["positions"][0]
                if acct["positions_confirmed"]:
                    self.assertEqual(1000, first["current_quantity"])
                    self.assertEqual(6500, first["delta_quantity"])
                else:
                    self.assertIsNone(first["current_quantity"])
                    self.assertIsNone(first["delta_quantity"])

    def test_context_and_all_role_prompts_use_frozen_policy(self):
        prompts = []

        class Runner:
            accepts_test_snapshots = True

            def run(self, agent, prompt, directory):
                prompts.append(prompt)
                identity = json.loads(prompt.splitlines()[1])
                if agent.stage.value in {"research", "challenge"}:
                    output = research_output(agent.agent_id)
                    prototype = output["findings"][0]
                    output["findings"] = []
                    for symbol in identity["candidate_symbols"]:
                        item = copy.deepcopy(prototype)
                        item.update(symbol=symbol, reply_to=[a.agent_id for a in module_agents("etf")[:3]]
                                    if agent.stage.value == "challenge" else [])
                        item["supporting_evidence"][0]["evidence_id"] = "E" + symbol
                        output["findings"].append(item)
                else:
                    output = plan_output(agent.agent_id)
                    output.update(status="defer", positions=[], cash_weight=1, valuations=[])
                return RunnerResult(output, directory / (agent.agent_id + ".json"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = ModuleStore(path)
            settings = SimpleNamespace(workspace=path, max_parallel_agents=3,
                commission_fixed_per_order=1, commission_minimum=1, commission_rate=None,
                stock_sell_stamp_duty_rate=.0005, market_data_max_age_seconds=300)
            snapshot = {"purpose": "verification", "as_of_time": AS_OF, "market": {}, "data_quality": {}}
            with patch("ai_quant.module_pipeline.source_tree_hash", return_value="fixture"):
                state = ModulePipeline(settings, store, Mock(), Runner()).run("etf", self.scan, snapshot)
            context = json.loads((store.directory / "etf" / state["run_id"] / "context.json").read_text(encoding="utf-8"))
            self.assertEqual("0.50", context["policy"]["etf_min_weight"])
            self.assertEqual("0.80", context["policy"]["etf_max_weight"])
            self.assertEqual("0.70", context["policy"]["etf_target_weight"])
            self.assertEqual("0.30", context["policy"]["sector_target_weight"])
            self.assertEqual("high_prosperity_growth", context["policy"]["sector_allocation_objective"])
            self.assertEqual(.8, context["policy"]["total_weight_cap"])
            self.assertEqual(6, len(prompts))
            self.assertTrue(all("冻结policy.etf_min_weight至etf_max_weight" in prompt for prompt in prompts))
            self.assertTrue(all("ETF批准方案90–98%" not in prompt for prompt in prompts))
            self.assertTrue(all("行业配置是主动收益目标" in prompt for prompt in prompts))
            self.assertTrue(all("全行业零配置必须说明" in prompt for prompt in prompts))
            self.assertTrue(all("无强制最低行业仓位" in prompt for prompt in prompts))

    def test_sector_reference_is_soft_not_a_forced_purchase(self):
        self.scan["policy"]["sector_target_weight"] = "0.40"
        self.scan["policy"]["sector_allocation_objective"] = "high_prosperity_growth"
        self.validate()  # Research may explain a lower sector allocation without forced buys.

    def test_excluded_hong_kong_cannot_enter_even_a_draft_position(self):
        self.scan["policy"]["excluded_equity_regions"] = ["hong_kong"]
        self.output["status"] = "defer"
        with self.assertRaisesRegex(ValueError, "用户当前排除"):
            self.validate()

    def test_excluded_region_stays_out_of_research_and_stale_allocation(self):
        from ai_quant.etf_strategy import select_etf_research_candidates, build_etf_allocation
        scan = build_etf_candidates(diversified_snapshot(),
            policy={"excluded_equity_regions": ["hong_kong"]})
        self.assertEqual(["hong_kong"], scan["policy"]["excluded_equity_regions"])
        self.assertNotIn("hong_kong", scan["policy"]["priority_research_buckets"])
        self.assertNotIn("513330", {r["symbol"] for r in select_etf_research_candidates(scan["candidates"])})
        held = select_etf_research_candidates(scan["candidates"], held_symbols={"513330"})
        self.assertIn("513330", {r["symbol"] for r in held})
        for row in scan["candidates"]:
            row["eligible"] = True
        allocation = build_etf_allocation(scan, account(), {"510300": .3, "513330": .3, "513500": .3})
        self.assertNotEqual("planned", allocation["status"])

    def test_sector_reference_rejects_invalid_weights(self):
        for weight in ("NaN", "-0.1", "1.1"):
            with self.subTest(weight=weight), self.assertRaisesRegex(ValueError, "sector_target"):
                build_etf_candidates(diversified_snapshot(), policy={"sector_target_weight": weight})

    def test_growth_broad_indices_do_not_count_as_sector_products(self):
        from ai_quant.etf_strategy import etf_sector
        for index in ("科创50指数", "纳斯达克100指数", "创业板50指数"):
            self.assertIsNone(etf_sector({"index_id": index}))
        self.assertEqual("semiconductors", etf_sector({"index_id": "科创芯片指数"}))
        self.assertEqual("healthcare", etf_sector({"index_id": "香港创新药指数"}))


if __name__ == "__main__":
    unittest.main()
