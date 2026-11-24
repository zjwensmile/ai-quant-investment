from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .rules import Side
from .securities import SecurityType


class FillStatus(StrEnum):
    FILLED = "filled"
    NOT_MARKETABLE = "not_marketable"
    BLOCKED_LIMIT_UP = "blocked_limit_up"
    BLOCKED_LIMIT_DOWN = "blocked_limit_down"


@dataclass(frozen=True)
class SimulatedFill:
    status: FillStatus
    requested_quantity: int
    filled_quantity: int
    fill_price: Decimal | None


def simulate_all_or_none_fill(
    side: Side,
    quantity: int,
    limit_price: Decimal,
    available_price: Decimal,
    *,
    at_limit_up: bool = False,
    at_limit_down: bool = False,
) -> SimulatedFill:
    """Apply the confirmed fill constraints after a price model selects a price."""
    if quantity <= 0:
        raise ValueError("委托数量必须大于 0")
    if side is Side.BUY and at_limit_up:
        return SimulatedFill(FillStatus.BLOCKED_LIMIT_UP, quantity, 0, None)
    if side is Side.SELL and at_limit_down:
        return SimulatedFill(FillStatus.BLOCKED_LIMIT_DOWN, quantity, 0, None)

    marketable = (
        available_price <= limit_price
        if side is Side.BUY
        else available_price >= limit_price
    )
    if not marketable:
        return SimulatedFill(FillStatus.NOT_MARKETABLE, quantity, 0, None)
    return SimulatedFill(FillStatus.FILLED, quantity, quantity, available_price)


def transaction_fees(
    security_type: SecurityType,
    side: Side,
    notional: Decimal,
    *,
    commission_fixed_per_order: Decimal = Decimal("1"),
    stock_sell_stamp_duty_rate: Decimal = Decimal("0.0005"),
) -> dict[str, Decimal]:
    if notional < 0:
        raise ValueError("成交金额不能为负数")
    commission = commission_fixed_per_order
    stamp_duty = (
        notional * stock_sell_stamp_duty_rate
        if security_type is SecurityType.STOCK and side is Side.SELL
        else Decimal("0")
    )
    return {
        "commission": commission,
        "stamp_duty": stamp_duty,
        "total": commission + stamp_duty,
    }
