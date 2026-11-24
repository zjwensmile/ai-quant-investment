from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_quant.etf_strategy import build_etf_candidates, build_etf_allocation, build_t_plan, government_bond_buy_excluded, etf_buy_excluded, etf_market_region, select_etf_research_candidates, etf_diversification_error, etf_sector
from ai_quant.providers.etf_facts import parse_etf_facts, select_etf_fact_symbols, fetch_etf_facts
from ai_quant.module_agents import module_agents


NOW = "2026-09-09T10:00:00+08:00"


def snapshot():
    return {"as_of_time": NOW, "market": {"latest_completed_trade_date": "2026-09-08"},
            "securities": [{"symbol": "510300", "name": "测试ETF", "exchange": "SSE", "security_type": "etf",
                            "trading_status": "tradable", "same_day_turnaround": False, "price_tick": "0.001",
                            "exchange_reference": {"source_url": "https://www.sse.com.cn/rules", "available_at": NOW}}],
            "quotes": {"510300": {"last": "4", "turnover": "1000000000", "available_at": NOW}},
            "etf_facts": {"510300": {"is_etf": True, "aum_cny": "94872000000", "aum_as_of": "2026-06-30",
                                        "aum_available_at": NOW, "aum_source_url": "https://fundf10.eastmoney.com/jbgk_510300.html",
                                        "aum_basis": "reported_net_assets", "index_id": "沪深300指数",
                                        "asset_group": "a_share_equity", "metadata_available_at": NOW,
                                        "metadata_source_url": "https://fundf10.eastmoney.com/jbgk_510300.html"}}}


def account():
    return {"planned_capital_cny": "100000", "funding_confirmed": False, "positions_confirmed": False,
            "cash_available_cny": None, "positions": [], "commission_fixed_per_order": "1"}


def diversified_snapshot():
    data = snapshot()
    for symbol, group, index in (("513330", "hong_kong_equity", "恒生互联网指数"), ("518880", "gold", "黄金9999")):
        data["securities"].append(dict(data["securities"][0], symbol=symbol))
        data["quotes"][symbol] = dict(data["quotes"]["510300"])
        data["etf_facts"][symbol] = dict(data["etf_facts"]["510300"], asset_group=group, index_id=index)
    return data


class EtfCandidateTests(unittest.TestCase):
    def test_sector_uses_underlying_not_legal_fund_name(self):
        row = {"name": "通信ETF", "facts": {"index_id": "中证全指通信设备指数",
               "source_fields": {"基金全称": "通信交易型开放式指数证券投资基金"}}}
        self.assertEqual("technology", etf_sector(row))
        row["facts"]["index_id"] = "沪深300指数"
        self.assertIsNone(etf_sector(row))
        for index in ("能源化工期货指数", "有色金属期货指数"):
            row["facts"]["index_id"] = index
            self.assertIsNone(etf_sector(row))
        self.assertEqual("unknown", etf_market_region({"name": "通信ETF"}))

    def test_eligible_core_and_sectors_precede_unknown_sector_placeholders(self):
        rows = [{"symbol": code, "name": name, "asset_group": group, "eligible": True, "score": score}
                for code, name, group, score in (
                    ("588000", "科创50ETF", "a_share_equity", 99),
                    ("510300", "沪深300ETF", "a_share_equity", 60),
                    ("513120", "港股创新药ETF", "hong_kong_equity", 98),
                    ("513180", "恒生科技ETF", "hong_kong_equity", 50),
                    ("513500", "标普500ETF", "overseas_equity", 30),
                    ("513100", "纳指ETF", "overseas_equity", 30),
                    ("515880", "通信ETF", "a_share_equity", 20))]
        rows += [{"symbol": f"15990{i}", "name": name, "eligible": False, "score": 100}
                 for i, name in enumerate(("消费ETF", "银行ETF", "电力ETF", "军工ETF"))]
        selected = select_etf_research_candidates(rows, 7)
        self.assertEqual(["510300", "513180", "513500", "513100"], [r["symbol"] for r in selected[:4]])
        self.assertTrue(all(r["eligible"] for r in selected))

    def test_all_debt_and_cash_forms_are_excluded_from_candidate_and_t(self):
        for name in ("信用债ETF", "公司债ETF", "可转债ETF", "科创债ETF", "短融ETF", "货币ETF"):
            with self.subTest(name=name):
                data = snapshot(); data["securities"][0]["name"] = name
                row = build_etf_candidates(data)["candidates"][0]
                self.assertTrue(etf_buy_excluded(row))
                self.assertFalse(row["eligible"])
                row["eligible"] = True  # A stale approval cannot bypass the mandate.
                self.assertEqual([], build_t_plan(row, {"symbol": "510300", "quantity": 1000},
                                 dict(account(), funding_confirmed=True, positions_confirmed=True))["legs"])
        for row in ({"name": "自由现金流ETF", "asset_group": "a_share_equity"},
                    {"name": "Cash Flow ETF", "asset_group": "a_share_equity"}):
            self.assertFalse(etf_buy_excluded(row))

    def test_core_regions_and_sectors_survive_twelve_candidate_cutoff(self):
        rows = [{"symbol": f"51{i:04}", "name": "A股ETF", "asset_group": "a_share_equity",
                 "score": 100 - i, "eligible": True} for i in range(20)]
        rows += [{"symbol": code, "name": name, "score": 1, "eligible": True} for code, name in
                 (("513100", "纳指ETF"), ("513500", "标普500ETF"), ("513330", "恒生互联网ETF"),
                  ("513520", "日经ETF"), ("513030", "德国ETF"), ("518880", "黄金ETF"))]
        rows.append({"symbol": "511360", "name": "短融ETF", "score": 999, "eligible": True})
        selected = select_etf_research_candidates(rows, 12)
        self.assertEqual(12, len(selected))
        self.assertTrue({"513100", "513500", "513330"}.issubset(
            {row["symbol"] for row in selected}))
        self.assertNotIn("511360", {row["symbol"] for row in selected})
        rows += [{"symbol": f"15990{i}", "name": name, "score": 0, "eligible": True} for i, name in
                 enumerate(("芯片ETF", "医药ETF", "银行ETF", "消费ETF", "能源ETF", "军工ETF"))]
        selected = select_etf_research_candidates(rows, 12, held_symbols={"511360"})
        self.assertTrue({f"15990{i}" for i in range(6)}.issubset({row["symbol"] for row in selected}))
        self.assertNotIn("513520", {row["symbol"] for row in selected})
        self.assertIn("511360", {row["symbol"] for row in selected})

    def test_sp500_research_slot_is_not_taken_by_other_sp_sector_indices(self):
        rows = [{"symbol": code, "name": name, "eligible": True, "score": score}
                for code, name, score in (("513350", "标普油气ETF", 100),
                                          ("513500", "标普500ETF", 20),
                                          ("513100", "纳指ETF", 10))]
        self.assertEqual(["513500", "513100"],
                         [row["symbol"] for row in select_etf_research_candidates(rows, 2)])
        rows[1]["name"] = "S&P 500 ETF"
        self.assertEqual("513500", select_etf_research_candidates(rows, 2)[0]["symbol"])

    def test_global_configuration_counts_equity_regions_not_tickers_or_gold(self):
        rows = {"policy": {"global_diversification_required": True}, "candidates": [
            {"symbol": "510300", "asset_group": "a_share_equity"},
            {"symbol": "510500", "asset_group": "a_share_equity"},
            {"symbol": "513100", "name": "纳指ETF"}, {"symbol": "513500", "name": "标普500ETF"},
            {"symbol": "518880", "asset_group": "gold"}]}
        self.assertEqual("global_portfolio_requires_two_verified_equity_regions",
                         etf_diversification_error(rows, {"510300": ".35", "510500": ".25", "518880": ".35"}))
        self.assertEqual("equity_region_weight_above_60pct",
                         etf_diversification_error(rows, {"513100": ".35", "513500": ".30", "510300": ".30"}))
        self.assertIsNone(etf_diversification_error(rows, {"510300": ".35", "513100": ".30", "518880": ".30"}))

    def test_scanner_reserves_us_before_display_truncation(self):
        data = snapshot()
        for i in range(21):
            symbol = f"51{i:04}"
            data["securities"].append(dict(data["securities"][0], symbol=symbol))
            data["quotes"][symbol] = dict(data["quotes"]["510300"])
            data["etf_facts"][symbol] = dict(data["etf_facts"]["510300"], index_id=f"测试A股指数{i}")
        for symbol, name in (("513100", "纳斯达克100指数"), ("513500", "标普500指数")):
            data["securities"].append(dict(data["securities"][0], symbol=symbol, name=name))
            data["quotes"][symbol] = dict(data["quotes"]["510300"])
            data["etf_facts"][symbol] = dict(data["etf_facts"]["510300"], index_id=name,
                                            asset_group="overseas_equity", aum_cny="2000000000")
        result = build_etf_candidates(data, limit=12)
        self.assertTrue(result["policy"]["global_diversification_required"])
        selected = {row["symbol"]: row for row in result["candidates"]}
        for code in ("513100", "513500"):
            self.assertTrue(selected[code]["eligible"])
            self.assertEqual("us", selected[code]["market_region"])

    def test_all_six_roles_receive_account_mandate(self):
        agents = module_agents("etf")
        self.assertEqual(6, len(agents))
        for agent in agents:
            self.assertIn("禁止新增全部债券类ETF", agent.mission)
            self.assertIn("货币/现金类ETF也不配置", agent.mission)

    def test_government_bond_excluded_by_underlying_and_full_name(self):
        for term in ("国债", "地方政府债", "地方债", "政金债", "政策性金融债", "国开债", "农发债", "口行债"):
            for source in ("index", "source_fields", "name"):
                with self.subTest(term=term, source=source):
                    data = snapshot()
                    fact = data["etf_facts"]["510300"]
                    if source == "index":
                        fact["index_id"] = term + "指数"
                    elif source == "name":
                        data["securities"][0]["name"] = term + "ETF"
                    else:
                        fact["source_fields"] = {"基金全称": term + "交易型开放式基金"}
                    row = build_etf_candidates(data)["candidates"][0]
                    self.assertFalse(row["eligible"])
                    self.assertTrue(row["buy_excluded"])
        self.assertTrue(government_bond_buy_excluded({"name": "货币ETF", "index_id": "货币市场"}))
        self.assertTrue(government_bond_buy_excluded({"name": "短融ETF", "index_id": "中证短期融资券指数"}))

    def test_excluded_holding_retained_when_outside_shortlist(self):
        data = diversified_snapshot()
        data["etf_facts"]["513330"]["index_id"] = "国债政金债指数"
        data["account"] = {"positions": [{"symbol": "513330", "quantity": 100}]}
        rows = build_etf_candidates(data, limit=1)["candidates"]
        self.assertIn("513330", [row["symbol"] for row in rows])

    def test_low_early_turnover_remains_conditional_research(self):
        data = snapshot(); data["market"]["session"] = "intraday"
        data["quotes"]["510300"]["turnover"] = "1000"
        row = build_etf_candidates(data)["candidates"][0]
        self.assertTrue(row["eligible"])
        self.assertIn("verify_live_depth_and_completed_session_liquidity_before_execution", row["execution_conditions"])
        self.assertIsNone(row["metrics"]["liquidity_turnover_cny"])
        data["market"]["session"] = "post_market"
        self.assertFalse(build_etf_candidates(data)["candidates"][0]["eligible"])

    def test_early_turnover_uses_completed_history_amount_when_known(self):
        class Store:
            def etf_attribute_map(self, known_at):
                return {}
            def bars_as_known(self, symbol, limit, end_date, known_at):
                self.query = symbol, limit, end_date.isoformat(), known_at
                return [{"trade_date": day, "amount": "20000000"} for day in
                        ("2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08")]
        data = snapshot(); data["market"]["session"] = "intraday"
        data["market"]["recent_completed_trade_dates"] = ["2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08"]
        data["quotes"]["510300"]["turnover"] = "1000"
        store = Store(); row = build_etf_candidates(data, store)["candidates"][0]
        self.assertTrue(row["eligible"])
        self.assertEqual("20000000", row["metrics"]["liquidity_turnover_cny"])
        self.assertEqual("five_completed_sessions_mean_amount", row["metrics"]["liquidity_basis"])
        self.assertEqual(("510300", 5, "2026-09-08", NOW), store.query)
        data["market"]["recent_completed_trade_dates"][0] = "2026-09-01"
        incomplete = build_etf_candidates(data, store)["candidates"][0]
        self.assertIsNone(incomplete["metrics"]["liquidity_turnover_cny"])

    def test_reported_aum_and_turnover_are_distinct(self):
        row = build_etf_candidates(snapshot())["candidates"][0]
        self.assertTrue(row["eligible"])
        self.assertEqual("94872000000", row["metrics"]["aum_cny"])
        self.assertEqual("1000000000", row["metrics"]["turnover_cny"])

    def test_missing_aum_never_uses_market_cap_or_turnover(self):
        data = snapshot()
        del data["etf_facts"]["510300"]["aum_cny"]
        data["quotes"]["510300"]["total_market_cap"] = "90000000000"
        row = build_etf_candidates(data)["candidates"][0]
        self.assertFalse(row["eligible"])
        self.assertIsNone(row["metrics"]["aum_cny"])

    def test_aum_old_future_and_unknown_basis_rejected(self):
        for key, value in (("aum_as_of", "2025-12-31"), ("aum_as_of", "2026-12-31"),
                           ("aum_available_at", "2026-09-10T10:00:00+08:00"),
                           ("aum_available_at", "2026-06-20T10:00:00+08:00"), ("aum_basis", "spot_market_cap")):
            with self.subTest(key=key, value=value):
                data = snapshot(); data["etf_facts"]["510300"][key] = value
                self.assertFalse(build_etf_candidates(data)["candidates"][0]["eligible"])

    def test_only_shenzhen_shanghai_etfs(self):
        for security_type, exchange, symbol in (("stock", "SSE", "600001"), ("etf", "HKEX", "02800"), ("etf", "SZSE", "510300")):
            data = snapshot(); data["securities"][0].update(security_type=security_type, exchange=exchange, symbol=symbol)
            self.assertEqual([], build_etf_candidates(data)["candidates"])

    def test_same_index_keeps_larger_liquid_fund(self):
        data = snapshot()
        second = deepcopy(data["securities"][0]); second["symbol"] = "510310"; data["securities"].append(second)
        data["quotes"]["510310"] = dict(data["quotes"]["510300"])
        data["etf_facts"]["510310"] = dict(data["etf_facts"]["510300"], aum_cny="2000000000")
        result = build_etf_candidates(data)
        self.assertEqual(1, result["eligible_count"])
        self.assertEqual("510300", result["candidates"][0]["symbol"])
        self.assertIn("duplicate_tracking_index", result["candidates"][1]["reasons"])

    def test_t0_never_inferred_from_name(self):
        data = snapshot(); data["securities"][0].update(name="黄金跨境货币ETF", same_day_turnaround=None)
        row = build_etf_candidates(data)["candidates"][0]
        self.assertFalse(row["eligible"])
        self.assertIsNone(row["same_day_turnaround"])

    def test_cross_border_requires_current_premium_and_calendar(self):
        data = snapshot(); fact = data["etf_facts"]["510300"]
        fact["asset_group"] = "overseas_equity"
        row = build_etf_candidates(data)["candidates"][0]
        self.assertTrue(row["eligible"])
        self.assertIn("verify_current_premium_before_execution", row["execution_conditions"])
        fact.update(premium_rate="0.005", premium_available_at=NOW, premium_source_url="https://example.org/nav",
                    underlying_market_open=True, foreign_calendar_source_url="https://example.org/calendar")
        self.assertTrue(build_etf_candidates(data)["candidates"][0]["eligible"])
        fact["premium_rate"] = "0.02"
        self.assertFalse(build_etf_candidates(data)["candidates"][0]["eligible"])

    def test_quote_timestamp_stale_or_future(self):
        for value in (None, "2026-09-07T10:00:00+08:00", "2026-09-09T10:01:00+08:00"):
            data = snapshot(); data["quotes"]["510300"]["available_at"] = value
            self.assertFalse(build_etf_candidates(data)["candidates"][0]["eligible"])


class EtfAllocationTests(unittest.TestCase):
    def test_conditional_budget_uses_limit_for_lots_and_investment_ratio(self):
        data = diversified_snapshot()
        data["securities"].append(dict(data["securities"][0], symbol="513500"))
        data["quotes"]["513500"] = dict(data["quotes"]["510300"], last="2.688")
        data["etf_facts"]["513500"] = dict(data["etf_facts"]["510300"],
            asset_group="us_equity", index_id="标普500指数")
        rows = build_etf_candidates(data)
        limits = {"510300": "4.432", "513330": "1.455", "513500": "2.459", "518880": "8.633"}
        weights = {"510300": ".336832", "513330": ".199335", "513500": ".31967", "518880": ".060431"}
        for row in rows["candidates"]:
            row["buy_limit_price"] = limits[row["symbol"]]
            if row["symbol"] != "513500":
                row["price"] = limits[row["symbol"]]
        before = deepcopy(rows)
        plan = build_etf_allocation(rows, account(), weights)
        self.assertEqual("planned", plan["status"])
        self.assertEqual({"510300": 7600, "513330": 13700, "513500": 13000, "518880": 700},
                         {p["symbol"]: p["target_quantity"] for p in plan["positions"]})
        self.assertEqual(Decimal("91626.8"), Decimal(plan["invested_cny"]))
        self.assertEqual(Decimal(".916268"), Decimal(plan["investment_ratio"]))
        self.assertEqual(Decimal("91630.8"), Decimal(plan["required_buy_cash_cny"]))
        self.assertEqual(Decimal("8369.2"), Decimal(plan["cash_reserve_cny"]))
        self.assertEqual("conditional_buy_limits", plan["valuation_basis"])
        self.assertFalse(plan["execution_authorized"])
        self.assertTrue(all(p["delta_quantity"] is None for p in plan["positions"]))
        for position in plan["positions"]:
            self.assertEqual(limits[position["symbol"]], position["valuation_price"])
            self.assertEqual(Decimal(limits[position["symbol"]]) * position["target_quantity"],
                             Decimal(position["target_notional_cny"]))
        self.assertEqual(before, rows)

    def test_partial_account_confirmation_keeps_hypothetical_plan(self):
        rows = build_etf_candidates(diversified_snapshot())
        for acct in (
            dict(account(), funding_confirmed=True, positions_confirmed=False, cash_available_cny="50000"),
            dict(account(), funding_confirmed=True, positions_confirmed=True, cash_available_cny=None),
            dict(account(), positions=[{"symbol": "159999", "quantity": 9000, "available_quantity": 9000}]),
        ):
            with self.subTest(account=acct):
                plan = build_etf_allocation(rows, acct)
                self.assertEqual("planned", plan["status"])
                self.assertEqual("100000", plan["capital_cny"])
                self.assertEqual("hypothetical_zero_inventory_for_target_sizing", plan["inventory_basis"])
                self.assertEqual("0", plan["expected_sell_proceeds_cny"])
                self.assertNotIn("current_nav_cny", plan)
                self.assertFalse(plan["execution_authorized"])
                self.assertTrue(all(row["delta_quantity"] is None and row["side"] == "unknown"
                                    for row in plan["positions"]))
                self.assertIn("confirm_funding_cash_positions_and_recalculate_rebalance_before_execution",
                              plan["execution_conditions"])
        invalid = build_etf_allocation(rows, dict(account(), cash_available_cny="-1"))
        self.assertIn("invalid_negative_cash", invalid["reasons"])

    def test_high_unit_price_sizes_fewer_lots_without_rejecting_plan(self):
        data = diversified_snapshot()
        data["quotes"]["510300"]["last"] = "80"
        plan = build_etf_allocation(build_etf_candidates(data), account())
        self.assertEqual("planned", plan["status"])
        self.assertEqual(400, plan["positions"][0]["target_quantity"])
        self.assertGreaterEqual(Decimal(plan["investment_ratio"]), Decimal(".90"))
        self.assertTrue(all(row["target_quantity"] % 100 == 0 for row in plan["positions"]))

    def test_configured_exposure_applies_to_target_and_actual_lots(self):
        rows = build_etf_candidates(diversified_snapshot(), policy={"etf_min_weight": ".50", "etf_max_weight": ".80"})
        plan = build_etf_allocation(rows, account(), {"510300": ".30", "513330": ".30"})
        self.assertEqual("planned", plan["status"])
        self.assertEqual("0.6", plan["investment_ratio"])
        self.assertEqual("0.50", plan["exposure_policy"]["etf_min_weight"])
        high = build_etf_allocation(rows, account(), {"510300": ".35", "513330": ".30", "518880": ".30"})
        self.assertIn("investment_target_outside_configured_etf_range", high["reasons"])
        low = build_etf_allocation(rows, account(), {"510300": ".251", "513330": ".249"})
        self.assertIn("lot_or_fee_constraints_prevent_valid_exposure", low["reasons"])
        default = build_etf_allocation(build_etf_candidates(diversified_snapshot()), account(),
                                       {"510300": ".30", "513330": ".30"})
        self.assertIn("investment_target_must_be_90_to_98pct", default["reasons"])

    def test_invalid_exposure_policy_fails_closed(self):
        for low, high in (("nan", ".98"), (".99", ".98"), (".5", "1.1"), ("-.1", ".8")):
            with self.subTest(low=low, high=high):
                with self.assertRaisesRegex(ValueError, "invalid_etf_exposure_policy"):
                    build_etf_candidates(snapshot(), policy={"etf_min_weight": low, "etf_max_weight": high})

    def test_configured_reference_is_soft_and_default_respects_maximum(self):
        policy = {"etf_min_weight": ".50", "etf_max_weight": ".80", "etf_target_weight": ".70"}
        rows = build_etf_candidates(diversified_snapshot(), policy=policy)
        self.assertEqual("0.70", build_etf_allocation(rows, account())["target_investment_ratio"])
        self.assertEqual("planned", build_etf_allocation(rows, account(), {"510300": ".30", "513330": ".30"})["status"])
        del policy["etf_target_weight"]
        rows = build_etf_candidates(diversified_snapshot(), policy=policy)
        self.assertEqual("0.80", build_etf_allocation(rows, account())["target_investment_ratio"])

    def test_exclusion_cannot_be_bypassed_with_eligible_flag_or_t(self):
        rows = build_etf_candidates(diversified_snapshot())
        row = next(row for row in rows["candidates"] if row["symbol"] == "513330")
        row.update(name="国债ETF", eligible=True)
        self.assertEqual("no_valid_plan", build_etf_allocation(rows, account(),
                         {"510300": ".35", "513330": ".30", "518880": ".30"})["status"])
        self.assertEqual([], build_t_plan(row, {"symbol": "513330"},
                                         dict(account(), funding_confirmed=True, positions_confirmed=True))["legs"])

    def test_excluded_existing_holding_can_reduce_but_not_increase(self):
        data = diversified_snapshot()
        data["etf_facts"]["513330"]["index_id"] = "地方政府债指数"
        acct = dict(account(), funding_confirmed=True, positions_confirmed=True, cash_available_cny="68000",
                    positions=[{"symbol": "513330", "quantity": 8000, "available_quantity": 8000}])
        rows = build_etf_candidates(data)
        rows["policy"]["global_diversification_required"] = False
        plan = build_etf_allocation(rows, acct, {"510300": ".35", "513330": ".30", "518880": ".30"})
        self.assertEqual("planned", plan["status"])
        self.assertEqual("100000", plan["current_nav_cny"])
        self.assertLess(next(row for row in plan["positions"] if row["symbol"] == "513330")["delta_quantity"], 0)
        plan = build_etf_allocation(rows, acct, {"510300": ".30", "513330": ".35", "518880": ".30"})
        self.assertIn("bond_or_money_buy_excluded_by_user", plan["reasons"])

    def test_verified_broad_and_sector_risk_groups_are_not_one_cap(self):
        data = diversified_snapshot()
        for symbol, fact in data["etf_facts"].items():
            fact["asset_group"] = "a_share_equity"
            fact["concentration_group"] = "a_share_broad_equity" if symbol == "510300" else "a_share_sector_equity"
        plan = build_etf_allocation(build_etf_candidates(data), account(),
                                    {"510300": ".35", "513330": ".30", "518880": ".30"})
        self.assertEqual("planned", plan["status"])

    def test_planned_funding_remains_planned_and_95pct_lot_target(self):
        acct = account(); before = deepcopy(acct)
        plan = build_etf_allocation(build_etf_candidates(diversified_snapshot()), acct)
        self.assertEqual("planned", plan["status"])
        self.assertFalse(plan["funding_confirmed"])
        self.assertFalse(plan["execution_authorized"])
        self.assertEqual("0.95", plan["target_investment_ratio"])
        self.assertEqual(8700, plan["positions"][0]["target_quantity"])
        self.assertGreaterEqual(Decimal(plan["investment_ratio"]), Decimal(".90"))
        self.assertLessEqual(Decimal(plan["invested_cny"]) + Decimal(plan["estimated_fees_cny"]), Decimal("100000"))
        self.assertEqual(before, acct)

    def test_cash_is_not_valid_default_plan(self):
        self.assertEqual("no_valid_plan", build_etf_allocation({"candidates": []}, account())["status"])
        for weight in ("0", ".89", ".99", "nan", "-1"):
            self.assertEqual("no_valid_plan", build_etf_allocation(build_etf_candidates(snapshot()), account(), {"510300": weight})["status"])

    def test_weak_regime_does_not_force_cash_like_allocation(self):
        acct = account(); acct["risk_regime"] = "weak"
        self.assertEqual("no_valid_plan", build_etf_allocation(build_etf_candidates(snapshot()), acct)["status"])
        data = diversified_snapshot()
        self.assertEqual("planned", build_etf_allocation(build_etf_candidates(data), acct)["status"])

    def test_confirmed_cash_and_fees_constrain_planning(self):
        acct = account(); acct.update(funding_confirmed=True, positions_confirmed=True,
                                      cash_available_cny="100000", commission_fixed_per_order="2000")
        self.assertEqual("no_valid_plan", build_etf_allocation(build_etf_candidates(diversified_snapshot()), acct)["status"])
        acct["commission_fixed_per_order"] = "1"
        self.assertEqual("planned", build_etf_allocation(build_etf_candidates(diversified_snapshot()), acct)["status"])

    def test_confirmed_capital_uses_nav_not_original_planned_budget(self):
        acct = dict(account(), funding_confirmed=True, positions_confirmed=True, cash_available_cny="60000",
                    positions=[{"symbol": "510300", "quantity": 5000, "available_quantity": 5000}])
        plan = build_etf_allocation(build_etf_candidates(diversified_snapshot()), acct)
        self.assertEqual("planned", plan["status"])
        self.assertEqual("80000", plan["current_nav_cny"])
        self.assertEqual("80000", plan["capital_cny"])
        self.assertEqual(7000, plan["positions"][0]["target_quantity"])

    def test_entry_max_does_not_inflate_nav_or_sell_price(self):
        rows = build_etf_candidates(diversified_snapshot())
        rows["candidates"][0]["buy_limit_price"] = "4.1"
        acct = dict(account(), funding_confirmed=True, positions_confirmed=True, cash_available_cny="52000",
                    positions=[{"symbol": "510300", "quantity": 12000, "available_quantity": 12000}])
        plan = build_etf_allocation(rows, acct)
        self.assertEqual("planned", plan["status"])
        self.assertEqual("100000", plan["current_nav_cny"])
        item = plan["positions"][0]
        self.assertEqual("market_quotes", plan["valuation_basis"])
        self.assertEqual("4", item["valuation_price"])
        self.assertEqual(Decimal(item["price"]) * item["target_quantity"], Decimal(item["target_notional_cny"]))
        self.assertEqual("sell", item["side"])
        self.assertEqual("4", item["limit_price"])
        self.assertEqual("13199", plan["expected_sell_proceeds_cny"])

    def test_buy_limit_sizes_buy_without_revaluing_holdings(self):
        rows = build_etf_candidates(diversified_snapshot())
        rows["candidates"][0]["buy_limit_price"] = "4.1"
        plan = build_etf_allocation(rows, account())
        self.assertEqual("planned", plan["status"])
        self.assertEqual(8500, plan["positions"][0]["target_quantity"])
        self.assertEqual("4.1", plan["positions"][0]["limit_price"])
        self.assertLessEqual(Decimal("4.1") * 8500, Decimal("35000"))

    def test_missing_old_holding_price_keeps_budget_research_without_nav_estimate(self):
        acct = dict(account(), funding_confirmed=True, positions_confirmed=True, cash_available_cny="60000",
                    positions=[{"symbol": "510500", "quantity": 5000, "available_quantity": 5000}])
        plan = build_etf_allocation(build_etf_candidates(diversified_snapshot()), acct)
        self.assertEqual("planned", plan["status"])
        self.assertIn("existing_holding_market_price_required_for_nav", plan["reasons"])
        self.assertEqual("100000", plan["capital_cny"])
        self.assertEqual("planned_funding", plan["capital_basis"])
        self.assertEqual(["510500"], plan["nav_unpriced_symbols"])
        self.assertNotIn("current_nav_cny", plan)
        self.assertEqual("0", plan["expected_sell_proceeds_cny"])
        self.assertTrue(all(row["delta_quantity"] is None for row in plan["positions"]))
        self.assertIn("refresh_existing_holding_prices_and_recalculate_nav_before_execution",
                      plan["execution_conditions"])

    def test_concentration_rejected(self):
        rows = build_etf_candidates(diversified_snapshot())
        self.assertEqual("no_valid_plan", build_etf_allocation(rows, account(), {"510300": ".95"})["status"])
        data = diversified_snapshot()
        for fact in data["etf_facts"].values():
            fact["asset_group"] = "a_share_equity"
        self.assertEqual("no_valid_plan", build_etf_allocation(build_etf_candidates(data), account(),
                         {"510300": ".35", "513330": ".30", "518880": ".30"})["status"])

    def test_rebalance_old_holding_has_sell_delta_and_contingent_cash(self):
        data = diversified_snapshot()
        data["securities"].append(dict(data["securities"][0], symbol="510500"))
        data["quotes"]["510500"] = dict(data["quotes"]["510300"], last="1")
        acct = dict(account(), funding_confirmed=True, positions_confirmed=True, cash_available_cny="90000",
                    positions=[{"symbol": "510500", "quantity": 10000, "available_quantity": 10000}])
        data["account"] = acct
        plan = build_etf_allocation(build_etf_candidates(data), acct)
        self.assertEqual("planned", plan["status"])
        self.assertTrue(plan["cash_contingent_on_sales"])
        self.assertEqual(-10000, next(row for row in plan["positions"] if row["symbol"] == "510500")["delta_quantity"])
        acct["positions"][0]["available_quantity"] = 0
        self.assertEqual("no_valid_plan", build_etf_allocation(build_etf_candidates(data), acct)["status"])
        del data["quotes"]["510500"]
        unpriced = build_etf_allocation(build_etf_candidates(data), acct)
        self.assertEqual("planned", unpriced["status"])
        self.assertEqual("0", unpriced["expected_sell_proceeds_cny"])
        self.assertTrue(all(row["delta_quantity"] is None for row in unpriced["positions"]))

    def test_expensive_lot_cannot_silently_break_90pct_floor(self):
        data = diversified_snapshot()
        for quote in data["quotes"].values():
            quote["last"] = "1000"
        plan = build_etf_allocation(build_etf_candidates(data), account())
        self.assertEqual("no_valid_plan", plan["status"])
        self.assertEqual([], plan["positions"])
        self.assertIn("lot_or_fee_constraints_prevent_valid_exposure", plan["reasons"])

    def test_second_equity_region_must_survive_real_lot_sizing(self):
        data = diversified_snapshot()
        data["securities"].append(dict(data["securities"][0], symbol="510500"))
        data["quotes"]["510500"] = dict(data["quotes"]["510300"])
        data["etf_facts"]["510500"] = dict(data["etf_facts"]["510300"], index_id="中证500指数")
        rows = build_etf_candidates(data)
        weights = {"510300": ".35", "510500": ".25", "518880": ".35", "513330": ".001"}
        # The nominal HK allocation is 100 yuan, below one 400-yuan trading lot;
        # the other holdings still exceed the 90% exposure floor.
        plan = build_etf_allocation(rows, account(), weights)
        self.assertEqual("no_valid_plan", plan["status"])
        self.assertIn("global_portfolio_requires_two_verified_equity_regions", plan["reasons"])
        self.assertEqual([], plan["positions"])
        weights.update({"510500": ".241", "513330": ".01"})
        plan = build_etf_allocation(rows, account(), weights)
        self.assertEqual("planned", plan["status"])
        self.assertEqual(200, next(row for row in plan["positions"] if row["symbol"] == "513330")["target_quantity"])


class EtfTTests(unittest.TestCase):
    def setUp(self):
        self.row = build_etf_candidates(snapshot())["candidates"][0]
        self.account = dict(account(), funding_confirmed=True, positions_confirmed=True, cash_available_cny="10000")
        self.position = {"symbol": "510300", "quantity": 1000, "available_quantity": 1000}

    def plan(self, **kwargs):
        return build_t_plan(self.row, self.position, self.account, buy_price="4", sell_price="4.1", **kwargs)

    def test_t1_sell_then_buy_uses_only_existing_noncore(self):
        plan = self.plan()
        self.assertEqual("conditional_ready", plan["status"])
        self.assertEqual(800, plan["core_quantity"])
        self.assertEqual(200, plan["quantity"])
        self.assertEqual("sell", plan["legs"][0]["side"])
        self.assertFalse(plan["execution_authorized"])

    def test_t1_buy_then_sell_cannot_sell_new_locked_shares(self):
        self.position["available_quantity"] = 0
        self.assertEqual([], self.plan(direction="buy_then_sell")["legs"])
        self.row["same_day_turnaround"] = True
        self.assertEqual("conditional_ready", self.plan(direction="buy_then_sell")["status"])

    def test_t1_buy_then_sell_with_existing_available_inventory(self):
        plan = self.plan(direction="buy_then_sell")
        self.assertEqual("confirmed_existing_inventory", plan["legs"][1]["inventory_basis"])

    def test_no_confirmed_position_has_no_fictional_legs(self):
        self.account["positions_confirmed"] = False
        self.assertEqual([], self.plan()["legs"])

    def test_core_inventory_cash_costs_and_round_lots(self):
        self.assertEqual([], self.plan(quantity=300)["legs"])
        self.assertEqual([], self.plan(quantity=150)["legs"])
        self.account["cash_available_cny"] = "800"
        self.assertEqual([], self.plan(direction="buy_then_sell")["legs"])
        self.account["commission_fixed_per_order"] = "20"
        self.assertEqual([], self.plan()["legs"])

    def test_unknown_cost_is_not_zero_cost(self):
        del self.account["commission_fixed_per_order"]
        self.assertEqual([], self.plan()["legs"])


class EtfFactsTests(unittest.TestCase):
    def test_fact_shortlist_preserves_lower_turnover_sector_targets(self):
        data = snapshot()
        for i, name in enumerate(("芯片ETF", "医药ETF", "银行ETF")):
            code = f"15990{i}"
            data["securities"].append(dict(data["securities"][0], symbol=code, name=name, exchange="SZSE"))
        for i in range(10):
            code = f"51500{i}"
            data["securities"].append(dict(data["securities"][0], symbol=code, name="日经ETF"))
            data["quotes"][code] = {"turnover": "900000000"}
        selected = select_etf_fact_symbols(data, limit=4)
        self.assertTrue({"159900", "159901", "159902", "510300"}.issubset(selected))
        self.assertLessEqual(len(selected), 5)

    def test_actual_fund_fields_override_generic_qdii_country_guess(self):
        for name, index, group, region in (
            ("港股创新药交易型开放式基金", "国证港股通创新药指数", "hong_kong_equity", "hong_kong"),
            ("纳斯达克交易型开放式基金", "纳斯达克100指数", "overseas_equity", "us"),
            ("日经交易型开放式基金", "日经225指数", "overseas_equity", "japan"),
            ("德国交易型开放式基金", "德国DAX指数", "overseas_equity", "europe"),
            ("印度交易型开放式基金", "印度Nifty50指数", "overseas_equity", "india"),
            ("全球交易型开放式基金", "全球指数", "overseas_equity", "mixed"),
            ("测试交易型开放式基金", "未知指数", "overseas_equity", "unknown")):
            page = f'<tr><th>基金全称</th><td>{name}</td></tr><tr><th>基金代码</th><td>513000</td></tr><tr><th>基金类型</th><td>QDII-股票</td></tr><tr><th>跟踪标的</th><td>{index}</td></tr>'
            fact = parse_etf_facts("513000", page, NOW)
            self.assertEqual(group, fact["asset_group"])
            self.assertEqual(region, fact["market_region"])

    def test_other_country_fact_targets_optional_and_only_observed_listed_names(self):
        data = snapshot()
        for symbol, name, exchange in (("513520", "日经ETF", "SSE"), ("513030", "德国ETF", "SSE"),
                                       ("159999", "印度ETF测试样本", "SZSE"), ("02800", "日本测试海外挂牌", "HKEX"),
                                       ("511360", "短融ETF", "SSE")):
            data["securities"].append(dict(data["securities"][0], symbol=symbol, name=name, exchange=exchange))
        selected = select_etf_fact_symbols(data, limit=1)
        self.assertFalse({"513520", "513030", "159999"}.issubset(selected))
        self.assertTrue({"513520", "513030", "159999"}.issubset(select_etf_fact_symbols(data, limit=10)))
        self.assertNotIn("02800", selected)
        self.assertNotIn("511360", selected)
        data["account"] = {"positions": [{"symbol": "511360", "quantity": 100}]}
        self.assertIn("511360", select_etf_fact_symbols(data, limit=1))

    def test_24h_cache_reuse_keeps_actual_observation_times(self):
        now = datetime.now().astimezone().isoformat()
        fact = dict(snapshot()["etf_facts"]["510300"], metadata_available_at=now, aum_available_at=now)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts-cache.json"
            path.write_text(json.dumps({"510300": fact}), encoding="utf-8")
            with patch("requests.get") as get:
                result = fetch_etf_facts(["510300"], cache_path=path)
            get.assert_not_called()
            self.assertTrue(result["510300"]["cache_reused"])
            self.assertEqual(now, result["510300"]["metadata_available_at"])
            self.assertEqual(now, result["510300"]["aum_available_at"])

    def test_failed_refresh_does_not_overwrite_positive_expired_cache(self):
        import requests
        old_time = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
        fact = dict(snapshot()["etf_facts"]["510300"], metadata_available_at=old_time, aum_available_at=old_time)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "facts-cache.json"
            path.write_text(json.dumps({"510300": fact}), encoding="utf-8")
            with patch("requests.get", side_effect=requests.Timeout("timeout")):
                result = fetch_etf_facts(["510300"], cache_path=path)
            self.assertIn("Timeout", result["510300"]["evidence_gaps"][0])
            self.assertEqual(fact, json.loads(path.read_text(encoding="utf-8"))["510300"])

    def test_rate_cap_stops_remaining_requests_instead_of_retrying(self):
        import requests
        response = Mock(status_code=514)
        response.raise_for_status.side_effect = requests.HTTPError("514 Frequency Capped")
        with patch("requests.get", return_value=response) as get:
            facts = fetch_etf_facts(["510300", "511880"], workers=1, request_interval_seconds=0)
        self.assertEqual(1, get.call_count)
        self.assertIn("HTTPError", facts["510300"]["evidence_gaps"][0])
        self.assertEqual(["fund_facts_rate_limited_not_requested"], facts["511880"]["evidence_gaps"])

    def test_reported_fixed_income_type_classifies_short_financing_etf(self):
        page = '''<tr><th>基金全称</th><td>海富通中证短融交易型开放式指数证券投资基金</td></tr>
        <tr><th>基金代码</th><td>511360（主代码）<th>基金类型</th><td>指数型-固收</td></tr>
        <tr><th>净资产规模</th><td>806.53亿元（截止至：2026年06月30日）<th>跟踪标的</th><td>中证短融指数</td></tr>'''
        fact = parse_etf_facts("511360", page, NOW)
        self.assertEqual("bond", fact["asset_group"])
        self.assertEqual("中证短融指数", fact["index_id"])

    def test_live_html_missing_td_closes_and_money_etf(self):
        page = '''<tr><th>基金全称</th><td>银华交易型货币市场基金</td><th>基金简称</th><td>银华日利ETF</td></tr>
        <tr><th>基金代码</th><td>511880（主代码）<th>基金类型</th><td>货币型-普通货币</td></tr>
        <tr><th>净资产规模</th><td>795.03亿元（截止至：2026年06月30日）<th>份额规模</th><td>7.9054亿份</td></tr>'''
        fact = parse_etf_facts("511880", page, NOW)
        self.assertEqual("79503000000.00", fact["aum_cny"])
        self.assertTrue(fact["is_etf"])
        self.assertEqual("money", fact["asset_group"])

    def test_actual_source_schema_scale_date_and_index(self):
        page = """<table><tr><th>基金全称</th><td>华泰柏瑞沪深300交易型开放式指数证券投资基金</td></tr>
        <tr><th>基金代码</th><td>510300（主代码）</td><th>基金类型</th><td>指数型-股票</td></tr>
        <tr><th>净资产规模</th><td>948.72亿元（截止至：2026年06月30日）</td></tr>
        <tr><th>跟踪标的</th><td>沪深300指数</td></tr></table>"""
        fact = parse_etf_facts("510300", page, NOW)
        self.assertEqual("94872000000.00", fact["aum_cny"])
        self.assertEqual("2026-06-30", fact["aum_as_of"])
        self.assertEqual("沪深300指数", fact["index_id"])
        self.assertTrue(fact["is_etf"])
        self.assertEqual("a_share_equity", fact["asset_group"])
        with self.assertRaises(ValueError):
            parse_etf_facts("510500", page, NOW)

    def test_liquidity_shortlist_excludes_unheld_cash_representatives(self):
        data = snapshot(); data["securities"].append(dict(data["securities"][0], symbol="511880", name="货币ETF"))
        self.assertEqual(["510300"], select_etf_fact_symbols(data, limit=1))

    def test_held_etf_is_verified_even_below_liquidity_shortlist(self):
        data = snapshot(); data["securities"].append(dict(data["securities"][0], symbol="562000"))
        data["account"] = {"positions": [{"symbol": "562000", "quantity": 100}]}
        self.assertIn("562000", select_etf_fact_symbols(data, limit=1))
        self.assertIn("562000", [row["symbol"] for row in build_etf_candidates(data, limit=1)["candidates"]])


if __name__ == "__main__":
    unittest.main()
