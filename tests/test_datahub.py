import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_quant.datahub import DataHub, SnapshotBuildError, _quote, _index_snapshots
from ai_quant.securities import Board, Exchange, classify_main_board_stock, classify_research_stock


class FakeProvider:
    name = "fake-market-data"

    def fetch_trading_calendar(self):
        return [{"trade_date": "2026-01-01"}, {"trade_date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()},
                {"trade_date": "2026-12-31"}]

    def fetch_stock_spot(self):
        return [
            {"代码": "600000", "名称": "浦发银行", "最新价": 10, "昨收": 9.9},
            {"代码": "002001", "名称": "ST新股", "最新价": 5, "昨收": 5},
            {"代码": "300001", "名称": "创业板样本", "最新价": 20, "昨收": 19},
        ]

    def fetch_etf_spot(self):
        return [{"代码": "513500", "名称": "标普500ETF", "最新价": 1.2, "昨收": 1.19}]

    def fetch_index_spot(self):
        return [
            {"代码": "sh000001", "名称": "上证指数", "最新价": 3800,
             "昨收": 3780, "涨跌额": 20, "涨跌幅": 0.53},
            {"代码": "sz399001", "名称": "深证成指", "最新价": 12000,
             "昨收": 12100, "涨跌额": -100, "涨跌幅": -0.83},
            {"代码": "sz399006", "名称": "创业板指", "最新价": 2600,
             "昨收": 2580, "涨跌额": 20, "涨跌幅": 0.78},
        ]

    def fetch_risk_warning(self):
        return [{"代码": "002001", "名称": "ST新股"}]

    def fetch_suspensions(self, trade_date):
        return [{"代码": "600000", "名称": "浦发银行"}]


class FailingProvider(FakeProvider):
    def fetch_etf_spot(self):
        raise RuntimeError("upstream unavailable")


class NonTradingDayProvider(FakeProvider):
    def fetch_trading_calendar(self):
        return [{"trade_date": "2026-01-01"}, {"trade_date": "2026-12-31"}]


class DataHubTests(unittest.TestCase):
    def test_quote_fetch_time_does_not_invent_holiday_trade_date(self):
        fetched = "2026-10-02T12:00:00+08:00"
        for row, status in (({}, "unavailable"), ({"时间戳": "15:00:00"}, "time_only_without_date")):
            quote = _quote({"最新价": 10, **row}, fetched)
            self.assertEqual(10, quote["last"])
            self.assertEqual(fetched, quote["available_at"])
            self.assertEqual(fetched, quote["fetched_at"])
            self.assertIsNone(quote["source_quote_at"])
            self.assertIsNone(quote["source_trade_date"])
            self.assertEqual(status, quote["quote_time_status"])

    def test_source_quote_date_and_timestamp_survive_fetch_during_holiday(self):
        fetched = "2026-10-02T12:00:00+08:00"
        row = {"最新价": 1.2, "数据日期": "2026-09-30 00:00:00", "更新时间": "2026-09-30 15:00:03+08:00"}
        quote = _quote(row, fetched)
        self.assertEqual("2026-09-30T15:00:03+08:00", quote["source_quote_at"])
        self.assertEqual("2026-09-30", quote["source_trade_date"])
        self.assertEqual("source_datetime", quote["quote_time_status"])
        self.assertEqual(row["更新时间"], quote["source_quote_time_raw"]["更新时间"])
        dated = _quote({"数据日期": 20260930}, fetched)
        self.assertEqual("2026-09-30", dated["source_trade_date"])
        self.assertIsNone(dated["source_quote_at"])
        self.assertEqual("source_date_only", dated["quote_time_status"])
        for fields, status in (({"更新时间": "2026-10-03 15:00:00"}, "source_time_after_fetch"),
                               ({"更新时间": "2026-09-30 15:00:00", "数据日期": "2026-09-29"}, "source_date_conflict"),
                               ({"更新时间": "NaT", "数据日期": "--"}, "unavailable")):
            self.assertEqual(status, _quote(fields, fetched)["quote_time_status"])

    def test_index_quote_time_is_not_replaced_by_fetch_time(self):
        fetched = "2026-10-02T12:00:00+08:00"
        row = {"代码": "sh000001", "最新价": 3800, "时间戳": "2026-09-30 15:00:00"}
        index = _index_snapshots([row], fetched)[0]
        self.assertEqual("2026-09-30T15:00:00+08:00", index["source_quote_at"])
        self.assertEqual(fetched, index["fetched_at"])

    def test_research_classifier_extends_main_board_without_granting_access(self):
        for symbol, expected in (("600000", (Exchange.SSE, Board.MAIN)),
                                 ("688256", (Exchange.SSE, Board.STAR)),
                                 ("300308", (Exchange.SZSE, Board.CHINEXT)),
                                 ("301001", (Exchange.SZSE, Board.CHINEXT))):
            self.assertEqual(expected, classify_research_stock(symbol))
        self.assertIsNone(classify_main_board_stock("688256"))
        self.assertIsNone(classify_main_board_stock("300308"))
        self.assertIsNone(classify_research_stock("510300"))
        self.assertIsNone(classify_research_stock("920001"))

    def test_growth_board_snapshot_is_explicit_and_preserves_trading_metadata(self):
        provider = FakeProvider()
        original_rows = provider.fetch_stock_spot()
        provider.fetch_stock_spot = lambda: original_rows + [
            {"代码": "688256", "名称": "科创样本", "最新价": 100, "昨收": 99},
            {"代码": "301001", "名称": "创业新样本", "最新价": 30, "昨收": 29},
        ]
        as_of = datetime.now(ZoneInfo("Asia/Shanghai"))
        default = DataHub(provider).build_snapshot(as_of).data
        self.assertEqual({"600000", "513500"}, {item["symbol"] for item in default["securities"]})
        account = {"permissions": [], "cash": 10000, "positions": []}
        result = DataHub(provider).build_snapshot(as_of, account, include_growth_boards=True).data
        stocks = {item["symbol"]: item for item in result["securities"]}
        self.assertEqual({"600000", "513500", "300001", "301001", "688256"}, set(stocks))
        self.assertEqual("star", stocks["688256"]["board"])
        self.assertEqual(1, stocks["688256"]["buy_lot_size"])
        self.assertEqual(200, stocks["688256"]["minimum_buy_quantity"])
        self.assertEqual("chinext", stocks["301001"]["board"])
        self.assertEqual(100, stocks["301001"]["buy_lot_size"])
        self.assertEqual("suspended", stocks["600000"]["trading_status"])
        self.assertEqual(account, result["account"])
        self.assertEqual("main_star_chinext", result["data_quality"]["stock_research_scope"])
        self.assertIn("688256", result["quotes"])

    def test_failed_or_uncovered_calendar_is_unknown_not_closed(self):
        for dates in ([], [{"trade_date": "2026-01-01"}]):
            provider = FakeProvider()
            provider.fetch_trading_calendar = lambda: dates
            snapshot = DataHub(provider).build_snapshot(datetime.now(ZoneInfo("Asia/Shanghai"))).data
            self.assertIsNone(snapshot["market"]["is_trading_day"])
            self.assertEqual("unknown", snapshot["market"]["session"])
            self.assertIsNone(snapshot["market"]["latest_completed_trade_date"])
            self.assertFalse(snapshot["market"]["execution_eligible"])

    def test_snapshot_excludes_risk_warning_stocks_and_keeps_provider_etfs(self) -> None:
        snapshot = DataHub(FakeProvider()).build_snapshot(
            datetime(2026, 8, 23, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        ).data
        securities = {item["symbol"]: item for item in snapshot["securities"]}
        self.assertEqual({"600000", "513500"}, set(securities))
        self.assertEqual("suspended", securities["600000"]["trading_status"])
        self.assertEqual("valid", snapshot["data_quality"]["status"])
        self.assertIn(
            "已按账户规则排除 1 只 ST/*ST 股票。",
            snapshot["data_quality"]["warnings"],
        )
        self.assertGreaterEqual(
            datetime.fromisoformat(snapshot["as_of_time"]),
            datetime.fromisoformat(snapshot["data_quality"]["available_at"]),
        )
        self.assertEqual(
            ["sse", "szse", "chinext"],
            [item["id"] for item in snapshot["market"]["indices"]],
        )
        distribution = snapshot["market"]["all_stock_distribution"]
        self.assertEqual(3, distribution["coverage"])
        self.assertEqual(2, distribution["advancers"])
        self.assertEqual(1, distribution["unchanged"])

    def test_critical_failure_does_not_write_partial_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.json"
            with self.assertRaises(SnapshotBuildError):
                DataHub(FailingProvider()).build_snapshot(
                    datetime(2026, 8, 23, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
                )
            self.assertFalse(target.exists())

    def test_non_trading_day_snapshot_is_research_only(self) -> None:
        snapshot = DataHub(NonTradingDayProvider()).build_snapshot(
            datetime(2026, 8, 23, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        ).data
        self.assertEqual("research_only", snapshot["data_quality"]["status"])
        self.assertFalse(snapshot["market"]["is_trading_day"])
        self.assertFalse(snapshot["market"]["execution_eligible"])
        self.assertEqual(2, snapshot["data_quality"]["quotes_without_source_date"])
        self.assertTrue(all(q["source_trade_date"] is None for q in snapshot["quotes"].values()))
        self.assertIn("不是成交时间", snapshot["data_quality"]["quote_time_semantics"])


if __name__ == "__main__":
    unittest.main()
