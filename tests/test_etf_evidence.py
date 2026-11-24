import copy
import json
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

import requests

from ai_quant.providers.etf_evidence import (compact_etf_evidence, enrich_etf_evidence,
    nav_comparison, pairwise_overlaps, parse_holdings, parse_industries)
from ai_quant.etf_strategy import select_etf_research_candidates


NOW = "2026-10-02T22:00:00+08:00"


def holding_html(period="2026-06-30", weights=("7.25", "6.36")):
    rows = "".join(f'<tr><td><a href="//quote.eastmoney.com/unify/r/105.{code}">{code}</a></td>'
                   f'<td>{name}</td><td>{weight}%</td></tr>'
                   for code, name, weight in zip(("NVDA", "AAPL"), ("英伟达", "苹果"), weights))
    return f'<h4 class="t">截止至：<font>{period}</font></h4><table><thead><tr><th>股票代码</th><th>股票名称</th><th>占净值比例</th></tr></thead><tbody>{rows}</tbody></table>'


def industries(symbol="513500", period="2026-06-30"):
    return {"Data": {"FundCode": symbol, "QuarterInfos": [{"JZRQ": period, "HYPZInfo": [
        {"BZDM": symbol, "FSRQ": period, "HYMC": name, "HYDM": str(i), "ZJZBL": weight}
        for i, (name, weight) in enumerate((("信息技术", "36.69"), ("金融", "11.34"), ("制造业", "20"))) ]}]}}


class EtfEvidenceTests(unittest.TestCase):
    def test_holdings_coverage_preserves_undisclosed_residual_and_market_identity(self):
        result = parse_holdings(holding_html(), "513500", NOW)
        self.assertEqual("2026-06-30", result["report_date"])
        self.assertEqual("0.1361", result["disclosed_weight"])
        self.assertEqual("0.8639", result["undisclosed_or_non_equity_weight"])
        self.assertEqual("105.NVDA", result["holdings"][0]["security_id"])
        self.assertEqual(NOW, result["available_at"])
        self.assertFalse(result["complete_portfolio_verified"])

    def test_latest_dated_report_without_future_period_or_normalization(self):
        html = holding_html("2026-03-31", ("1", "2")) + holding_html() + holding_html("2026-12-31")
        self.assertEqual("2026-06-30", parse_holdings(html, "513500", NOW)["report_date"])
        with self.assertRaises(ValueError):
            parse_holdings(holding_html(weights=("80", "30")), "513500", NOW)

    def test_industry_original_taxonomy_is_preserved_without_guessing_tech(self):
        result = parse_industries(industries(), "513500", NOW)
        self.assertEqual("technology", result["industries"][0]["sector"])
        self.assertIsNone(result["industries"][2]["sector"])
        self.assertEqual("制造业", result["industries"][2]["industry_name"])
        self.assertEqual("0.3197", result["undisclosed_or_non_equity_weight"])
        self.assertEqual("0.2", result["unmapped_disclosed_sector_weight"])
        with self.assertRaisesRegex(ValueError, "identity"):
            parse_industries(industries(), "513100", NOW)

    def test_overlap_is_full_disclosed_intersection_lower_bound_not_full_portfolio(self):
        a = {"holdings": parse_holdings(holding_html(), "513500", NOW),
             "industries": parse_industries(industries(), "513500", NOW)}
        b = {"holdings": parse_holdings(holding_html(weights=("10", "3")), "513100", NOW),
             "industries": parse_industries(industries("513100"), "513100", NOW)}
        result = pairwise_overlaps({"513500": a, "513100": b})[0]
        self.assertEqual(Decimal(".1025"), Decimal(result["holdings_overlap_lower_bound"]))
        self.assertEqual(Decimal(".4803"), Decimal(result["sector_overlap_lower_bound"]))
        self.assertIsNone(result["full_portfolio_overlap"])
        b["holdings"]["report_date"] = "2026-03-31"
        self.assertIsNone(pairwise_overlaps({"a": a, "b": b})[0]["holdings_overlap_lower_bound"])
        self.assertIsNone(pairwise_overlaps({"a": a, "b": {"evidence_gaps": ["unavailable"]}})[0]["holdings_overlap_lower_bound"])

    def test_no_name_match_for_different_market_or_share_class(self):
        a = {"holdings": parse_holdings(holding_html(), "513500", NOW)}
        b = copy.deepcopy(a)
        for row in b["holdings"]["holdings"]:
            row["security_id"] = "116." + row["symbol"]
        result = pairwise_overlaps({"a": a, "b": b})[0]
        self.assertEqual("0", result["holdings_overlap_lower_bound"])
        self.assertIsNone(result["full_portfolio_overlap"])

    def test_compact_evidence_calculates_before_truncating_rows(self):
        holdings = parse_holdings(holding_html(), "513500", NOW)
        holdings["holdings"] = [{"security_id": f"105.X{i}", "symbol": f"X{i}", "name": f"Name{i}", "weight": ".02"} for i in range(30)]
        holdings.update(disclosed_weight=".60", undisclosed_or_non_equity_weight=".40")
        funds = {"a": {"holdings": holdings}, "b": {"holdings": copy.deepcopy(holdings)}}
        full = {"etf_evidence": funds, "etf_overlap_evidence": pairwise_overlaps(funds)}
        compact, overlaps = compact_etf_evidence(full)
        self.assertEqual(10, len(compact["a"]["holdings"]["holdings"]))
        self.assertEqual(30, compact["a"]["holdings"]["disclosed_holding_count"])
        self.assertEqual(".40", compact["a"]["holdings"]["undisclosed_or_non_equity_weight"])
        self.assertEqual(10, len(overlaps[0]["common_holdings"]))
        self.assertEqual(20, overlaps[0]["display_truncated_count"])
        self.assertEqual(Decimal(".60"), Decimal(overlaps[0]["holdings_overlap_lower_bound"]))
        self.assertEqual(30, len(full["etf_overlap_evidence"][0]["common_holdings"]))

    def test_nav_date_must_be_source_date_not_fetch_time(self):
        payload = {"Data": {"LSJZList": [{"FSRQ": "2026-09-30", "DWJZ": "2", "LJJZ": "4"}]}}
        for quote in ({"last": 2.1, "available_at": "2026-09-30T15:10:00+08:00"},
                      {"last": 2.1, "source_trade_date": "2026-09-29"}):
            result = nav_comparison(payload, "510300", quote, "a_share", NOW)
            self.assertEqual("quote_date_unknown_or_different_from_nav", result["status"])
            self.assertIsNone(result["price_to_dated_nav_gap"])

    def test_domestic_same_day_close_only_historical_and_uses_unit_not_accumulated_nav(self):
        payload = {"Data": {"LSJZList": [{"FSRQ": "2026-09-30", "DWJZ": "2", "LJJZ": "4"}]}}
        quote = {"last": 2.1, "source_trade_date": "2026-09-30", "source_quote_at": "2026-09-30T15:00:00+08:00"}
        result = nav_comparison(payload, "510300", quote, "a_share", NOW)
        self.assertEqual("historical_same_day_close_nav_comparison", result["status"])
        self.assertEqual("0.05", result["price_to_dated_nav_gap"])
        self.assertFalse(result["execution_usable"])
        self.assertIsNone(result["premium_rate"])
        quote["source_quote_at"] = "2026-09-30T10:30:00+08:00"
        self.assertIsNone(nav_comparison(payload, "510300", quote, "a_share", NOW)["price_to_dated_nav_gap"])

    def test_qdii_same_date_is_still_not_realtime_premium(self):
        payload = {"Data": {"LSJZList": [{"FSRQ": "2026-09-30", "DWJZ": "2"}]}}
        quote = {"last": 2.1, "source_trade_date": "2026-09-30", "source_quote_at": "2026-09-30T15:00:00+08:00"}
        result = nav_comparison(payload, "513500", quote, "us", NOW)
        self.assertEqual("cross_border_or_other_asset_valuation_not_synchronous", result["status"])
        self.assertFalse(result["execution_usable"])
        self.assertIsNone(result["premium_rate"])
        self.assertIn("错时", result["note"])

    def test_bounded_network_failure_preserves_unknown_and_does_not_fetch_bonds(self):
        snapshot = {"securities": [{"symbol": code, "name": name, "security_type": "etf", "exchange": "SSE"}
                    for code, name in (("513500", "标普500ETF"), ("511360", "短融ETF"))]}
        with patch("requests.get", side_effect=requests.Timeout("network unavailable")) as get:
            result = enrich_etf_evidence(snapshot, ["513500", "511360"], timeout=.2)
        self.assertEqual(3, get.call_count)
        self.assertEqual(.2, get.call_args.kwargs["timeout"])
        self.assertNotIn("511360", result["etf_evidence"])
        self.assertEqual(3, len(result["etf_evidence"]["513500"]["evidence_gaps"]))
        self.assertNotIn("holdings", result["etf_evidence"]["513500"])
        self.assertNotIn("etf_evidence", snapshot)
        with self.assertRaisesRegex(ValueError, "shortlist"):
            enrich_etf_evidence(snapshot, [str(i) for i in range(13)])

    def test_rate_limit_stops_remaining_requests(self):
        snapshot = {"securities": [{"symbol": "513500", "name": "标普500ETF", "security_type": "etf", "exchange": "SSE"}]}
        response = Mock(status_code=429)
        response.raise_for_status.side_effect = requests.HTTPError("429")
        with patch("requests.get", return_value=response) as get:
            result = enrich_etf_evidence(snapshot, ["513500"])
        self.assertEqual(1, get.call_count)
        self.assertEqual(3, len(result["etf_evidence"]["513500"]["evidence_gaps"]))

    def test_replaced_quote_preserves_its_own_source(self):
        snapshot = {"securities": [{"symbol": "513500", "name": "标普500ETF", "security_type": "etf", "exchange": "SSE"}],
                    "quotes": {"513500": {"last": 2.1, "source_url": "https://qt.gtimg.cn/q=sh513500", "source_trade_date": "2026-09-30"}},
                    "data_quality": {"source_details": {"etf_spot": "sina"}}}
        payloads = [{"content": holding_html()}, industries(),
                    {"Data": {"LSJZList": [{"FSRQ": "2026-09-30", "DWJZ": "2"}]}}]
        responses = [Mock(status_code=200, text=json.dumps(p)) for p in payloads]
        with patch("requests.get", side_effect=responses):
            result = enrich_etf_evidence(snapshot, ["513500"])
        self.assertEqual("https://qt.gtimg.cn/q=sh513500", result["etf_evidence"]["513500"]["nav_comparison"]["quote_source"])

    def test_verified_hk_broad_candidate_precedes_technology_only_proxy(self):
        rows = [{"symbol": "513180", "name": "恒生科技ETF", "score": 99, "eligible": True},
                {"symbol": "159920", "name": "恒生ETF", "index_id": "恒生指数", "score": 50, "eligible": True}]
        self.assertEqual("159920", select_etf_research_candidates(rows, 1)[0]["symbol"])


if __name__ == "__main__":
    unittest.main()
