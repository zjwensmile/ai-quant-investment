from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any, Iterable

from .execution import transaction_fees
from .order_state import OrderState, validate_transition
from .rules import Side
from .securities import SecurityType, normalize_symbol


class AccountLedger:
    """Reduce append-only account events into one auditable account state."""

    @staticmethod
    def reduce(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
        state: dict[str, Any] | None = None
        for event in events:
            payload = event.get("payload", event.get("payload_json", {}))
            event_type = str(event["event_type"])
            if event_type == "account_initialized":
                state = copy.deepcopy(payload)
                continue
            if state is None:
                raise ValueError("首个账户事件必须是 account_initialized")
            if event_type == "cash_reconciled":
                state["cash"] = float(payload["cash"])
                state["cash_status"] = "confirmed"
            elif event_type == "position_reconciled":
                _replace_position(state, payload)
            elif event_type == "fill":
                _apply_fill(state, payload)
            elif event_type == "settlement":
                position = _position(state, str(payload["symbol"]))
                available = int(payload["available_quantity"])
                if available < 0 or available > int(position["quantity"]):
                    raise ValueError("可用数量必须在 0 和持仓数量之间")
                position["available_quantity"] = available
            else:
                raise ValueError(f"未知账户事件类型: {event_type}")
            state["version"] = str(event.get("event_id") or state.get("version", "ledger"))
        return state


def build_fill_payload(
    *,
    symbol: str,
    name: str,
    security_type: SecurityType,
    side: Side,
    quantity: int,
    price: Decimal,
    same_day_turnaround: bool = False,
) -> dict[str, Any]:
    if quantity <= 0 or price <= 0:
        raise ValueError("成交数量和价格必须大于 0")
    notional = price * quantity
    fees = transaction_fees(security_type, side, notional)
    return {
        "symbol": normalize_symbol(symbol),
        "name": name,
        "security_type": security_type.value,
        "side": side.value,
        "quantity": quantity,
        "price": str(price),
        "notional": str(notional),
        "commission": str(fees["commission"]),
        "stamp_duty": str(fees["stamp_duty"]),
        "total_fees": str(fees["total"]),
        "same_day_turnaround": same_day_turnaround,
    }


def record_confirmed_fill(
    store: Any,
    *,
    account_event_id: str,
    state_event_id: str,
    event_time: str,
    payload: dict[str, Any],
    order_id: str | None = None,
) -> dict[str, Any]:
    candidate_payload = copy.deepcopy(payload)
    if not order_id:
        candidate = store.list_account_events() + [{
            "event_id": account_event_id, "event_type": "fill",
            "payload": candidate_payload,
        }]
        state = AccountLedger.reduce(candidate)
        store.append_account_event(account_event_id, "fill", event_time, candidate_payload)
        return state
    current = store.current_order_state(order_id)
    if current is None:
        raise ValueError(f"找不到订单: {order_id}")
    if current["to_state"] not in {
        OrderState.SUBMITTED.value, OrderState.PARTIALLY_FILLED.value,
    }:
        raise ValueError("只有已下单或部分成交订单可以绑定实际成交")
    plan = store.get_order_plan_by_id(str(current["plan_id"]))
    order = next(
        (item for item in (plan or {}).get("orders", []) if item["order_id"] == order_id),
        None,
    )
    if order is None:
        raise ValueError("订单计划中找不到该订单")
    prior = store.linked_filled_quantity(order_id)
    quantity = int(candidate_payload["quantity"])
    remaining = int(order["quantity"]) - prior
    if quantity > remaining:
        raise ValueError("实际成交数量超过订单剩余数量")
    if prior > 0:
        candidate_payload["commission"] = "0"
        candidate_payload["total_fees"] = candidate_payload["stamp_duty"]
    candidate_payload["order_id"] = order_id
    candidate = store.list_account_events() + [{
        "event_id": account_event_id, "event_type": "fill", "payload": candidate_payload,
    }]
    state = AccountLedger.reduce(candidate)
    target = (
        OrderState.FILLED.value if quantity == remaining
        else OrderState.PARTIALLY_FILLED.value
    )
    validate_transition(str(current["to_state"]), target)
    store.append_linked_fill(
        account_event_id, event_time, candidate_payload, state_event_id, order_id,
        str(current["plan_id"]), str(current["to_state"]), target,
    )
    return state


def _apply_fill(state: dict[str, Any], payload: dict[str, Any]) -> None:
    symbol = normalize_symbol(str(payload["symbol"]))
    quantity = int(payload["quantity"])
    price = Decimal(str(payload["price"]))
    fees = Decimal(str(payload["total_fees"]))
    side = Side(str(payload["side"]))
    positions = state.setdefault("positions", [])
    position = next((item for item in positions if item["symbol"] == symbol), None)
    if side is Side.BUY:
        old_quantity = int((position or {}).get("quantity") or 0)
        if position is None:
            position = {
                "symbol": symbol,
                "name": payload.get("name", symbol),
                "security_type": payload["security_type"],
                "quantity": 0,
                "available_quantity": 0,
                "frozen_quantity": 0,
                "cost_price": 0.0,
            }
            positions.append(position)
        old_cost = Decimal(str(position.get("cost_price") or 0)) * old_quantity
        position["quantity"] = old_quantity + quantity
        if payload.get("same_day_turnaround"):
            position["available_quantity"] = int(position.get("available_quantity") or 0) + quantity
        position["cost_price"] = float((old_cost + price * quantity + fees) / (old_quantity + quantity))
        cash_effect = -(price * quantity + fees)
    else:
        if position is None or int(position.get("quantity") or 0) < quantity:
            raise ValueError(f"{symbol} 卖出数量超过持仓")
        if int(position.get("available_quantity") or 0) < quantity:
            raise ValueError(f"{symbol} 卖出数量超过可用数量")
        position["quantity"] = int(position["quantity"]) - quantity
        position["available_quantity"] = int(position["available_quantity"]) - quantity
        cash_effect = price * quantity - fees
        if position["quantity"] == 0:
            positions.remove(position)
    payload["cash_effect"] = str(cash_effect)
    if state.get("cash") is not None:
        state["cash"] = float(Decimal(str(state["cash"])) + cash_effect)


def _position(state: dict[str, Any], symbol: str) -> dict[str, Any]:
    code = normalize_symbol(symbol)
    for item in state.setdefault("positions", []):
        if item["symbol"] == code:
            return item
    raise ValueError(f"账户中没有持仓 {code}")


def _replace_position(state: dict[str, Any], payload: dict[str, Any]) -> None:
    positions = state.setdefault("positions", [])
    symbol = normalize_symbol(str(payload["symbol"]))
    replacement = copy.deepcopy(payload)
    replacement["symbol"] = symbol
    quantity = int(replacement.get("quantity") or 0)
    available = int(replacement.get("available_quantity") or 0)
    frozen = int(replacement.get("frozen_quantity") or 0)
    if quantity < 0 or available < 0 or frozen < 0 or available + frozen > quantity:
        raise ValueError("券商对账持仓数量不一致")
    positions[:] = [item for item in positions if item["symbol"] != symbol]
    if quantity > 0:
        positions.append(replacement)
    positions.sort(key=lambda item: item["symbol"])
