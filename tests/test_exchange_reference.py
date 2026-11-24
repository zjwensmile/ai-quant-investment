import unittest
from decimal import Decimal

from ai_quant.exchange_reference import ExchangeReferenceProvider


class ExchangeReferenceProviderTests(unittest.TestCase):
    def test_sse_flags_are_derived_from_official_filter_results(self) -> None:
        def loader(url, params, referer):
            if params["SWING_TRADE"] == "是":
                return {"result": [{"FUND_CODE": "513500"}]}
            if params["SUBCLASS"]:
                return {"result": [{"FUND_CODE": "588000"}]}
            return {
                "result": [
                    {
                        "FUND_CODE": "563020",
                        "LISTING_DATE": "2023-12-14",
                        "CATEGORY": "F112",
                    },
                    {"FUND_CODE": "513500"},
                    {"FUND_CODE": "588000"},
                ]
            }

        rows = {
            item.symbol: item
            for item in ExchangeReferenceProvider(loader).fetch_sse_etf_attributes()
        }
        self.assertFalse(rows["563020"].same_day_turnaround)
        self.assertEqual(Decimal("0.10"), rows["563020"].price_limit_ratio)
        self.assertTrue(rows["513500"].same_day_turnaround)
        self.assertEqual(Decimal("0.20"), rows["588000"].price_limit_ratio)

    def test_szse_list_does_not_guess_unpublished_trade_flags(self) -> None:
        def loader(url, params, referer):
            page = int(params["PAGENO"])
            return [
                {
                    "metadata": {
                        "tabkey": "tab1",
                        "pagecount": 2,
                    },
                    "data": [
                        {
                            "sys_key": f"<a>15900{page}</a>",
                            "kzjcurl": "sample",
                        }
                    ],
                }
            ]

        rows = ExchangeReferenceProvider(loader).fetch_szse_etf_list()
        self.assertEqual(["159001", "159002"], [item.symbol for item in rows])
        self.assertTrue(all(item.same_day_turnaround is None for item in rows))
        self.assertTrue(all(item.price_limit_ratio is None for item in rows))


if __name__ == "__main__":
    unittest.main()
