"""Unknown risk-list coverage permits research, never confirms an entry signal."""

import unittest

from ai_quant.closing_strategy import build_closing_candidates


def candidate(symbol="000001", risk_warning=None, name="示例公司", trading_status="tradable", amount=100_000_000):
    return {"symbol": symbol, "name": name, "security_type": "stock",
            "security": {"symbol": symbol, "name": name, "security_type": "stock",
                         "risk_warning": risk_warning, "trading_status": trading_status},
            "quote": {"last": 10}, "features": {"history_as_of_date": "2026-09-08",
                "amount_20d_average": amount, "return_20d": .02, "close_to_ma20": .01}}


def scan(*rows):
    return build_closing_candidates({"as_of_time": "2026-09-09T09:00:00+08:00",
        "research_universe": list(rows), "data_quality": {"warnings": ["风险警示名单源不可用"]}})


class ClosingCandidateRiskCoverageTests(unittest.TestCase):
    def test_unavailable_risk_list_does_not_empty_research_pool(self):
        result = scan(candidate("000001"), candidate("000002"))
        self.assertEqual(2, result["eligible_count"])
        self.assertEqual(0, result["excluded_count"])
        self.assertEqual({"000001", "000002"}, {row["symbol"] for row in result["candidates"]})
        self.assertEqual("research_only", result["quality"]["status"])
        for row in result["candidates"]:
            self.assertIsNone(row["security"]["risk_warning"])
            self.assertTrue(row["eligible"])
            self.assertFalse(row["signal_confirmed"])
            self.assertTrue(any("风险警示名单" in gap and "仅可条件研究" in gap for gap in row["evidence_gaps"]))

    def test_known_risk_warning_is_still_excluded(self):
        result = scan(candidate(risk_warning=True))
        self.assertEqual([], result["candidates"])
        self.assertEqual(1, result["excluded_count"])

    def test_st_name_is_excluded_even_when_risk_list_is_unknown(self):
        for name in ("ST示例", "*ST示例", "st示例"):
            with self.subTest(name=name):
                result = scan(candidate(name=name))
                self.assertEqual([], result["candidates"])
                self.assertEqual(1, result["excluded_count"])

    def test_unknown_risk_does_not_bypass_liquidity_or_trading_status(self):
        for row in (candidate(amount=99_999_999), candidate(trading_status="suspended")):
            with self.subTest(row=row):
                result = scan(row)
                self.assertEqual([], result["candidates"])
                self.assertEqual(1, result["excluded_count"])
