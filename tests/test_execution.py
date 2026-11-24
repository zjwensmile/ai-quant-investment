from __future__ import annotations

import unittest
from decimal import Decimal

from ai_quant.execution import FillStatus, simulate_all_or_none_fill, transaction_fees
from ai_quant.rules import Side
from ai_quant.securities import SecurityType


class ExecutionTests(unittest.TestCase):
    def test_limit_up_buy_and_limit_down_sell_do_not_fill(self) -> None:
        buy = simulate_all_or_none_fill(
            Side.BUY, 100, Decimal("10"), Decimal("10"), at_limit_up=True
        )
        sell = simulate_all_or_none_fill(
            Side.SELL, 100, Decimal("10"), Decimal("10"), at_limit_down=True
        )
        self.assertEqual(FillStatus.BLOCKED_LIMIT_UP, buy.status)
        self.assertEqual(0, buy.filled_quantity)
        self.assertEqual(FillStatus.BLOCKED_LIMIT_DOWN, sell.status)
        self.assertEqual(0, sell.filled_quantity)

    def test_marketable_order_fills_completely(self) -> None:
        fill = simulate_all_or_none_fill(
            Side.BUY, 4200, Decimal("1.17"), Decimal("1.168")
        )
        self.assertEqual(FillStatus.FILLED, fill.status)
        self.assertEqual(4200, fill.filled_quantity)
        self.assertEqual(Decimal("1.168"), fill.fill_price)

    def test_fixed_commission_and_stock_sell_stamp_duty(self) -> None:
        stock = transaction_fees(
            SecurityType.STOCK, Side.SELL, Decimal("10000")
        )
        etf = transaction_fees(SecurityType.ETF, Side.SELL, Decimal("10000"))
        self.assertEqual(Decimal("1"), stock["commission"])
        self.assertEqual(Decimal("5.0000"), stock["stamp_duty"])
        self.assertEqual(Decimal("1"), etf["total"])


if __name__ == "__main__":
    unittest.main()
