from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from subprocess import CompletedProcess
from unittest.mock import patch

from ai_quant.chanlun import (
    AcceptanceResult, ChanlunScanRequest, ChanlunSignal, ExecutionRules, MarketBar,
    RustChanlunBridge, SignalKind, confirm_recent_scans, replay_recent_signals,
    replay_signals, run_acceptance, select_candidates,
)


def _bars(symbol: str = "600000", count: int = 10, *, opening_offset: float = 0) -> list[MarketBar]:
    start = date(2026, 1, 2)
    return [
        MarketBar(symbol, start + timedelta(days=index), 10 + index + opening_offset,
                  10.5 + index + opening_offset, 9.5 + index + opening_offset,
                  10 + index + opening_offset, amount=1000 * (index + 1))
        for index in range(count)
    ]


class ChanlunTests(unittest.TestCase):
    def test_rust_bridge_uses_v1_compact_items_payload_and_parses_metadata(self) -> None:
        bars = _bars(count=3)
        reply = {
            "schema_version": "chanlun_bridge/v1",
            "results": [{"symbol": "600000", "request_id": None, "errors": [],
                         "directions": {"stroke": "up"}, "zhongshus": [{"zg": 11}],
                         "markers": [{"kind": "second_buy", "bar_index": 1, "price": 10.1}]}],
        }
        completed = CompletedProcess(["bridge"], 0, stdout=json.dumps(reply), stderr="")
        with patch("ai_quant.chanlun.subprocess.run", return_value=completed) as run:
            signals = RustChanlunBridge(["bridge"]).scan({"600000": bars})
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual("600000", sent["items"][0]["symbol"])
        self.assertEqual("600000", sent["items"][0]["request_id"])
        self.assertEqual(3, len(sent["items"][0]["dates"]))
        self.assertEqual(SignalKind.SECOND_BUY, signals["600000"][0].kind)
        self.assertEqual({"directions": {"stroke": "up"}, "zhongshus": [{"zg": 11}]}, signals["600000"][0].chanlun)

    def test_rust_bridge_request_ids_allow_multiple_prefixes_for_one_symbol(self) -> None:
        bars = _bars(count=3)
        reply = {"schema_version": "chanlun_bridge/v1", "results": [
            {"symbol": "600000", "request_id": "first", "errors": [], "markers": []},
            {"symbol": "600000", "request_id": "second", "errors": [], "markers": [
                {"kind": "third_buy", "bar_index": 2, "anchor_trade_date": bars[2].trade_date.isoformat()}]},
        ]}
        completed = CompletedProcess(["bridge"], 0, stdout=json.dumps(reply), stderr="")
        with patch("ai_quant.chanlun.subprocess.run", return_value=completed):
            output = RustChanlunBridge(["bridge"]).scan_requests([
                ChanlunScanRequest("first", "600000", tuple(bars[:2])),
                ChanlunScanRequest("second", "600000", tuple(bars)),
            ])
        self.assertEqual([], output["first"])
        self.assertIs(SignalKind.THIRD_BUY, output["second"][0].kind)

    def test_rust_bridge_replay_requests_confirmation_dates(self) -> None:
        bars = _bars(count=4)
        reply = {"schema_version": "chanlun_bridge/v1", "results": [{
            "symbol": "600000", "request_id": "600000", "errors": [],
            "directions": {}, "zhongshus": [], "markers": [{
                "kind": "second_buy", "bar_index": 1,
                "anchor_trade_date": bars[1].trade_date.isoformat(),
                "confirmed_trade_date": bars[3].trade_date.isoformat(),
            }],
        }]}
        completed = CompletedProcess(["bridge"], 0, stdout=json.dumps(reply), stderr="")
        with patch("ai_quant.chanlun.subprocess.run", return_value=completed) as run:
            signals = RustChanlunBridge(["bridge"]).replay({"600000": bars})
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertTrue(sent["replay"])
        self.assertEqual(bars[3].trade_date, signals["600000"][0].confirmed_date)
        self.assertEqual(bars[3].trade_date, signals["600000"][0].first_visible_date)

    def test_recent_prefix_scan_sends_one_history_and_reconstructs_views(self) -> None:
        bars = _bars(count=8)
        reply = {"schema_version": "chanlun_bridge/v1", "results": [{
            "symbol": "600000", "request_id": "600000", "errors": [],
            "directions": {}, "zhongshus": [], "markers": [{
                "kind": "third_buy", "bar_index": 4,
                "anchor_trade_date": bars[4].trade_date.isoformat(),
                "confirmed_trade_date": bars[6].trade_date.isoformat(),
            }],
        }]}
        completed = CompletedProcess(["bridge"], 0, stdout=json.dumps(reply), stderr="")
        with patch("ai_quant.chanlun.subprocess.run", return_value=completed) as run:
            scans = RustChanlunBridge(["bridge"]).scan_recent_prefixes(
                {"600000": bars}, bars[-1].trade_date, sessions=5
            )
        sent = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(5, sent["recent_sessions"])
        self.assertEqual(1, len(sent["items"]))
        self.assertEqual(8, len(sent["items"][0]["dates"]))
        confirmed = confirm_recent_scans(scans, bars[-1].trade_date, sessions=5)
        self.assertEqual(bars[6].trade_date, confirmed[0].confirmed_date)

    def test_rust_bridge_rejects_unknown_schema_version(self) -> None:
        completed = CompletedProcess(["bridge"], 0, stdout='{"results": []}', stderr="")
        with patch("ai_quant.chanlun.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "schema_version"):
                RustChanlunBridge(["bridge"]).scan({"600000": _bars(count=1)})

    def test_replay_stamps_confirmation_at_first_visible_prefix(self) -> None:
        bars = _bars(count=5)

        def scanner(prefixes):
            prefix = prefixes["600000"]
            return {"600000": ([ChanlunSignal("600000", SignalKind.SECOND_BUY, prefix[1].trade_date)] if len(prefix) >= 4 else [])}

        found = replay_signals({"600000": bars}, scanner)
        self.assertEqual([ChanlunSignal("600000", SignalKind.SECOND_BUY, bars[1].trade_date,
                                         bars[3].trade_date, bars[3].trade_date)], found)

    def test_recent_confirmation_and_candidate_invalidation(self) -> None:
        bars_a = _bars("600000", 8)
        bars_b = _bars("600001", 8)
        old = ChanlunSignal("600000", SignalKind.SECOND_BUY, bars_a[1].trade_date)
        new = ChanlunSignal("600000", SignalKind.THIRD_BUY, bars_a[5].trade_date)
        scans = {
            ("600000", bars_a[2].trade_date): [old], ("600000", bars_a[3].trade_date): [old],
            ("600000", bars_a[4].trade_date): [old], ("600000", bars_a[5].trade_date): [old, new],
            ("600000", bars_a[6].trade_date): [old, new], ("600000", bars_a[7].trade_date): [old, new],
        }
        self.assertEqual([new.confirmed(bars_a[5].trade_date)], confirm_recent_scans(scans, bars_a[7].trade_date))
        signals = [
            old.confirmed(bars_a[5].trade_date),
            ChanlunSignal("600001", SignalKind.THIRD_BUY, bars_b[2].trade_date).confirmed(bars_b[6].trade_date),
            ChanlunSignal("600000", SignalKind.THIRD_SELL, bars_a[3].trade_date).confirmed(bars_a[7].trade_date),
        ]
        candidates = select_candidates(signals, {"600000": bars_a, "600001": bars_b}, bars_a[7].trade_date)
        self.assertEqual(["600001"], [candidate.symbol for candidate in candidates])
        self.assertEqual("CHANLUN_THIRD_BUY", candidates[0].candidate_source)

    def test_fresh_confirmation_does_not_revive_an_anchor_older_than_twenty_bars(self) -> None:
        bars = _bars(count=30)
        stale_anchor = ChanlunSignal(
            "600000", SignalKind.SECOND_BUY, bars[0].trade_date
        ).confirmed(bars[-1].trade_date)
        self.assertEqual(
            [],
            select_candidates(
                [stale_anchor], {"600000": bars}, bars[-1].trade_date
            ),
        )

    def test_stale_sell_anchor_does_not_invalidate_a_current_buy(self) -> None:
        bars = _bars(count=30)
        buy = ChanlunSignal(
            "600000", SignalKind.SECOND_BUY, bars[-3].trade_date
        ).confirmed(bars[-2].trade_date)
        stale_sell = ChanlunSignal(
            "600000", SignalKind.THIRD_SELL, bars[0].trade_date
        ).confirmed(bars[-1].trade_date)
        candidates = select_candidates(
            [buy, stale_sell], {"600000": bars}, bars[-1].trade_date
        )
        self.assertEqual([buy.signal_id], [item.signal_id for item in candidates])

    def test_recent_replay_does_not_reconfirm_old_marker(self) -> None:
        bars = _bars(count=8)
        scanner = lambda prefixes: {"600000": [ChanlunSignal("600000", SignalKind.SECOND_BUY, prefixes["600000"][1].trade_date)]}
        self.assertEqual([], replay_recent_signals({"600000": bars}, scanner, bars[-1].trade_date, sessions=5))

    def test_acceptance_executes_on_raw_bars_and_uses_one_yuan_commission(self) -> None:
        signal_bars = _bars(count=10)
        raw_bars = _bars(count=10, opening_offset=100)

        def scanner(prefixes):
            prefix = prefixes["600000"]
            signal = []
            if len(prefix) >= 3:
                signal.append(ChanlunSignal("600000", SignalKind.SECOND_BUY, prefix[2].trade_date))
            if len(prefix) >= 7:
                signal.append(ChanlunSignal("600000", SignalKind.THIRD_SELL, prefix[6].trade_date))
            return {"600000": signal}

        result = run_acceptance(
            {"600000": signal_bars}, scanner, execution_bars_by_symbol={"600000": raw_bars},
            rules=ExecutionRules(initial_cash=100_000, minimum_history_days=10, minimum_completed_trades=1),
        )
        self.assertIsInstance(result, AcceptanceResult)
        self.assertEqual(1, result.completed_trades)
        self.assertEqual(raw_bars[3].open * 800 + 1, result.trades[0]["entry_cost"])
        self.assertEqual(raw_bars[7].trade_date.isoformat(), result.trades[0]["exit_date"])
        self.assertTrue(result.sufficient_sample)

    def test_acceptance_uses_historical_replay_and_does_not_count_open_marks(self) -> None:
        bars = _bars(count=10)
        confirmed = ChanlunSignal(
            "600000", SignalKind.SECOND_BUY, bars[2].trade_date
        ).confirmed(bars[3].trade_date)

        def ordinary_scanner(_):
            raise AssertionError("daily Python replay should not run")

        result = run_acceptance(
            {"600000": bars},
            ordinary_scanner,
            historical_scanner=lambda _: {"600000": [confirmed]},
            rules=ExecutionRules(
                minimum_history_days=10, minimum_completed_trades=1
            ),
        )
        self.assertEqual(0, result.completed_trades)
        self.assertFalse(result.sufficient_sample)
        self.assertEqual(1, len(result.trades))
        self.assertTrue(result.trades[0]["marked_to_market"])

    def test_acceptance_does_not_buy_a_late_confirmed_stale_anchor(self) -> None:
        bars = _bars(count=30)
        stale_anchor = ChanlunSignal(
            "600000", SignalKind.SECOND_BUY, bars[0].trade_date
        ).confirmed(bars[-2].trade_date)
        result = run_acceptance(
            {"600000": bars},
            lambda _: {},
            historical_scanner=lambda _: {"600000": [stale_anchor]},
            rules=ExecutionRules(
                minimum_history_days=30, minimum_completed_trades=0
            ),
        )
        self.assertEqual((), result.trades)

    def test_acceptance_does_not_exit_on_a_late_confirmed_stale_sell(self) -> None:
        bars = _bars(count=30)
        buy = ChanlunSignal(
            "600000", SignalKind.SECOND_BUY, bars[2].trade_date
        ).confirmed(bars[3].trade_date)
        stale_sell = ChanlunSignal(
            "600000", SignalKind.THIRD_SELL, bars[0].trade_date
        ).confirmed(bars[-2].trade_date)
        result = run_acceptance(
            {"600000": bars},
            lambda _: {},
            historical_scanner=lambda _: {"600000": [buy, stale_sell]},
            rules=ExecutionRules(
                minimum_history_days=30, minimum_completed_trades=1
            ),
        )
        self.assertEqual(0, result.completed_trades)
        self.assertTrue(result.trades[0]["marked_to_market"])

    def test_acceptance_does_not_charge_stock_stamp_duty_to_etf(self) -> None:
        bars = _bars("510300", count=10, opening_offset=100)
        buy = ChanlunSignal(
            "510300", SignalKind.SECOND_BUY, bars[2].trade_date
        ).confirmed(bars[2].trade_date)
        sell = ChanlunSignal(
            "510300", SignalKind.THIRD_SELL, bars[6].trade_date
        ).confirmed(bars[6].trade_date)
        result = run_acceptance(
            {"510300": bars},
            lambda _: {},
            historical_scanner=lambda _: {"510300": [buy, sell]},
            stamp_duty_rate_by_symbol={"510300": 0.0},
            rules=ExecutionRules(
                initial_cash=100_000,
                minimum_history_days=10,
                minimum_completed_trades=1,
            ),
        )
        trade = result.trades[0]
        gross = int(trade["quantity"]) * float(trade["exit_price"])
        self.assertAlmostEqual(gross - 1, (1 + float(trade["net_return"])) * float(trade["entry_cost"]))

    def test_acceptance_applies_the_same_twenty_candidate_cap(self) -> None:
        bars_by_symbol = {
            f"60{index:04d}": _bars(f"60{index:04d}", count=8)
            for index in range(21)
        }
        replayed = {
            symbol: [
                ChanlunSignal(
                    symbol, SignalKind.SECOND_BUY, bars[2].trade_date
                ).confirmed(bars[3].trade_date)
            ]
            for symbol, bars in bars_by_symbol.items()
        }
        result = run_acceptance(
            bars_by_symbol,
            lambda _: {},
            historical_scanner=lambda _: replayed,
            rules=ExecutionRules(
                initial_cash=1_000_000,
                minimum_history_days=8,
                minimum_completed_trades=0,
            ),
        )
        self.assertEqual(20, len(result.trades))


if __name__ == "__main__":
    unittest.main()
