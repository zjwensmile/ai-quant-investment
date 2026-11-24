from __future__ import annotations

from enum import StrEnum


class OrderState(StrEnum):
    BLOCKED = "blocked"
    WAITING_TRIGGER = "waiting_trigger"
    EXECUTABLE = "executable"
    USER_VIEWED = "user_viewed"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    UNFILLED = "unfilled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ABANDONED = "abandoned"
    RECONCILED = "reconciled"


ALLOWED_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.BLOCKED: {OrderState.EXPIRED, OrderState.ABANDONED},
    OrderState.WAITING_TRIGGER: {
        OrderState.EXECUTABLE, OrderState.EXPIRED, OrderState.ABANDONED,
    },
    OrderState.EXECUTABLE: {
        OrderState.USER_VIEWED, OrderState.EXPIRED, OrderState.ABANDONED,
    },
    OrderState.USER_VIEWED: {
        OrderState.SUBMITTED, OrderState.EXPIRED, OrderState.ABANDONED,
    },
    OrderState.SUBMITTED: {
        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.UNFILLED,
        OrderState.CANCELLED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED,
        OrderState.RECONCILED,
    },
    OrderState.FILLED: {OrderState.RECONCILED},
    OrderState.UNFILLED: {OrderState.RECONCILED},
    OrderState.CANCELLED: {OrderState.RECONCILED},
    OrderState.EXPIRED: set(),
    OrderState.ABANDONED: set(),
    OrderState.RECONCILED: set(),
}


def validate_transition(current: str, target: str) -> tuple[OrderState, OrderState]:
    before = OrderState(current)
    after = OrderState(target)
    if after not in ALLOWED_TRANSITIONS[before]:
        raise ValueError(f"订单状态不能从 {before.value} 转为 {after.value}")
    return before, after
