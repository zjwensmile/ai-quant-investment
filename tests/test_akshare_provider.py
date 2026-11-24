import unittest
from datetime import date, datetime
from unittest.mock import patch

from ai_quant.providers import AkshareProvider


class Frame:
    def __init__(self, records):
        self._records = records
        self.columns = list(records[0]) if records else ["代码"]

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError(orient)
        return self._records


class FakeAkshare:
    def stock_zh_a_spot(self):
        return Frame([{"代码": "sh600000", "名称": "浦发银行"}])

    def stock_zh_a_spot_em(self):
        raise AssertionError("Sina succeeded, Eastmoney must not be called")

    def fund_etf_category_sina(self, symbol):
        self.etf_symbol = symbol
        return Frame([{"代码": "sh510300", "名称": "沪深300ETF"}])

    def fund_etf_spot_em(self):
        raise AssertionError("Sina succeeded, Eastmoney must not be called")

    def stock_zh_a_st_em(self):
        raise RuntimeError("eastmoney unavailable")

    def stock_tfp_em(self, date):
        raise RuntimeError("eastmoney unavailable")

    def tool_trade_date_hist_sina(self):
        return Frame([{"trade_date": date(2026, 8, 21)}])


class AkshareProviderTests(unittest.TestCase):
    def test_sina_is_primary_for_stock_and_etf(self) -> None:
        module = FakeAkshare()
        provider = AkshareProvider(module)
        self.assertEqual(1, len(provider.fetch_stock_spot()))
        self.assertEqual(1, len(provider.fetch_etf_spot()))
        self.assertEqual("ETF基金", module.etf_symbol)
        self.assertEqual("sina", provider.sources["stock_spot"])
        self.assertEqual("sina", provider.sources["etf_spot"])

    def test_auxiliary_failure_is_explicitly_incomplete(self) -> None:
        provider = AkshareProvider(FakeAkshare())
        self.assertEqual([], provider.fetch_risk_warning())
        self.assertEqual([], provider.fetch_suspensions(date(2026, 8, 23)))
        self.assertFalse(provider.risk_warning_complete)
        self.assertFalse(provider.suspension_complete)
        self.assertEqual(2, len(provider.warnings))

    def test_trading_calendar_uses_sina(self) -> None:
        provider = AkshareProvider(FakeAkshare())
        with patch("ai_quant.providers.exchange_calendar.verified_exchange_calendar", return_value=([], {})):
            self.assertEqual(1, len(provider.fetch_trading_calendar()))
        self.assertEqual("sina", provider.sources["trading_calendar"])

    def test_verified_exchange_calendar_has_year_bound_and_holidays(self):
        from ai_quant.providers.exchange_calendar import verified_exchange_calendar
        rows, evidence = verified_exchange_calendar(datetime.fromisoformat("2026-09-09T14:00:00+08:00"))
        dates = {row["trade_date"] for row in rows}
        self.assertIn("2026-09-09", dates)
        for day in ("2026-09-25", "2026-10-01", "2026-02-23", "2026-09-20"):
            self.assertNotIn(day, dates)
        self.assertEqual("2026-12-31", evidence["coverage_end"])
        self.assertTrue(evidence["source_url"].startswith("https://www.sse.com.cn/"))
        self.assertEqual(([], {}), verified_exchange_calendar(datetime.fromisoformat("2027-01-01T10:00:00+08:00")))
        self.assertEqual(([], {}), verified_exchange_calendar(datetime.fromisoformat("2026-09-09T12:00:00+08:00")))

    def test_verified_local_calendar_avoids_failed_sina(self):
        from ai_quant.providers.exchange_calendar import verified_exchange_calendar
        result = verified_exchange_calendar(datetime.fromisoformat("2026-09-09T14:00:00+08:00"))
        provider = AkshareProvider(FakeAkshare())
        with patch("ai_quant.providers.exchange_calendar.verified_exchange_calendar", return_value=result):
            self.assertGreater(len(provider.fetch_trading_calendar()), 200)
        self.assertEqual("sse_verified_annual_calendar", provider.sources["trading_calendar"])
        self.assertTrue(provider.trading_calendar_complete)

    def test_dividends_prefer_cninfo_and_accept_empty_history(self) -> None:
        class DividendAkshare(FakeAkshare):
            def stock_dividend_cninfo(self, symbol):
                self.dividend_symbol = symbol
                return Frame([])

            def stock_fhps_detail_em(self, symbol):
                raise AssertionError("CNInfo succeeded with a valid empty history")

            def stock_history_dividend_detail(self, symbol, indicator):
                raise AssertionError("CNInfo succeeded with a valid empty history")

        module = DividendAkshare()
        provider = AkshareProvider(module)
        self.assertEqual([], provider.fetch_dividends("600000"))
        self.assertEqual("600000", module.dividend_symbol)
        self.assertEqual("cninfo", provider.sources["dividends:600000"])

    def test_stock_history_falls_back_to_eastmoney(self) -> None:
        class HistoryAkshare(FakeAkshare):
            def stock_zh_a_daily(self, **kwargs):
                raise RuntimeError("sina unavailable")

            def stock_zh_a_hist(self, **kwargs):
                self.history_kwargs = kwargs
                return Frame([{"日期": "2026-08-21", "开盘": 10, "收盘": 10.1}])

        module = HistoryAkshare()
        provider = AkshareProvider(module)
        rows = provider.fetch_daily_history(
            "600000", "sh", "stock", date(2026, 8, 20), date(2026, 8, 23), "raw"
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("600000", module.history_kwargs["symbol"])
        self.assertEqual("eastmoney", provider.sources["history:600000:raw"])


if __name__ == "__main__":
    unittest.main()
