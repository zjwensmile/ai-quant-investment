import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ai_quant.contexts import ContextBuilder
from ai_quant.chanlun import ChanlunSignal, SignalKind
from ai_quant.features import FeatureEngine, _daily_bar_cutoff
from ai_quant.market_store import CorporateAction, DailyBar, MarketStore
from ai_quant.registry import get_agent


class FeatureTests(unittest.TestCase):
    def test_authoritative_cutoff_excludes_weekend_and_holiday_calendar_days(self) -> None:
        for as_of, completed in (
            ("2026-09-06T23:00:00+08:00", "2026-09-04"),
            ("2026-09-07T10:00:00+08:00", "2026-09-04"),
            ("2026-10-05T10:00:00+08:00", "2026-09-30"),
        ):
            with self.subTest(as_of=as_of):
                snapshot = {"as_of_time": as_of, "market": {
                    "session": "closed", "latest_completed_trade_date": completed,
                }}
                self.assertEqual(date.fromisoformat(completed), _daily_bar_cutoff(snapshot))

    def test_candidates_include_features_and_portfolio_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2026, 5, 1)
            bars = [
                DailyBar(
                    "600000", start + timedelta(days=index), 10 + index / 100,
                    10.2 + index / 100, 9.8 + index / 100, 10 + index / 100,
                    1000 + index, 10000 + index * 100,
                )
                for index in range(70)
            ]
            store.upsert_bars(bars, "raw", "fake", "2026-08-23T08:00:00+08:00")
            snapshot = {
                "snapshot_id": "test",
                "as_of_time": "2026-08-23T08:30:00+08:00",
                "account": {
                    "capital_budget": 5000,
                    "cash": None,
                    "positions": [{"symbol": "600000", "quantity": 100}],
                },
                "market": {},
                "events": [],
                "securities": [{"symbol": "600000", "name": "浦发银行", "security_type": "stock"}],
                "quotes": {"600000": {"last": 10.69}},
                "data_quality": {"status": "research_only"},
            }
            enriched = FeatureEngine(store).enrich(snapshot)
            self.assertEqual("600000", enriched["candidates"]["HOLDINGS"][0]["symbol"])
            self.assertTrue(enriched["candidates"]["HOLDINGS"][0]["held"])
            self.assertIn("必须复核", enriched["candidates"]["HOLDINGS"][0]["program_reason"])
            self.assertEqual(
                {"HOLDINGS", "CHANLUN_SECOND_BUY", "CHANLUN_THIRD_BUY"},
                set(enriched["candidates"]),
            )
            self.assertEqual(
                "rust_chanlun_full_pool_then_agent_selects_up_to_20",
                enriched["candidate_policy"]["candidate_selection"],
            )
            self.assertFalse(
                enriched["candidate_policy"]["program_ranked_new_buys"]
            )
            self.assertEqual(["600000"], enriched["portfolio_context"]["held_symbols"])
            self.assertEqual(3931, enriched["portfolio_context"]["planning_unallocated_budget"])
            self.assertEqual("2026-08-22", enriched["feature_coverage"]["daily_bar_cutoff"])
            self.assertEqual(
                str(bars[-1].trade_date),
                enriched["candidates"]["HOLDINGS"][0]["features"]["history_as_of_date"],
            )
            self.assertEqual(
                {str(bars[-1].trade_date): 1},
                enriched["feature_coverage"]["history_as_of_distribution"],
            )
            self.assertEqual("insufficient_data", enriched["market"]["regime"]["state"])
            self.assertEqual(
                "insufficient_data", enriched["market"]["sector_diffusion"]["status"]
            )
            context = ContextBuilder().build(get_agent("trend_swing"), enriched)
            self.assertEqual(
                {"HOLDINGS", "CHANLUN_SECOND_BUY", "CHANLUN_THIRD_BUY"},
                set(context["candidates"]),
            )
            self.assertEqual({"600000"}, set(context["securities"]))

    def test_feature_engine_does_not_read_a_future_daily_bar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2026, 1, 1)
            bars = [
                DailyBar(
                    "600000",
                    start + timedelta(days=index),
                    10 + index / 100,
                    10.2 + index / 100,
                    9.8 + index / 100,
                    10 + index / 100,
                    1000,
                    10000,
                )
                for index in range(61)
            ]
            store.upsert_bars(bars, "raw", "fake", "2026-03-02T18:00:00+08:00")
            store.upsert_bars(
                [DailyBar("600000", date(2026, 3, 3), 1000, 1000, 1000, 1000, 1, 1000)],
                "raw", "fake", "2026-03-03T18:00:00+08:00",
            )
            snapshot = {
                "snapshot_id": "point-in-time",
                "as_of_time": "2026-03-03T08:30:00+08:00",
                "account": {"positions": [{"symbol": "600000", "quantity": 100}]},
                "market": {"session": "pre_market"},
                "events": [],
                "securities": [
                    {
                        "symbol": "600000",
                        "name": "浦发银行",
                        "security_type": "stock",
                        "trading_status": "tradable",
                        "risk_warning": False,
                    }
                ],
                "quotes": {},
                "data_quality": {"status": "research_only"},
            }
            enriched = FeatureEngine(store).enrich(snapshot)
            self.assertEqual(
                10.6, enriched["candidates"]["HOLDINGS"][0]["features"]["last_raw_close"]
            )

    def test_post_market_complete_quote_fills_missing_current_daily_bar_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2026, 1, 2)
            bars = [
                DailyBar("600000", start + timedelta(days=index), 10, 10, 10, 10,
                         1000, 10000)
                for index in range(61)
            ]
            store.upsert_bars(bars, "raw", "fake", "2026-03-03T16:00:00+08:00")
            snapshot = {
                "snapshot_id": "post-market-overlay",
                "as_of_time": "2026-03-04T18:00:00+08:00",
                "account": {"positions": [{"symbol": "600000", "quantity": 100}]},
                "market": {"session": "post_market", "is_trading_day": True},
                "events": [],
                "securities": [{"symbol": "600000", "name": "浦发银行",
                                "security_type": "stock", "trading_status": "tradable",
                                "risk_warning": False}],
                "quotes": {"600000": {"open": 10, "high": 20, "low": 10,
                                         "last": 20, "volume": 2000,
                                         "turnover": 30000,
                                         "available_at": "2026-03-04T17:00:00+08:00"}},
                "data_quality": {"status": "research_only"},
            }
            enriched = FeatureEngine(store).enrich(snapshot)
            features = enriched["candidates"]["HOLDINGS"][0]["features"]
            self.assertEqual(20, features["last_raw_close"])
            self.assertAlmostEqual(1.0, features["return_1d"])
            self.assertEqual(1, enriched["feature_coverage"]["post_market_quote_overlays"])

    def test_features_neutralize_known_corporate_action_without_changing_raw_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2026, 1, 1)
            bars = [
                DailyBar(
                    "600000", start + timedelta(days=index), 10, 10, 10, 10,
                    1000, 10000,
                )
                for index in range(61)
            ]
            bars[-1] = DailyBar(
                "600000", bars[-1].trade_date, 8.25, 8.25, 8.25, 8.25, 1000, 10000
            )
            store.upsert_bars(bars, "raw", "fake", "2026-03-02T18:00:00+08:00")
            store.upsert_actions(
                [
                    CorporateAction(
                        "600000", date(2026, 2, 1), bars[-1].trade_date,
                        bars[-2].trade_date, 1.0, 1.0, 1.0, "implemented", {},
                    )
                ],
                "fake",
                "2026-02-01T18:00:00+08:00",
            )
            snapshot = {
                "snapshot_id": "adjustment",
                "as_of_time": "2026-03-03T18:00:00+08:00",
                "account": {"positions": [{"symbol": "600000", "quantity": 100}]},
                "market": {"session": "post_market"},
                "events": [],
                "securities": [{
                    "symbol": "600000", "name": "浦发银行", "security_type": "stock",
                    "trading_status": "tradable", "risk_warning": False,
                }],
                "quotes": {},
                "data_quality": {"status": "research_only"},
            }
            enriched = FeatureEngine(store).enrich(snapshot)
            features = enriched["candidates"]["HOLDINGS"][0]["features"]
            self.assertAlmostEqual(0.0, features["return_1d"])
            self.assertEqual(8.25, features["last_raw_close"])
            self.assertEqual(1.0, features["corporate_actions_applied"])

    def test_future_announcement_does_not_adjust_historical_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2026, 1, 1)
            bars = [
                DailyBar("600000", start + timedelta(days=index), 10, 10, 10, 10,
                         1000, 10000)
                for index in range(61)
            ]
            bars[-1] = DailyBar(
                "600000", bars[-1].trade_date, 8.25, 8.25, 8.25, 8.25, 1000, 10000
            )
            store.upsert_bars(bars, "raw", "fake", "2026-03-02T18:00:00+08:00")
            store.upsert_actions(
                [CorporateAction(
                    "600000", date(2026, 3, 4), bars[-1].trade_date,
                    bars[-2].trade_date, 1.0, 1.0, 1.0, "implemented", {},
                )],
                "fake", "2026-03-04T18:00:00+08:00",
            )
            snapshot = {
                "snapshot_id": "future-action",
                "as_of_time": "2026-03-03T18:00:00+08:00",
                "account": {"positions": [{"symbol": "600000", "quantity": 100}]},
                "market": {"session": "post_market"}, "events": [],
                "securities": [{"symbol": "600000", "name": "浦发银行",
                                "security_type": "stock", "trading_status": "tradable",
                                "risk_warning": False}],
                "quotes": {}, "data_quality": {"status": "research_only"},
            }
            enriched = FeatureEngine(store).enrich(snapshot)
            self.assertAlmostEqual(
                -0.175, enriched["candidates"]["HOLDINGS"][0]["features"]["return_1d"]
            )

    def test_action_announced_after_ex_date_is_never_used_retroactively(self) -> None:
        from ai_quant.features import _signal_closes

        bars = [
            {"trade_date": "2026-01-02", "close": 10.0},
            {"trade_date": "2026-01-05", "close": 8.25},
        ]
        actions = [{
            "announcement_date": "2026-01-06",
            "ex_date": "2026-01-05",
            "cash_dividend_per_10": 1.0,
            "bonus_shares_per_10": 1.0,
            "transfer_shares_per_10": 1.0,
        }]
        closes, applied = _signal_closes(bars, actions)
        self.assertEqual(0, applied)
        self.assertAlmostEqual(8.25, closes[-1])

    def test_chanlun_shadow_is_isolated_and_active_mode_routes_candidate(self) -> None:
        class FakeBridge:
            def scan_recent_prefixes(self, bars_by_symbol, as_of_date, *, sessions=5):
                scan_dates = sorted({
                    bar.trade_date
                    for bars in bars_by_symbol.values()
                    for bar in bars
                    if bar.trade_date <= as_of_date
                })[-(sessions + 1):]
                result = {}
                for symbol, bars in bars_by_symbol.items():
                    anchor = bars[-4].trade_date
                    signal = ChanlunSignal(
                        symbol,
                        SignalKind.SECOND_BUY,
                        anchor,
                        price=bars[-4].low,
                        chanlun={"directions": {"trend": "up"}, "zhongshus": []},
                    )
                    for index, visible_date in enumerate(scan_dates):
                        result[(symbol, visible_date)] = [signal] if index >= 2 else []
                return result

        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2025, 1, 1)
            bars = [
                DailyBar(
                    "600000", start + timedelta(days=index), 10 + index / 100,
                    10.2 + index / 100, 9.8 + index / 100, 10 + index / 100,
                    1000 + index, 10000 + index * 100,
                )
                for index in range(260)
            ]
            store.upsert_bars(bars, "raw", "fake", "2026-01-01T18:00:00+08:00")
            snapshot = {
                "snapshot_id": "chanlun-mode",
                "as_of_time": "2026-01-02T18:00:00+08:00",
                "account": {"positions": []},
                "market": {"session": "post_market", "is_trading_day": False},
                "events": [],
                "securities": [{
                    "symbol": "600000", "name": "浦发银行", "security_type": "stock",
                    "trading_status": "tradable", "risk_warning": False,
                }],
                "quotes": {},
                "data_quality": {"status": "research_only"},
            }

            shadow = FeatureEngine(
                store, chanlun_bridge=FakeBridge(), chanlun_mode="shadow"
            ).enrich(snapshot)
            self.assertEqual([], shadow["candidates"]["CHANLUN_SECOND_BUY"])
            shadow_candidate = shadow["chanlun_daily_candidates"]["CHANLUN_SECOND_BUY"][0]
            self.assertEqual(
                shadow_candidate["signal_id"],
                shadow["chanlun_shadow_candidates"]["CHANLUN_SECOND_BUY"][0]["signal_id"],
            )
            self.assertEqual("second_buy", shadow_candidate["chanlun"]["signal"])
            self.assertTrue(shadow_candidate["signal_id"].startswith("600000-1d-second_buy-"))
            self.assertEqual("shadow", shadow["feature_coverage"]["chanlun"]["mode"])

            active = FeatureEngine(
                store, chanlun_bridge=FakeBridge(), chanlun_mode="active"
            ).enrich(snapshot)
            self.assertEqual(
                shadow_candidate["signal_id"],
                active["chanlun_daily_candidates"]["CHANLUN_SECOND_BUY"][0]["signal_id"],
            )
            self.assertEqual([], active["candidates"]["CHANLUN_SECOND_BUY"])
            self.assertEqual({}, active["chanlun_shadow_candidates"])
            self.assertEqual("active", active["feature_coverage"]["chanlun"]["mode"])
            self.assertFalse(
                active["feature_coverage"]["chanlun"]["reference_validation_passed"]
            )

    def test_chanlun_daily_pool_is_not_truncated_by_agent_selection_limit(self) -> None:
        class FakeBridge:
            def scan_recent_prefixes(self, bars_by_symbol, as_of_date, *, sessions=20):
                scan_dates = sorted({
                    bar.trade_date
                    for bars in bars_by_symbol.values()
                    for bar in bars
                    if bar.trade_date <= as_of_date
                })[-(sessions + 1):]
                result = {}
                for symbol, bars in bars_by_symbol.items():
                    signal = ChanlunSignal(
                        symbol,
                        SignalKind.SECOND_BUY,
                        bars[-4].trade_date,
                        price=bars[-4].low,
                        chanlun={"directions": {"trend": "up"}, "zhongshus": []},
                    )
                    for index, visible_date in enumerate(scan_dates):
                        result[(symbol, visible_date)] = [signal] if index >= 2 else []
                return result

        with tempfile.TemporaryDirectory() as directory:
            store = MarketStore(Path(directory) / "market.sqlite3")
            store.initialize()
            start = date(2025, 1, 1)
            symbols = [f"60{index:04d}" for index in range(25)]
            bars = [
                DailyBar(
                    symbol, start + timedelta(days=index), 10 + index / 100,
                    10.2 + index / 100, 9.8 + index / 100, 10 + index / 100,
                    1000 + index, 10000 + index * 100,
                )
                for symbol in symbols
                for index in range(260)
            ]
            store.upsert_bars(bars, "raw", "fake", "2026-01-01T18:00:00+08:00")
            snapshot = {
                "snapshot_id": "full-chanlun-pool",
                "as_of_time": "2026-01-02T18:00:00+08:00",
                "account": {"positions": []},
                "market": {"session": "post_market", "is_trading_day": False},
                "events": [],
                "securities": [{
                    "symbol": symbol,
                    "name": symbol,
                    "security_type": "stock",
                    "trading_status": "tradable",
                    "risk_warning": False,
                } for symbol in symbols],
                "quotes": {},
                "data_quality": {"status": "research_only"},
            }

            enriched = FeatureEngine(
                store,
                top_n=3,
                chanlun_bridge=FakeBridge(),
                chanlun_mode="active",
            ).enrich(snapshot)

            self.assertEqual(
                25,
                len(enriched["chanlun_daily_candidates"]["CHANLUN_SECOND_BUY"]),
            )
            self.assertEqual(3, enriched["candidate_policy"]["agent_selection_maximum"])
            self.assertEqual([], enriched["candidates"]["CHANLUN_SECOND_BUY"])


if __name__ == "__main__":
    unittest.main()
