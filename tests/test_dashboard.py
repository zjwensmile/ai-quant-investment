import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_quant.config import Settings
from ai_quant.dashboard import (
    INDEX_HTML,
    _agent_work_phase,
    _candidate_funnel,
    _codex_failure_message,
    _compact_chat_snapshot,
    _latest_agent_summaries,
    _latest_snapshot,
    _portfolio_summary,
    _task_progress,
)


class DashboardHelpersTests(unittest.TestCase):
    def test_approved_entry_prices_override_different_monitoring_prices(self):
        approved = {"symbol": "600000", "action": "buy", "target_weight": 0.1,
                    "take_profit_price": 12, "stop_loss_price": 9,
                    "risk_price_rationale": "批准开仓纪律"}
        portfolio = {"account_version": "v1", "target_positions": [approved]}
        risk = {"decision": "approve", "as_of_time": "2026-09-06T10:00:00+08:00",
                "approved_positions": [approved], "monitored_positions": [{
                    "symbol": "600000", "take_profit_price": 13, "stop_loss_price": 8,
                    "risk_price_rationale": "观察用阈值",
                }]}

        class Store:
            def list_runs(self): return [{"run_id": "mock", "status": "completed"}]
            def get_run(self, run_id):
                return {"executions": [{"agent_id": agent, "output_json": json.dumps(output)}
                                       for agent, output in (("portfolio_controller", portfolio), ("risk_controller", risk))]}
            def model_portfolio(self, run_id): return {"positions": [approved]}
            def get_order_plan(self, run_id): return {"status": "blocked"}

        with patch("ai_quant.dashboard._account", return_value={"version": "v1", "positions": []}), \
             patch("ai_quant.dashboard._latest_snapshot_path", return_value=None), \
             patch("pathlib.Path.exists", return_value=False):
            result = _portfolio_summary(SimpleNamespace(runtime_dir=Path("runtime")), Store())
        target = result["decision"]["new_buy_targets"][0]
        self.assertEqual((12, 9), (target["take_profit_price"], target["stop_loss_price"]))
        self.assertEqual("批准开仓纪律", target["risk_price_rationale"])
        self.assertEqual("approved_positions", target["risk_price_source"])
        self.assertEqual(risk["as_of_time"], target["risk_analysis_as_of"])
        self.assertIn("本轮止盈 / 止损", INDEX_HTML)
        self.assertIn("riskPrices(row)", INDEX_HTML)
        self.assertIn("待风控：本轮尚未提供完整止盈 / 止损", INDEX_HTML)

    def test_raw_snapshot_task_has_a_non_prediction_phase(self):
        class Store:
            def list_system_tasks(self, limit=20):
                return [{"task_id": "task-raw", "action": "snapshot_raw", "status": "running"}]

        progress = _task_progress(Store())
        self.assertFalse(progress["is_prediction"])
        self.assertIn("原始行情", progress["phase"])

    def test_snapshot_task_reads_coarse_stage_progress(self):
        class Store:
            def list_system_tasks(self, limit=20):
                return [{"task_id": "task-market", "action": "snapshot", "status": "running"}]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(root, root / "runtime", root / "runtime" / "audit.sqlite3")
            path = settings.runtime_dir / "task_progress" / "task-market.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "phase": "正在抓取全市场证券主数据与实时行情",
                "current_stage": 1,
                "total_stages": 5,
            }, ensure_ascii=False), encoding="utf-8")
            progress = _task_progress(Store(), settings=settings)

        self.assertEqual(1, progress["stage_progress"]["current_stage"])
        self.assertEqual(5, progress["stage_progress"]["total_stages"])

    def test_chanlun_selector_is_shown_in_discovery_phase(self):
        self.assertEqual(
            "① 概念与候选发现",
            _agent_work_phase("chanlun_candidate_selector", "discovery"),
        )

    def test_chat_snapshot_is_bounded_and_excludes_full_security_payload(self):
        rows = [{
            "symbol": f"{index:06d}", "name": "测试", "held": False,
            "program_reason": "候选", "security": {"large": "payload"},
            "features": {"return_1d": 0.01, "return_20d": 0.02},
        } for index in range(20)]
        compact = _compact_chat_snapshot({
            "snapshot_id": "snapshot-1", "as_of_time": "2026-08-24T09:00:00+08:00",
            "market": {}, "data_quality": {"status": "research_only", "warnings": []},
            "feature_coverage": {"coverage_ratio": 0.8},
            "candidates": {"TACTICAL": rows},
        })
        self.assertEqual(10, len(compact["candidates"]["TACTICAL"]))
        self.assertNotIn("security", compact["candidates"]["TACTICAL"][0])

    def test_chat_error_hides_echoed_context(self):
        detail = _codex_failure_message(
            "private context\nERROR: Not logged in", ""
        )
        self.assertEqual("Codex CLI 尚未通过 ChatGPT 会员登录", detail)
        self.assertNotIn("private context", detail)

    def test_dashboard_translates_acceptance_blockers_for_user(self):
        self.assertIn("确认券商可用现金", INDEX_HTML)
        self.assertIn("确认每日调度时刻", INDEX_HTML)
        self.assertIn("手工实盘试运行", INDEX_HTML)

    def test_dashboard_separates_target_pool_from_program_candidates(self):
        self.assertIn("当前持仓", INDEX_HTML)
        self.assertIn("目标候选池", INDEX_HTML)
        self.assertIn("全量日线缠论候选", INDEX_HTML)
        self.assertNotIn("A/B/C 原始程序候选", INDEX_HTML)
        self.assertIn("全量二买", INDEX_HTML)
        self.assertIn("全量三买", INDEX_HTML)
        self.assertIn("信号 ID / 类型", INDEX_HTML)
        self.assertIn("锚定 / 确认日期", INDEX_HTML)
        self.assertIn("Agent 研究", INDEX_HTML)
        self.assertIn("Agent 已选", INDEX_HTML)
        self.assertIn("/api/portfolio/latest", INDEX_HTML)
        self.assertIn("刷新时价", INDEX_HTML)
        self.assertIn("建议执行价位", INDEX_HTML)
        self.assertIn("一键完整流程", INDEX_HTML)
        self.assertIn("仅运行多 Agent 预测", INDEX_HTML)
        self.assertIn("Sol 解释 Agent", INDEX_HTML)
        self.assertIn("Agent 止盈 / 止损", INDEX_HTML)
        self.assertNotIn("<th>有效至</th>", INDEX_HTML)
        self.assertIn("全部 A 股涨跌分布", INDEX_HTML)
        self.assertIn("最近一次多 Agent 研究摘要", INDEX_HTML)

    def test_candidate_ui_has_one_renderer_and_matches_full_signal_identity(self):
        self.assertEqual(1, INDEX_HTML.count("function renderCandidates()"))
        self.assertNotIn("renderCandidates=function", INDEX_HTML)
        self.assertIn("selected_chanlun_identities", INDEX_HTML)
        self.assertIn("已选入（精确匹配）", INDEX_HTML)
        self.assertIn("旧快照无信号 ID，无法核验", INDEX_HTML)
        self.assertIn("d.snapshot_id===state.snapshot?.snapshot_id", INDEX_HTML)
        self.assertIn("当前快照尚未运行正式 Agent", INDEX_HTML)
        self.assertIn(".scroll table{min-width:720px}", INDEX_HTML)

    def test_dashboard_hides_removed_status_cards_and_shows_holding_totals(self):
        self.assertNotIn("<h2>投入上限</h2>", INDEX_HTML)
        self.assertNotIn("历史行情覆盖", INDEX_HTML)
        self.assertNotIn("行业分类覆盖", INDEX_HTML)
        self.assertNotIn("ETF 规则覆盖", INDEX_HTML)
        self.assertIn("持仓成本", INDEX_HTML)
        self.assertIn("最新市值", INDEX_HTML)

    def test_dashboard_explains_that_stale_targets_are_suppressed(self):
        self.assertIn("旧目标候选、止盈止损和分析理由均已隐藏", INDEX_HTML)
        self.assertIn("当前纯现金账户", INDEX_HTML)
        self.assertIn("待重新分析", INDEX_HTML)

    def test_agent_summary_ui_shows_all_agent_content_without_expansion(self):
        self.assertIn("agentSummarySignature", INDEX_HTML)
        self.assertIn('data-agent-id="${esc(a.agent_id)}"', INDEX_HTML)
        self.assertIn('<article class="agentSummary"', INDEX_HTML)
        self.assertNotIn('<details class="agentSummary"', INDEX_HTML)
        self.assertIn("本轮具体工作", INDEX_HTML)
        self.assertIn("负责什么", INDEX_HTML)
        self.assertNotIn("renderAgentSummaryMetrics", INDEX_HTML)

    def test_candidate_funnel_explains_where_new_ideas_stopped(self):
        result = _candidate_funnel(
            {"candidates": {
                "HOLDINGS": [{"symbol": "000001"}],
                "QUALITY_VALUE": [{"symbol": "600000", "entry_eligible": True}],
                "TACTICAL": [{"symbol": "600001", "entry_eligible": False}],
            }},
            {"research": {"decisions": [{
                "symbol": "600000", "strategy": "QUALITY_VALUE",
                "direction": "neutral",
            }]}},
            {"target_positions": []},
            {"approved_positions": []},
        )
        self.assertEqual(2, result["program_candidates"])
        self.assertEqual(1, result["entry_eligible"])
        self.assertEqual(1, result["research_decisions"])
        self.assertEqual(0, result["bullish_decisions"])

    def test_candidate_funnel_includes_monthly_concepts_and_breakdown(self):
        result = _candidate_funnel(
            {"monthly_concepts": {"concepts": [{"name": "科技半导体"}]},
             "candidates": {
                 "CHANLUN_SECOND_BUY": [{"symbol": "600000", "agent_concept": "科技半导体", "entry_eligible": True}],
                 "chanlun_shadow_candidates": [{"symbol": "600001"}],
             }, "chanlun_daily_candidates": {
                 "CHANLUN_SECOND_BUY": [{"symbol": "600000"}],
                 "CHANLUN_THIRD_BUY": [{"symbol": "600001"}, {"symbol": "600002"}],
             }},
            {"macro_market": {"decisions": [{
                "symbol": "600000", "strategy": "CHANLUN_SECOND_BUY",
                "direction": "bullish",
            }]}, "market_sentiment": {"decisions": [{
                "symbol": "600000", "strategy": "CHANLUN_SECOND_BUY",
                "direction": "bullish",
            }]}},
            {"target_positions": [{"symbol": "600000", "action": "buy"}]},
            {"approved_positions": [{"symbol": "600000", "action": "buy"}]},
        )
        self.assertEqual(1, result["monthly_concepts"])
        self.assertEqual(1, result["program_candidates"])
        self.assertEqual(3, result["chanlun_daily_candidates"])
        self.assertEqual(1, result["chanlun_second_buy_candidates"])
        self.assertEqual(2, result["chanlun_third_buy_candidates"])
        self.assertEqual(1, result["agent_selected_candidates"])
        self.assertEqual(1, result["completed_research"])
        self.assertEqual(1, result["first_wave_researched"])
        self.assertEqual(1, result["second_wave_researched"])
        self.assertEqual("科技半导体", result["concept_breakdown"][0]["name"])
        self.assertEqual(1, result["concept_breakdown"][0]["risk_approved"])

    def test_candidate_funnel_requires_exact_chanlun_signal_id(self):
        result = _candidate_funnel(
            {"candidates": {"CHANLUN_SECOND_BUY": [{
                "symbol": "600000", "signal_id": "signal-a",
            }]}},
            {"macro_market": {"decisions": [{
                "symbol": "600000", "strategy": "CHANLUN_SECOND_BUY",
                "signal_id": "signal-b", "direction": "bullish",
            }]}},
            {"target_positions": []}, {"approved_positions": []},
        )
        self.assertEqual(0, result["completed_research"])
        self.assertEqual(0, result["research_decisions"])

    def test_portfolio_summary_marks_old_account_decision_for_reanalysis(self):
        class Store:
            def list_runs(self):
                return [{"run_id": "run-1", "status": "completed",
                         "as_of_time": "2026-08-24T15:20:00+08:00"}]

            def get_run(self, run_id):
                return {"executions": [
                    {"agent_id": "portfolio_controller", "output_json": json.dumps({
                        "account_version": "old-account", "selected_scenario": "hold",
                        "target_positions": [{"symbol": "563020", "action": "hold",
                                              "target_weight": 0.1}],
                    })},
                    {"agent_id": "risk_controller", "output_json": json.dumps({
                        "decision": "defer", "rationale": "等待重算",
                        "approved_positions": [{
                            "symbol": "563020", "take_profit_price": 1.25,
                            "stop_loss_price": 1.12,
                            "risk_price_rationale": "按持仓成本与波动评估",
                        }],
                    })},
                ]}

            def model_portfolio(self, run_id):
                return {"cash_weight": 0.9, "positions": [
                    {"symbol": "563020", "action": "hold", "target_weight": 0.1}
                ]}

            def get_order_plan(self, run_id):
                return {"status": "no_action", "blockers": ["风控暂缓"]}

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"
            snapshots.mkdir()
            (snapshots / "market-1.json").write_text(json.dumps({
                "snapshot_id": "market-1", "as_of_time": "2026-08-25T10:00:00+08:00",
                "quotes": {"563020": {"last": 1.179, "available_at": "10:00"}},
            }), encoding="utf-8")
            run_dir = runtime / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            (run_dir / "snapshot.json").write_text(json.dumps({
                "snapshot_id": "run-market", "candidates": {},
                "chanlun_daily_candidates": {
                    "CHANLUN_SECOND_BUY": [{"symbol": "600000", "signal_id": "signal-a"}],
                    "CHANLUN_THIRD_BUY": [],
                },
            }), encoding="utf-8")
            (run_dir / "dashboard_snapshot.json").write_text(json.dumps({
                "snapshot_id": "run-market",
                "candidates": {"CHANLUN_SECOND_BUY": [{
                    "symbol": "600000", "signal_id": "signal-a",
                }]},
                "chanlun_daily_candidates": {
                    "CHANLUN_SECOND_BUY": [{"symbol": "600000", "signal_id": "signal-a"}],
                    "CHANLUN_THIRD_BUY": [],
                },
            }), encoding="utf-8")
            account = {
                "version": "new-account", "capital_budget": 50000, "cash": 50000,
                "positions": [{"symbol": "563020", "name": "红利低波ETF易方",
                               "quantity": 4200, "available_quantity": 4200,
                               "cost_price": 1.175}],
            }
            with patch("ai_quant.dashboard._account", return_value=account):
                summary = _portfolio_summary(
                    SimpleNamespace(runtime_dir=runtime), Store()
                )
        self.assertTrue(summary["decision"]["needs_reanalysis"])
        self.assertTrue(summary["decision"]["stale_result_suppressed"])
        self.assertEqual([], summary["decision"]["new_buy_targets"])
        self.assertEqual([], summary["decision"]["targets"])
        self.assertEqual([], summary["decision"]["opportunities"])
        self.assertIsNone(summary["decision"]["portfolio_rationale"])
        self.assertIsNone(summary["decision"]["risk_rationale"])
        self.assertEqual("pending_reanalysis", summary["decision"]["risk_decision"])
        self.assertEqual("563020", summary["decision"]["pending_holdings"][0]["symbol"])
        self.assertIsNone(summary["holdings"][0]["target"])
        self.assertAlmostEqual(16.8, summary["holdings"][0]["unrealized_pnl"])
        self.assertAlmostEqual(4935.0, summary["holding_totals"]["cost_basis"])
        self.assertAlmostEqual(4951.8, summary["holding_totals"]["market_value"])
        self.assertAlmostEqual(16.8, summary["holding_totals"]["unrealized_pnl"])
        self.assertAlmostEqual(0.099036, summary["holding_totals"]["budget_ratio"])
        self.assertEqual(
            1, summary["decision"]["candidate_funnel"]["agent_selected_candidates"]
        )

    def test_latest_agent_summaries_exposes_each_execution_work(self):
        class Store:
            def list_runs(self):
                return [{"run_id": "run-1", "status": "completed",
                         "as_of_time": "2026-08-25T15:00:00+08:00"}]

            def get_run(self, run_id):
                return {"executions": [{
                    "agent_id": "trend_swing", "model": "gpt-test",
                    "stage": "research", "status": "completed",
                    "trace_path": "trace.jsonl", "output_json": json.dumps({
                        "overall_summary": "趋势分化",
                        "decisions": [{
                            "subject": "浦发银行", "direction": "neutral",
                            "summary": "趋势证据不足",
                            "supporting_evidence": [{"source": "https://example.com"}],
                        }],
                        "rejected_candidates": [{"symbol": "600001"}],
                        "search_queries": ["浦发银行 公告"],
                    }),
                }]}

        result = _latest_agent_summaries(Store())
        self.assertTrue(result["available"])
        self.assertEqual("趋势分化", result["agents"][0]["conclusion"])
        self.assertTrue(result["agents"][0]["trace_available"])
        self.assertEqual("③ 第二波：交叉判断", result["agents"][0]["phase"])
        self.assertEqual(1, result["agents"][0]["metrics"]["decisions"])
        self.assertEqual(1, result["agents"][0]["metrics"]["neutral"])
        self.assertEqual(1, result["agents"][0]["metrics"]["evidence"])
        self.assertIn("判断：浦发银行", result["agents"][0]["work_items"][0])
        self.assertIn("趋势证据不足", result["agents"][0]["work_items"][0])

    def test_latest_agent_summaries_ignores_fake_verification_run(self):
        class Store:
            def list_runs(self):
                return [
                    {"run_id": "fake", "status": "verification_completed"},
                    {"run_id": "formal", "status": "completed"},
                ]

            def get_run(self, run_id):
                self.selected = run_id
                return {"run_id": run_id, "status": "completed", "executions": []}

        store = Store()
        result = _latest_agent_summaries(store)
        self.assertTrue(result["available"])
        self.assertEqual("formal", store.selected)

    def test_latest_snapshot_adds_refresh_price_to_program_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"
            snapshots.mkdir()
            (snapshots / "market-1.json").write_text(json.dumps({
                "snapshot_id": "market-1", "as_of_time": "2026-08-25T10:00:00+08:00",
                "market": {}, "quotes": {"600000": {"last": 12.34,
                                                       "available_at": "10:00"}},
                "candidates": {
                    "HOLDINGS": [], "QUALITY_VALUE": [], "DEFENSIVE": [],
                    "TACTICAL": [{"symbol": "600000", "name": "浦发银行"}],
                },
            }), encoding="utf-8")
            snapshot = _latest_snapshot(SimpleNamespace(runtime_dir=runtime))
        candidate = snapshot["candidates"]["TACTICAL"][0]
        self.assertEqual(12.34, candidate["refresh_price"])
        self.assertEqual("10:00", candidate["price_available_at"])

    def test_latest_snapshot_can_use_current_account_analysis_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"; snapshots.mkdir()
            (snapshots / "market-old.json").write_text(json.dumps({
                "snapshot_id": "market-old", "market": {}, "quotes": {},
                "candidates": {"HOLDINGS": [{"symbol": "000936"}]},
            }), encoding="utf-8")
            analysis = snapshots / "analysis-account-current.json"
            analysis.write_text(json.dumps({
                "snapshot_id": "market-old", "market": {}, "quotes": {},
                "candidates": {"HOLDINGS": [{"symbol": "000815"}]},
            }), encoding="utf-8")
            analysis.touch()
            snapshot = _latest_snapshot(SimpleNamespace(runtime_dir=runtime))
        self.assertEqual("000815", snapshot["candidates"]["HOLDINGS"][0]["symbol"])

    def test_latest_snapshot_prefers_agent_candidate_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"; snapshots.mkdir()
            (snapshots / "market-new.json").write_text(json.dumps({
                "snapshot_id": "market-new", "market": {}, "quotes": {},
                "candidates": {"HOLDINGS": []},
            }), encoding="utf-8")
            (snapshots / "agent-candidates-old.json").write_text(json.dumps({
                "snapshot_id": "agent-old", "market": {}, "quotes": {},
                "candidates": {"AGENT_SELECTED": [{"symbol": "600000"}]},
            }), encoding="utf-8")
            snapshot = _latest_snapshot(SimpleNamespace(runtime_dir=runtime))
        self.assertEqual("agent-old", snapshot["snapshot_id"])

    def test_latest_snapshot_keeps_shadow_candidates_outside_agent_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"; snapshots.mkdir()
            (snapshots / "market-1.json").write_text(json.dumps({
                "snapshot_id": "market-1", "market": {},
                "quotes": {"600000": {"last": 12.34}},
                "candidates": {"HOLDINGS": []},
                "chanlun_shadow_candidates": {"CHANLUN_SECOND_BUY": [{
                    "symbol": "600000", "signal_id": "600000-1d-second-buy-20260820",
                    "signal": "second_buy", "status": "active",
                }]},
            }), encoding="utf-8")
            snapshot = _latest_snapshot(SimpleNamespace(runtime_dir=runtime))
        candidate = snapshot["candidates"]["chanlun_shadow_candidates"][0]
        self.assertEqual(12.34, candidate["refresh_price"])
        self.assertNotIn("CHANLUN_SECOND_BUY", snapshot["candidates"])

    def test_latest_snapshot_keeps_full_daily_pool_separate_from_agent_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"; snapshots.mkdir()
            (snapshots / "market-1.json").write_text(json.dumps({
                "snapshot_id": "market-1", "market": {},
                "quotes": {"600000": {"last": 12.34}, "600001": {"last": 8.76}},
                "candidates": {"CHANLUN_SECOND_BUY": [{
                    "symbol": "600000", "signal_id": "selected-signal",
                }]},
                "chanlun_daily_candidates": {
                    "CHANLUN_SECOND_BUY": [{
                        "symbol": "600000", "signal_id": "selected-signal",
                    }],
                    "CHANLUN_THIRD_BUY": [{
                        "symbol": "600001", "signal_id": "not-selected-signal",
                    }],
                },
            }), encoding="utf-8")
            snapshot = _latest_snapshot(SimpleNamespace(runtime_dir=runtime))
        self.assertEqual(2, sum(
            len(rows) for rows in snapshot["chanlun_daily_candidates"].values()
        ))
        self.assertEqual(8.76, snapshot["chanlun_daily_candidates"]["CHANLUN_THIRD_BUY"][0]["refresh_price"])
        self.assertEqual("selected-signal", snapshot["candidates"]["CHANLUN_SECOND_BUY"][0]["signal_id"])
        self.assertEqual([{
            "candidate_source": "CHANLUN_SECOND_BUY", "symbol": "600000",
            "signal_id": "selected-signal",
        }], snapshot["selected_chanlun_identities"])

    def test_latest_snapshot_does_not_apply_unmatched_old_selector_to_new_full_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"; snapshots.mkdir()
            full_pool = snapshots / "market-current.json"
            full_pool.write_text(json.dumps({
                "snapshot_id": "market-current", "market": {}, "quotes": {},
                "candidates": {"HOLDINGS": []}, "chanlun_daily_candidates": {
                    "CHANLUN_SECOND_BUY": [{"symbol": "600000", "signal_id": "current"}],
                    "CHANLUN_THIRD_BUY": [],
                },
            }), encoding="utf-8")
            old_selector = snapshots / "agent-candidates-later.json"
            old_selector.write_text(json.dumps({
                "snapshot_id": "market-old", "market": {}, "quotes": {},
                "candidates": {"CHANLUN_SECOND_BUY": [{
                    "symbol": "600000", "signal_id": "old",
                }]},
            }), encoding="utf-8")
            old_selector.touch()
            snapshot = _latest_snapshot(SimpleNamespace(runtime_dir=runtime))
        self.assertEqual("market-current", snapshot["snapshot_id"])
        self.assertEqual([], snapshot["selected_chanlun_identities"])
        self.assertEqual("current", snapshot["chanlun_daily_candidates"]["CHANLUN_SECOND_BUY"][0]["signal_id"])

    def test_latest_snapshot_keeps_agent_pool_but_uses_newer_market_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            snapshots = runtime / "snapshots"; snapshots.mkdir()
            agent = snapshots / "agent-candidates-old.json"
            agent.write_text(json.dumps({
                "snapshot_id": "agent-old", "as_of_time": "2026-08-27T10:00:00+08:00",
                "market": {"regime": "neutral"},
                "quotes": {"600000": {"last": 10}},
                "monthly_concepts": {"month": "2026-08", "concepts": [{"name": "银行"}]},
                "candidates": {"AGENT_SELECTED": [{"symbol": "600000"}]},
            }), encoding="utf-8")
            newer = snapshots / "market-new.json"
            newer.write_text(json.dumps({
                "snapshot_id": "market-new", "as_of_time": "2026-08-27T15:20:00+08:00",
                "market": {"regime": "offensive"},
                "quotes": {"600000": {"last": 12.5, "available_at": "15:20"}},
                "candidates": {"HOLDINGS": []},
            }), encoding="utf-8")
            newer.touch()
            snapshot = _latest_snapshot(SimpleNamespace(runtime_dir=runtime))
        self.assertEqual("market-new", snapshot["snapshot_id"])
        self.assertEqual("agent-old", snapshot["candidate_snapshot_id"])
        self.assertEqual(12.5, snapshot["candidates"]["AGENT_SELECTED"][0]["refresh_price"])
        self.assertEqual("银行", snapshot["monthly_concepts"]["concepts"][0]["name"])

    def test_non_prediction_task_is_labeled_clearly(self):
        class Store:
            def list_system_tasks(self, limit=20):
                return [{"task_id": "task-1", "action": "enrich",
                         "status": "running", "created_at": "2026-08-25T02:09:07+08:00"}]

        progress = _task_progress(Store())
        self.assertFalse(progress["is_prediction"])
        self.assertIn("不运行 Agent 预测", progress["phase"])

    def test_history_task_reports_live_database_progress(self):
        class Store:
            def list_system_tasks(self, limit=20):
                return [{"task_id": "task-history", "action": "history",
                         "status": "running", "created_at": "2026-08-26T15:24:29+08:00"}]

        class Market:
            def status(self):
                return {"history_target_status": {
                    "completed": 64, "failed": 2, "running": 2, "queued": 32,
                }}

        progress = _task_progress(Store(), Market())
        self.assertEqual(64, progress["completed_items"])
        self.assertEqual(2, progress["failed_items"])
        self.assertEqual(100, progress["total_items"])
        self.assertEqual(66.0, progress["percent"])

    def test_prediction_progress_reports_each_daily_agent(self):
        class Store:
            def list_system_tasks(self, limit=20):
                return [{"task_id": "task-2", "action": "analyze",
                         "status": "running", "created_at": "2026-08-25T10:00:00+08:00"}]

            def list_runs(self):
                return [{"run_id": "run-2", "created_at": "2026-08-25T10:00:01+08:00"}]

            def get_run(self, run_id):
                return {"run_id": run_id, "status": "running", "executions": [
                    {"agent_id": "macro_market", "status": "completed"},
                    {"agent_id": "market_profitability", "status": "running"},
                ]}

        progress = _task_progress(Store())
        self.assertTrue(progress["is_prediction"])
        self.assertEqual(12, progress["total_agents"])
        self.assertEqual(1, progress["completed_agents"])
        self.assertIn("赚钱效应 Agent", progress["phase"])


if __name__ == "__main__":
    unittest.main()
