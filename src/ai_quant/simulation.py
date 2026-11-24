from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .execution import simulate_all_or_none_fill, transaction_fees
from .market_store import MarketStore
from .rules import Side
from .securities import SecurityType
from .store import AuditStore


MODEL_VERSION = "next-session-open-v1-provisional"


class ShadowExecutionEngine:
    """Daily-bar shadow fills. Never writes to the real account ledger."""

    def __init__(self, audit_store: AuditStore, market_store: MarketStore) -> None:
        self.audit_store = audit_store
        self.market_store = market_store

    def settle(self, plan: dict[str, Any], through: date) -> dict[str, int]:
        completed = 0
        pending = 0
        cutoff = date.fromisoformat(str(plan["daily_bar_cutoff"]))
        for order in plan.get("orders", []):
            if order["status"] != "ready":
                self.audit_store.record_shadow_fill(
                    self._blocked(plan, order, "plan_blocked")
                )
                completed += 1
                continue
            baseline = self.market_store.bars(str(order["symbol"]), "raw", 1, cutoff)
            future = self.market_store.bars_between(
                str(order["symbol"]), date.fromordinal(cutoff.toordinal() + 1), through
            )
            if not baseline or not future:
                pending += 1
                continue
            previous_close = Decimal(str(baseline[-1]["close"]))
            available = Decimal(str(future[0]["open"]))
            ratio_value = order.get("price_limit_ratio")
            ratio = Decimal(str(ratio_value)) if ratio_value is not None else None
            at_limit_up = ratio is not None and available >= previous_close * (1 + ratio)
            at_limit_down = ratio is not None and available <= previous_close * (1 - ratio)
            side = Side(order["side"])
            simulated = simulate_all_or_none_fill(
                side, int(order["quantity"]), Decimal(str(order["limit_price"])),
                available, at_limit_up=at_limit_up, at_limit_down=at_limit_down,
            )
            notional = available * simulated.filled_quantity
            fees = (
                transaction_fees(SecurityType(order["security_type"]), side, notional)["total"]
                if simulated.filled_quantity else Decimal("0")
            )
            payload = {
                "order_id": order["order_id"], "run_id": plan["run_id"],
                "plan_id": plan["plan_id"], "model_version": MODEL_VERSION,
                "status": simulated.status.value,
                "requested_quantity": simulated.requested_quantity,
                "filled_quantity": simulated.filled_quantity,
                "fill_trade_date": future[0]["trade_date"],
                "fill_price": float(available) if simulated.filled_quantity else None,
                "notional": float(notional), "fees": float(fees),
                "never_updates_real_account": True,
            }
            self.audit_store.record_shadow_fill(payload)
            completed += 1
        result = {"completed": completed, "pending": pending}
        self.audit_store.record_shadow_settlement({
            "run_id": plan["run_id"],
            "plan_id": plan["plan_id"],
            "as_of_date": str(plan["daily_bar_cutoff"]),
            "model_version": MODEL_VERSION,
            "status": "completed" if pending == 0 else "pending",
            "orders": len(plan.get("orders", [])),
            **result,
        })
        return result

    @staticmethod
    def _blocked(plan: dict[str, Any], order: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "order_id": order["order_id"], "run_id": plan["run_id"],
            "plan_id": plan["plan_id"], "model_version": MODEL_VERSION,
            "status": status, "requested_quantity": int(order.get("quantity") or 0),
            "filled_quantity": 0, "fill_trade_date": None, "fill_price": None,
            "notional": 0.0, "fees": 0.0, "never_updates_real_account": True,
        }
